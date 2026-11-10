"""Bounded stateful scenarios and native-first metamorphic adjudication for P7-07."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from hypothesis import find, settings, strategies as st

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/p7-p707-stateful-pilot-20261110.json"
MAX_SEQUENCE_LENGTH = 6
FIXED_SEED = 20261110


@dataclass(frozen=True)
class Action:
    command_id: str
    actor_id: str
    conversation_kind: str
    conversation_id: str


def fixed_contexts() -> list[dict[str, str]]:
    """Return the required two actors crossed with private/group sessions."""
    return [
        {"actor_id": actor, "conversation_kind": kind, "conversation_id": conversation}
        for actor, private_id, group_id in (("actor-a", "user-a", "group-a"), ("actor-b", "user-b", "group-b"))
        for kind, conversation in (("private", private_id), ("group", group_id))
    ]


class SyntheticCounterPlugin:
    """Small self-owned per-conversation counter used to validate sequence logic."""

    def __init__(self, *, seed: int = 2, global_state: bool = False) -> None:
        self.seed = seed
        self.global_state = global_state
        self.state: dict[str, int] = {}

    def _key(self, action: Action) -> str:
        return "global" if self.global_state else f"{action.conversation_kind}:{action.conversation_id}"

    def run(self, actions: Iterable[Action]) -> list[dict[str, Any]]:
        outputs = []
        for action in actions:
            key = self._key(action)
            value = self.state.setdefault(key, self.seed)
            if action.command_id == "increment":
                value += 1
                self.state[key] = value
                reply = value
            elif action.command_id == "clear":
                value = 0
                self.state[key] = value
                reply = value
            elif action.command_id == "read":
                reply = value
            else:
                raise ValueError(f"unknown synthetic command: {action.command_id}")
            outputs.append({"command_id": action.command_id, "reply": reply, "actor_id": action.actor_id, "conversation_id": action.conversation_id})
        return outputs


class SyntheticTeamupPlugin:
    """Self-owned group roster fixture whose commands have observable state dependencies."""

    def __init__(self, *, seed_members: Iterable[str] = ()) -> None:
        self.members: dict[str, set[str]] = {}
        self.seed_members = set(seed_members)

    def run(self, actions: Iterable[Action]) -> list[dict[str, Any]]:
        outputs = []
        for action in actions:
            members = self.members.setdefault(action.conversation_id, set(self.seed_members))
            if action.command_id == "join":
                members.add(action.actor_id)
                reply: Any = len(members)
            elif action.command_id == "leave":
                members.discard(action.actor_id)
                reply = len(members)
            elif action.command_id == "members":
                reply = sorted(members)
            else:
                raise ValueError(f"unknown synthetic teamup command: {action.command_id}")
            outputs.append({"command_id": action.command_id, "reply": reply, "actor_id": action.actor_id, "conversation_id": action.conversation_id})
        return outputs


def infer_dependencies(
    command_ids: list[str],
    execute: Callable[[list[str]], list[Any]],
) -> list[dict[str, str]]:
    """Infer A→B when B's output changes after A, compared with isolated B."""
    edges = []
    for first in command_ids:
        for second in command_ids:
            if first == second:
                continue
            baseline = execute([second])
            after_first = execute([first, second])
            if len(baseline) != 1 or len(after_first) != 2:
                raise ValueError("dependency probe executor must return one output per action")
            if baseline[0] != after_first[1]:
                edges.append({"before": first, "after": second})
    return edges


def infer_counter_dependencies() -> list[dict[str, str]]:
    def execute(command_ids: list[str]) -> list[Any]:
        context = fixed_contexts()[0]
        actions = [Action(command, **context) for command in command_ids]
        return [row["reply"] for row in SyntheticCounterPlugin().run(actions)]

    return infer_dependencies(["increment", "clear", "read"], execute)


def infer_teamup_dependencies() -> list[dict[str, str]]:
    context = next(row for row in fixed_contexts() if row["conversation_kind"] == "group")

    def infer_with_seed(seed_members: Iterable[str]) -> list[dict[str, str]]:
        def execute(command_ids: list[str]) -> list[Any]:
            actions = [Action(command, **context) for command in command_ids]
            return [row["reply"] for row in SyntheticTeamupPlugin(seed_members=seed_members).run(actions)]

        return infer_dependencies(["join", "leave", "members"], execute)

    combined = {
        (row["before"], row["after"])
        for seeded in ((), (context["actor_id"],))
        for row in infer_with_seed(seeded)
    }
    return [{"before": before, "after": after} for before, after in sorted(combined)]


def generate_sequences(
    dependency_edges: list[dict[str, str]],
    *,
    seed: int = FIXED_SEED,
    max_length: int = MAX_SEQUENCE_LENGTH,
) -> list[list[Action]]:
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("stateful sequence seed must be a non-negative integer")
    if not isinstance(max_length, int) or not 1 <= max_length <= MAX_SEQUENCE_LENGTH:
        raise ValueError(f"stateful sequence length must be in 1..{MAX_SEQUENCE_LENGTH}")
    contexts = fixed_contexts()
    edges = sorted({(row["before"], row["after"]) for row in dependency_edges})
    outgoing: dict[str, list[str]] = {}
    for before, after in edges:
        outgoing.setdefault(before, []).append(after)
    paths: list[list[str]] = []

    def extend(path: list[str]) -> None:
        if len(path) >= 2:
            paths.append(path)
        if len(path) >= max_length:
            return
        for following in outgoing.get(path[-1], []):
            if following not in path:
                extend([*path, following])

    for start in sorted(outgoing):
        extend([start])
    sequences: list[list[Action]] = []
    for path in paths:
        for context in contexts:
            sequences.append([Action(command_id, **context) for command_id in path])
    # Seeded rotation changes order reproducibly while preserving all actor/session pairs.
    if sequences:
        offset = seed % len(sequences)
        sequences = sequences[offset:] + sequences[:offset]
    return [sequence[:max_length] for sequence in sequences]


def shrink_failing_sequence(sequence: list[Action], is_failure: Callable[[list[Action]], bool]) -> list[Action]:
    """Shrink to a failing subsequence using Hypothesis' deterministic shrinker."""
    if not sequence or len(sequence) > MAX_SEQUENCE_LENGTH:
        raise ValueError("a failing sequence must contain 1..6 actions")
    indices = find(
        st.lists(st.integers(min_value=0, max_value=len(sequence) - 1), min_size=1, max_size=len(sequence)),
        lambda values: values == sorted(set(values)) and is_failure([sequence[index] for index in values]),
        settings=settings(database=None, derandomize=True, max_examples=1000, deadline=None),
    )
    return [sequence[index] for index in indices]


def evaluate_metamorphic(
    relation_id: str,
    native_before: Any,
    native_after: Any,
    federated_before: Any,
    federated_after: Any,
) -> dict[str, Any]:
    """Require Federated to preserve only relations that Native satisfies."""
    native_holds = native_before == native_after
    if not native_holds:
        return {"status": "skipped", "reason_code": "native_relation_not_satisfied", "relation_id": relation_id}
    federated_holds = federated_before == federated_after
    return {
        "status": "passed" if federated_holds else "failed",
        "reason_code": None if federated_holds else "federated_relation_violated",
        "relation_id": relation_id,
    }


def _pilot_entry_conclusions(entry: dict[str, Any]) -> dict[str, Any]:
    session_result = (
        {
            "status": "skipped",
            "reason_code": "pilot_entry_skipped_before_stateful_probe",
            "native_relation": "not_observed",
        }
        if entry.get("status") == "skipped"
        else {
            "status": "unknown",
            "reason_code": "p7_p704_did_not_run_multi_actor_stateful_sequences",
            "native_relation": "not_observed",
        }
    )
    return {
        "session_isolation": session_result,
        "restart_persistence": {
            "status": "skipped",
            "reason_code": "optional_restart_relation_deferred_until_stateful_oracle_is_established",
        },
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_pilot_report(pilot: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    if pilot.get("kind") != "p7_p704_generated_differential_pilot" or pilot.get("status") != "completed":
        raise ValueError("expected a completed P7-04 differential pilot report")
    entries = []
    totals = {
        relation: {status: 0 for status in ("passed", "failed", "skipped", "unknown")}
        for relation in ("session_isolation", "restart_persistence")
    }
    for source in pilot.get("entries", []):
        conclusions = _pilot_entry_conclusions(source)
        for relation, result in conclusions.items():
            totals[relation][result["status"]] += 1
        entries.append({
            "registry_entry_id": source.get("registry_entry_id"),
            "ecosystem": source.get("ecosystem"),
            "package": source.get("package"),
            "version": source.get("version"),
            "pilot_status": source.get("status"),
            "relations": conclusions,
        })
    dependencies = infer_counter_dependencies()
    sequences = generate_sequences(dependencies)
    teamup_dependencies = infer_teamup_dependencies()
    teamup_sequences = generate_sequences(teamup_dependencies)
    return {
        "schema_version": 1,
        "kind": "p7_p707_stateful_adjudication",
        "status": "completed",
        "source_report": source_path.resolve().relative_to(ROOT).as_posix() if source_path.resolve().is_relative_to(ROOT) else str(source_path.resolve()),
        "source_report_sha256": _sha256(source_path.read_bytes()),
        "scope": {
            "actors": 2,
            "conversations_per_actor": ["private", "group"],
            "maximum_sequence_length": MAX_SEQUENCE_LENGTH,
            "generation_seed": FIXED_SEED,
            "native_first_rule": "A relation is checked in Federated only when the Native observations satisfy it; otherwise it is skipped with the native reason.",
            "pilot_note": "P7-04 did not dispatch multiple actors or stateful sequences; each pilot relation remains unknown rather than inferred from command differential outcomes.",
        },
        "synthetic_fixture": {
            "plugins": [
                {
                    "plugin": "self-owned synthetic per-conversation counter",
                    "commands": ["increment", "clear", "read"],
                    "dependency_edges": dependencies,
                    "generated_sequences": len(sequences),
                },
                {
                    "plugin": "self-owned synthetic group roster",
                    "commands": ["join", "leave", "members"],
                    "dependency_edges": teamup_dependencies,
                    "generated_sequences": len(teamup_sequences),
                },
            ],
            "generated_sequences": len(sequences) + len(teamup_sequences),
            "contexts": len(fixed_contexts()),
            "maximum_generated_length": max((len(sequence) for sequence in sequences + teamup_sequences), default=0),
            "sequence_set_sha256": _sha256(_canonical([
                [[asdict(action) for action in sequence] for sequence in sequences],
                [[asdict(action) for action in sequence] for sequence in teamup_sequences],
            ])),
        },
        "counts": {"entries": len(entries), "relation_positions": len(entries) * 2, "by_relation": totals},
        "entries": entries,
    }


def write_report(path: Path, report: dict[str, Any]) -> str:
    path = path.expanduser().absolute()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite P7-07 report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    digest = _sha256(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True, help="completed P7-04 report")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args(argv)
    source_path = options.pilot.expanduser().resolve(strict=True)
    pilot = json.loads(source_path.read_text(encoding="utf-8"))
    report = build_pilot_report(pilot, source_path=source_path)
    digest = write_report(options.output, report)
    print(json.dumps({"status": report["status"], "entries": report["counts"]["entries"], "relation_positions": report["counts"]["relation_positions"], "synthetic_sequences": report["synthetic_fixture"]["generated_sequences"], "output": str(options.output.resolve()), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
