"""Small, deterministic P7 mutation campaign over self-owned plugin traces.

Mutations are applied to synthetic observations and fixture state outside the
production Core/Host binaries. The report measures oracle sensitivity at each
layer; it is not a mutation score for production runtime code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from lab.automation.p7_contracts import evaluate_contract
from lab.automation.p7_differential_campaign import _round_scenario_results

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/p7-p706-mutation-regression-20261110.json"
MUTATION_VERSION = "p7-synthetic-observations-v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _operation(text: str, *, event_id: str = "evt-a") -> dict[str, Any]:
    return {
        "source_event_id": event_id,
        "account_id": "bot-a",
        "operation": "send_message",
        "target": {"kind": "group", "id": "group-a"},
        "params": {"message": [
            {"type": "text", "text": text},
            {"type": "image", "url": "fixture://image-a"},
        ]},
    }


def _baseline() -> list[dict[str, Any]]:
    return [_operation("alpha"), _operation("beta")]


def _mutated(mutant_id: str) -> dict[str, Any]:
    operations = _baseline()
    if mutant_id == "wrong_session":
        operations[0]["target"] = {"kind": "private", "id": "user-b"}
    elif mutant_id == "wrong_account":
        operations[0]["account_id"] = "bot-b"
    elif mutant_id == "drop_reply":
        operations = operations[1:]
    elif mutant_id == "duplicate_reply":
        operations.insert(1, json.loads(json.dumps(operations[0])))
    elif mutant_id == "discard_non_text_segment":
        operations[0]["params"]["message"] = [segment for segment in operations[0]["params"]["message"] if segment["type"] == "text"]
    elif mutant_id == "truncate_text":
        operations[0]["params"]["message"][0]["text"] = "alp"
    elif mutant_id == "reorder_replies":
        operations.reverse()
    elif mutant_id == "swallow_handler_exception":
        return {"operations": operations, "handler_exception": {
            "raised": False,
            "host_alive_after": True,
            "next_event_status": "completed",
        }}
    elif mutant_id == "cross_actor_state":
        return {"operations": operations, "actor_state": {
            "native_before": 0,
            "native_after_actor_a": 0,
            "federated_before": 0,
            "federated_after_actor_a": 1,
        }}
    elif mutant_id == "share_data_directory":
        return {"operations": operations, "actor_data_directories": {
            "actor-a": "/work/shared",
            "actor-b": "/work/shared",
        }}
    else:
        raise ValueError(f"unknown P7 mutant: {mutant_id}")
    return {"operations": operations}


def _differential_detected(mutant: dict[str, Any]) -> bool:
    events = [{"scenario_id": "synthetic-case", "event_id": "evt-a", "command_id": "echo", "kind": "raw_text"}]
    native = {"status": "completed", "operations": _baseline()}
    mutated = {"status": "completed", "operations": mutant["operations"]}
    rows, _, _, _ = _round_scenario_results(events, native, native, mutated)
    return rows[0]["status"] == "non_equivalent"


def _contract_detected(mutant_id: str, mutant: dict[str, Any]) -> bool:
    if mutant_id in {"wrong_session", "wrong_account"}:
        result = evaluate_contract("C3", {
            "events": {"evt-a": {"native_allowed_targets": [
                {"account_id": "bot-a", "kind": "group", "id": "group-a"},
            ]}},
            "operations": [{
                "event_id": "evt-a",
                "target": {"account_id": mutant["operations"][0]["account_id"], **mutant["operations"][0]["target"]},
            }],
        })
        return result["status"] == "failed"
    if mutant_id == "swallow_handler_exception":
        result = evaluate_contract("C5", {"handler_exception": mutant.get("handler_exception")})
        return result["status"] == "failed"
    return False


def _stateful_detected(mutant_id: str, mutant: dict[str, Any]) -> bool:
    if mutant_id != "cross_actor_state":
        return False
    state = mutant["actor_state"]
    native_isolated = state["native_before"] == state["native_after_actor_a"]
    federated_isolated = state["federated_before"] == state["federated_after_actor_a"]
    return native_isolated and not federated_isolated


def _fixture_detected(mutant_id: str, mutant: dict[str, Any]) -> bool:
    if mutant_id != "share_data_directory":
        return False
    directories = mutant["actor_data_directories"]
    return len(set(directories.values())) != len(directories)


MUTANTS: tuple[tuple[str, str], ...] = (
    ("wrong_session", "contract and differential"),
    ("wrong_account", "contract and differential"),
    ("drop_reply", "differential"),
    ("duplicate_reply", "differential"),
    ("discard_non_text_segment", "differential"),
    ("truncate_text", "differential"),
    ("reorder_replies", "differential"),
    ("swallow_handler_exception", "contract"),
    ("cross_actor_state", "stateful"),
    ("share_data_directory", "fixture"),
)


def run_campaign() -> dict[str, Any]:
    rows = []
    detections = {layer: 0 for layer in ("fixture", "contract", "differential", "stateful")}
    for mutant_id, _planned_layer in MUTANTS:
        mutant = _mutated(mutant_id)
        by_layer = {
            "fixture": _fixture_detected(mutant_id, mutant),
            "contract": _contract_detected(mutant_id, mutant),
            "differential": _differential_detected(mutant) if mutant_id not in {"wrong_session", "wrong_account", "swallow_handler_exception", "cross_actor_state", "share_data_directory"} else False,
            "stateful": _stateful_detected(mutant_id, mutant),
        }
        detected_by = [layer for layer, detected in by_layer.items() if detected]
        for layer in detected_by:
            detections[layer] += 1
        rows.append({
            "mutant_id": mutant_id,
            "detected": bool(detected_by),
            "detected_by": detected_by,
            "survival_reason": None if detected_by else "no layer detected this mutation",
            "synthetic_observation_sha256": _sha256(_canonical(mutant)),
        })
    detected_count = sum(row["detected"] for row in rows)
    return {
        "schema_version": 1,
        "kind": "p7_p706_synthetic_mutation_regression",
        "mutation_version": MUTATION_VERSION,
        "status": "passed" if detected_count == len(MUTANTS) else "failed",
        "scope": {
            "mutants": "self-owned synthetic event observations and fixture state",
            "third_party_artifacts_modified": False,
            "production_core_host_modified_for_injection": False,
            "limitation": "This measures oracle sensitivity on synthetic observations; it is not a mutation score for compiled Core/Host runtime code.",
        },
        "counts": {
            "mutants": len(rows),
            "detected": detected_count,
            "survived": len(rows) - detected_count,
            "mutation_score": detected_count / len(rows) if rows else None,
            "detected_by_layer": detections,
        },
        "layers": ["fixture", "contract", "differential", "stateful"],
        "mutants": rows,
    }


def write_report(path: Path, report: dict[str, Any]) -> str:
    path = path.expanduser().absolute()
    if path.exists():
        raise FileExistsError(f"refusing to overwrite P7-06 report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    digest = _sha256(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args(argv)
    report = run_campaign()
    digest = write_report(options.output, report)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "output": str(options.output.resolve()), "sha256": digest}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
