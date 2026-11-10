"""Deterministically generate source-bound command scenarios without running plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from hypothesis import find, settings, strategies as st

GENERATOR_NAME = "ligamina-scenario-generator"
GENERATOR_VERSION = "1"
DEFAULT_FIXED_TIME = "2026-10-26T00:00:00Z"
DEFAULT_VOLATILE_FIELDS = (
    "event_id",
    "trace_id",
    "timestamp",
    "message_id",
    "request_id",
    "generated_at",
    "duration_ms",
    "elapsed_ms",
    "latency_ms",
)
MAX_COMMAND_ARGUMENTS_BYTES = 4096
MAX_GENERATED_SCENARIOS = 16
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b"
)
EXAMPLE_RE = re.compile(r"(?:`([^`\n]{1,128})`|'([^'\n]{1,128})'|\"([^\"\n]{1,128})\")")


class ScenarioGenerationError(ValueError):
    """The descriptor or requested scenario bounds are invalid."""


@dataclass(frozen=True)
class PairwiseResult:
    rows: list[dict[str, Any]]
    covered_pairs: int
    total_pairs: int

    @property
    def complete(self) -> bool:
        return self.covered_pairs == self.total_pairs


@dataclass(frozen=True)
class ScenarioBatch:
    scenarios: list[dict[str, Any]]
    pairwise: dict[str, Any]
    source_inventory_sha256: str
    command_descriptor_sha256: str
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION, "seed": self.seed},
            "source_inventory_sha256": self.source_inventory_sha256,
            "command_descriptor_sha256": self.command_descriptor_sha256,
            "pairwise": self.pairwise,
            "scenarios": self.scenarios,
        }


@dataclass(frozen=True)
class _Factor:
    key: str
    parameter: dict[str, Any]
    values: list[Any]
    role: str


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def type_domain(parameter: dict[str, Any], *, maximum_text_length: int = 2048) -> list[Any]:
    """Return a small, ordered boundary domain based on a command parameter."""
    choices = parameter.get("enum", parameter.get("choices"))
    if isinstance(choices, list) and choices:
        examples = [value for value in _description_examples(parameter) if value in choices]
        return _unique_json([*choices, *examples])
    values: list[Any] = []
    values.extend(_description_examples(parameter))
    raw_type = str(parameter.get("type", "text")).casefold()
    if raw_type in {"boolean", "bool", "flag"}:
        values.extend([False, True])
    elif raw_type in {"integer", "int", "number", "float", "decimal"}:
        lower = parameter.get("minimum", parameter.get("min"))
        upper = parameter.get("maximum", parameter.get("max"))
        if raw_type in {"integer", "int"}:
            values.extend([0, -1, 1, -(2**63), 2**63 - 1])
            if isinstance(lower, int) and not isinstance(lower, bool):
                values.append(lower)
            if isinstance(upper, int) and not isinstance(upper, bool):
                values.append(upper)
        else:
            values.extend([0.0, -1.0, 1.0, -1e308, 1e308])
            if isinstance(lower, (int, float)) and not isinstance(lower, bool):
                values.append(float(lower))
            if isinstance(upper, (int, float)) and not isinstance(upper, bool):
                values.append(float(upper))
    elif raw_type in {"array", "list"}:
        values.extend([[], ["sample"], ["值🙂", 1]])
    elif raw_type in {"object", "dict", "json"}:
        values.extend([{}, {"value": "sample"}, {"值": [1, True]}])
    else:
        values.extend([
            "sample",
            "",
            "a b",
            " leading",
            "trailing ",
            " \t ",
            "中文🙂",
            "full-width：Ａ",
            "quote \" and slash \\",
            "x" * maximum_text_length,
        ])
    return _unique_json(values)


def parameter_strategy(parameter: dict[str, Any], *, maximum_text_length: int = 2048):
    """Hypothesis strategy for one descriptor parameter, including boundary values."""
    raw_type = str(parameter.get("type", "text")).casefold()
    fixed = st.sampled_from(type_domain(parameter, maximum_text_length=maximum_text_length))
    if isinstance(parameter.get("enum", parameter.get("choices")), list) and parameter.get("enum", parameter.get("choices")):
        return fixed
    if raw_type in {"boolean", "bool", "flag"}:
        return st.one_of(fixed, st.booleans())
    if raw_type in {"integer", "int"}:
        minimum = parameter.get("minimum", parameter.get("min", -2**31))
        maximum = parameter.get("maximum", parameter.get("max", 2**31 - 1))
        minimum = minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else -2**31
        maximum = maximum if isinstance(maximum, int) and not isinstance(maximum, bool) else 2**31 - 1
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        return st.one_of(fixed, st.integers(min_value=minimum, max_value=maximum))
    if raw_type in {"number", "float", "decimal"}:
        return st.one_of(fixed, st.floats(allow_nan=False, allow_infinity=False, width=32))
    if raw_type in {"array", "list"}:
        return st.one_of(fixed, st.lists(st.text(max_size=64), max_size=4))
    if raw_type in {"object", "dict", "json"}:
        return st.one_of(fixed, st.dictionaries(st.text(min_size=1, max_size=12), st.integers(-2, 2), max_size=4))
    return st.one_of(fixed, st.text(max_size=maximum_text_length))


def pairwise_cases(
    factors: Sequence[_Factor], *, max_rows: int, seed: int = 0
) -> PairwiseResult:
    """Build deterministic rows that cover every value pair while the row budget allows."""
    if max_rows <= 0:
        raise ScenarioGenerationError("max_rows must be positive")
    domains = [_unique_json(factor.values) for factor in factors]
    if any(not domain for domain in domains):
        raise ScenarioGenerationError("pairwise factors must have non-empty domains")
    if not factors:
        return PairwiseResult([{}], 0, 0)
    if len(factors) == 1:
        rows = [{factors[0].key: value} for value in domains[0][:max_rows]]
        return PairwiseResult(rows, 0, 0)

    uncovered: set[tuple[int, int, int, int]] = {
        (left, left_value, right, right_value)
        for left in range(len(domains))
        for right in range(left + 1, len(domains))
        for left_value in range(len(domains[left]))
        for right_value in range(len(domains[right]))
    }
    total_pairs = len(uncovered)
    rows: list[dict[str, Any]] = []
    while uncovered and len(rows) < max_rows:
        left, left_value, right, right_value = min(uncovered)
        selected = {left: left_value, right: right_value}
        for factor_index, domain in enumerate(domains):
            if factor_index in selected:
                continue
            scores = []
            for candidate in range(len(domain)):
                score = 0
                for previous_index, previous_value in selected.items():
                    if factor_index < previous_index:
                        first, first_value = factor_index, candidate
                        second, second_value = previous_index, previous_value
                    else:
                        first, first_value = previous_index, previous_value
                        second, second_value = factor_index, candidate
                    score += (first, first_value, second, second_value) in uncovered
                rotation = (candidate - ((seed + factor_index + len(rows)) % len(domain))) % len(domain)
                scores.append((score, -rotation, -candidate, candidate))
            selected[factor_index] = max(scores)[-1]
        row = {factors[index].key: domains[index][selected[index]] for index in range(len(factors))}
        rows.append(row)
        covered = {
            (left_index, left_value, right_index, right_value)
            for left_index in range(len(domains))
            for right_index in range(left_index + 1, len(domains))
            for left_value in [selected[left_index]]
            for right_value in [selected[right_index]]
        }
        uncovered.difference_update(covered)
    return PairwiseResult(rows, total_pairs - len(uncovered), total_pairs)


def generate_scenarios(
    *,
    inventory_sha256: str,
    descriptor: dict[str, Any],
    host_id: str,
    seed: int,
    max_scenarios: int = MAX_GENERATED_SCENARIOS,
    fixed_time_utc: str = DEFAULT_FIXED_TIME,
) -> ScenarioBatch:
    """Generate deterministic, schema-shaped command cases; never loads or invokes a plugin."""
    if not SHA256_RE.fullmatch(inventory_sha256):
        raise ScenarioGenerationError("inventory_sha256 must be lowercase SHA-256")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 or seed > 2**53 - 1:
        raise ScenarioGenerationError("seed must be a non-negative safe integer")
    if not 1 <= max_scenarios <= MAX_GENERATED_SCENARIOS:
        raise ScenarioGenerationError(f"max_scenarios must be between 1 and {MAX_GENERATED_SCENARIOS}")
    if descriptor.get("framework") not in {"koishi", "nonebot", "astrbot"}:
        raise ScenarioGenerationError("unsupported command framework")
    command_id = descriptor.get("command_id")
    invocation = descriptor.get("invocation")
    if not isinstance(command_id, str) or not command_id or not isinstance(invocation, str) or not invocation:
        raise ScenarioGenerationError("descriptor requires non-empty command_id and invocation")
    if not isinstance(host_id, str) or not host_id.strip():
        raise ScenarioGenerationError("host_id must be non-empty")
    descriptor_hash = object_sha256(descriptor)
    rng = random.Random(int(hashlib.sha256(f"{inventory_sha256}:{descriptor_hash}:{seed}".encode()).hexdigest(), 16))
    factors = _descriptor_factors(descriptor, rng)
    has_required = any(parameter.get("required", False) for parameter in descriptor.get("arguments", []))
    aliases = [item for item in descriptor.get("aliases", []) if isinstance(item, str) and item.strip()]
    has_typed_parameters = factors[0].role != "raw_text"
    reserve = min(max_scenarios - 1, int(has_required) + int(has_typed_parameters) + int(bool(aliases)))
    pairwise = pairwise_cases(factors, max_rows=max(1, max_scenarios - reserve), seed=seed)
    pairwise_summary = {
        "complete": pairwise.complete,
        "covered_pairs": pairwise.covered_pairs,
        "total_pairs": pairwise.total_pairs,
    }

    cases: list[tuple[str, dict[str, Any], str | None]] = []
    for row in pairwise.rows:
        kind = "raw_text" if factors and factors[0].role == "raw_text" else "pairwise"
        cases.append((kind, row, None))
    if has_required and len(cases) < max_scenarios:
        omitted = next(
            parameter["name"]
            for parameter in descriptor.get("arguments", [])
            if parameter.get("required", False)
        )
        base = dict(cases[0][1]) if cases else {}
        base["_omitted_required"] = [omitted]
        cases.append(("missing_required", base, None))
    if has_typed_parameters and len(cases) < max_scenarios:
        extra = dict(cases[0][1]) if cases else {}
        extra["_extra_argument"] = "ligamina-extra-argument"
        cases.append(("extra_argument", extra, None))
    if aliases and len(cases) < max_scenarios:
        alias = aliases[seed % len(aliases)]
        alias_case = dict(cases[0][1]) if cases else {}
        cases.append(("alias", alias_case, alias))
    cases = cases[:max_scenarios]

    actor_digest = hashlib.sha256(f"{inventory_sha256}:{host_id}:{seed}".encode()).hexdigest()[:16]
    actor_id = f"actor-{actor_digest}"
    conversation_id = f"private-{actor_digest}"
    account_id = "ligamina-test-account"
    scenarios = []
    for index, (kind, inputs, alias) in enumerate(cases):
        raw_arguments = _render_arguments(descriptor, inputs)
        source = {
            "inventory": inventory_sha256,
            "descriptor": descriptor_hash,
            "version": GENERATOR_VERSION,
            "seed": seed,
            "index": index,
            "kind": kind,
            "inputs": inputs,
            "alias": alias,
        }
        scenario_id = f"scn-{object_sha256(source)[:24]}"
        if kind == "alias" and alias:
            step = _event_step(
                scenario_id=scenario_id,
                host_id=host_id,
                actor_id=actor_id,
                conversation_id=conversation_id,
                account_id=account_id,
                invocation=alias,
                raw_arguments=raw_arguments,
                fixed_time_utc=fixed_time_utc,
            )
        else:
            step = {
                "step_id": "step-1",
                "kind": "tool_call",
                "host_id": host_id,
                "actor_id": actor_id,
                "conversation_id": conversation_id,
                "command_id": command_id,
                "invocation": invocation,
                "arguments": raw_arguments,
                "inputs": inputs,
            }
        scenarios.append({
            "schema_version": 1,
            "scenario_id": scenario_id,
            "source_inventory_sha256": inventory_sha256,
            "command_descriptor_sha256": descriptor_hash,
            "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION, "seed": seed},
            "relation": {"kind": "differential", "id": "native-vs-federated"},
            "actors": [{"actor_id": actor_id, "account_id": account_id}],
            "conversations": [{
                "conversation_id": conversation_id,
                "kind": "private",
                "actor_id": actor_id,
                "account_id": account_id,
            }],
            "steps": [step],
            "controls": {
                "random_seed": seed,
                "timezone": "UTC",
                "fixed_time_utc": fixed_time_utc,
                "network": "offline",
            },
            "normalization": {"volatile_fields": list(DEFAULT_VOLATILE_FIELDS)},
            "case": {"kind": kind, "index": index, "pairwise": pairwise_summary},
        })
    return ScenarioBatch(scenarios, pairwise_summary, inventory_sha256, descriptor_hash, seed)


def shrink_failing_example(strategy, predicate: Callable[[Any], bool], *, seed: int = 0, max_examples: int = 1000) -> Any:
    """Find a minimal failing value with deterministic Hypothesis shrinking."""
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ScenarioGenerationError("shrink seed must be a non-negative integer")
    deterministic = settings(
        database=None,
        derandomize=True,
        max_examples=max_examples,
        deadline=None,
    )
    return find(strategy, predicate, settings=deterministic, random=random.Random(seed))


def normalize_observation(value: Any, *, volatile_fields: Iterable[str] = DEFAULT_VOLATILE_FIELDS) -> Any:
    """Normalize known volatile fields and embedded UUID/timestamp tokens."""
    volatile = {field.casefold() for field in volatile_fields}

    def visit(current: Any, key: str | None = None) -> Any:
        if key is not None and key.casefold() in volatile:
            return f"<volatile:{key.casefold()}>"
        if isinstance(current, dict):
            return {str(child_key): visit(child, str(child_key)) for child_key, child in sorted(current.items(), key=lambda item: str(item[0]))}
        if isinstance(current, list):
            return [visit(child) for child in current]
        if isinstance(current, str):
            result = UUID_RE.sub("<volatile:uuid>", current)
            return TIMESTAMP_RE.sub("<volatile:timestamp>", result)
        return current

    return visit(value)


def compare_repeated_observations(first: Any, second: Any, *, volatile_fields: Iterable[str] = DEFAULT_VOLATILE_FIELDS) -> dict[str, Any]:
    """Classify a Native self-differential comparison without treating all-volatile output as pass."""
    normalized_first = normalize_observation(first, volatile_fields=volatile_fields)
    normalized_second = normalize_observation(second, volatile_fields=volatile_fields)
    if not _has_stable_observation(normalized_first) and not _has_stable_observation(normalized_second):
        return {
            "status": "unknown",
            "reason": "nondeterministic",
            "normalized_first": normalized_first,
            "normalized_second": normalized_second,
        }
    status = "stable" if normalized_first == normalized_second else "nondeterministic"
    return {
        "status": status,
        "reason": None if status == "stable" else "native_repetitions_differ",
        "normalized_first": normalized_first,
        "normalized_second": normalized_second,
    }


def load_command_descriptor(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioGenerationError(f"cannot load command descriptor {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScenarioGenerationError("command descriptor must be a JSON object")
    return value


def _descriptor_factors(descriptor: dict[str, Any], rng: random.Random) -> list[_Factor]:
    arguments = descriptor.get("arguments", [])
    options = descriptor.get("options", [])
    if not isinstance(arguments, list) or not isinstance(options, list):
        raise ScenarioGenerationError("descriptor arguments and options must be arrays")
    factors: list[_Factor] = []
    for index, argument in enumerate(arguments):
        if not isinstance(argument, dict) or not isinstance(argument.get("name"), str):
            raise ScenarioGenerationError(f"invalid argument descriptor at index {index}")
        domain = type_domain(argument)
        if argument.get("required") is not True:
            domain = [None, *domain]
        factors.append(_Factor(f"arg:{argument['name']}", argument, _rotate(domain, rng), "argument"))
    for index, option in enumerate(options):
        if not isinstance(option, dict) or not isinstance(option.get("name"), str):
            raise ScenarioGenerationError(f"invalid option descriptor at index {index}")
        domain = type_domain(option)
        if str(option.get("type", "")).casefold() in {"boolean", "bool", "flag"}:
            domain = [None, False, True]
        else:
            domain = [None, *domain]
        factors.append(_Factor(f"opt:{option['name']}", option, _rotate(_unique_json(domain), rng), "option"))
    if not factors:
        raw_examples = _raw_text_domain(descriptor)
        factors.append(_Factor("raw_text", {"type": "text"}, _rotate(raw_examples, rng), "raw_text"))
    return factors


def _rotate(values: list[Any], rng: random.Random) -> list[Any]:
    if len(values) <= 1:
        return values
    offset = rng.randrange(len(values))
    return values[offset:] + values[:offset]


def _raw_text_domain(descriptor: dict[str, Any]) -> list[str]:
    example_source = " ".join(str(descriptor.get(key, "")) for key in ("usage", "description"))
    examples = [match.group(1) or match.group(2) or match.group(3) for match in EXAMPLE_RE.finditer(example_source)]
    tokens = re.findall(r"(?<![<\[])[\w./-]{2,32}(?![>\]])", str(descriptor.get("usage", "")))
    return _unique_json([*examples, *tokens, "hello", "hello world", " leading", "trailing ", "中文🙂", 'quote " slash \\', "x" * 2048, ""])


def _description_examples(parameter: dict[str, Any]) -> list[Any]:
    values = []
    for key in ("example", "examples", "description", "usage", "syntax"):
        value = parameter.get(key)
        if isinstance(value, str):
            values.extend(match.group(1) or match.group(2) or match.group(3) for match in EXAMPLE_RE.finditer(value))
        elif isinstance(value, list):
            values.extend(value[:16])
    return values


def _unique_json(values: Iterable[Any]) -> list[Any]:
    result = []
    seen: set[bytes] = set()
    for value in values:
        try:
            encoded = canonical_json_bytes(value)
        except (TypeError, ValueError):
            continue
        if encoded not in seen:
            seen.add(encoded)
            result.append(value)
    return result


def _render_arguments(descriptor: dict[str, Any], inputs: dict[str, Any]) -> str:
    tokens = []
    omitted = set(inputs.get("_omitted_required", []))
    for argument in descriptor.get("arguments", []):
        name = argument["name"]
        if name in omitted:
            continue
        value = inputs.get(f"arg:{name}")
        if value is None:
            continue
        tokens.append(_render_token(value))
    for option in descriptor.get("options", []):
        name = option["name"]
        value = inputs.get(f"opt:{name}")
        if value is None or value is False:
            continue
        token = _option_token(option)
        if value is True:
            tokens.append(token)
        else:
            tokens.extend([token, _render_token(value)])
    if "raw_text" in inputs:
        tokens.insert(0, str(inputs["raw_text"]))
    if "_extra_argument" in inputs:
        tokens.append(str(inputs["_extra_argument"]))
    return " ".join(tokens)[:MAX_COMMAND_ARGUMENTS_BYTES]


def _render_token(value: Any) -> str:
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(value, bool):
        raw = "true" if value else "false"
    else:
        raw = str(value)
    return shlex.quote(raw)


def _option_token(option: dict[str, Any]) -> str:
    syntax = option.get("syntax")
    if isinstance(syntax, str):
        match = re.search(r"--?[\w-]+", syntax)
        if match:
            return match.group(0)
    name = str(option["name"])
    return name if name.startswith("-") else f"--{name}"


def _event_step(
    *, scenario_id: str, host_id: str, actor_id: str, conversation_id: str,
    account_id: str, invocation: str, raw_arguments: str, fixed_time_utc: str,
) -> dict[str, Any]:
    message = " ".join(item for item in (invocation, raw_arguments) if item)
    return {
        "step_id": "step-1",
        "kind": "event",
        "host_id": host_id,
        "actor_id": actor_id,
        "conversation_id": conversation_id,
        "event": {
            "schema_version": "ligamina/0.1",
            "event_id": f"event-{scenario_id[4:]}",
            "trace_id": f"trace-{scenario_id[4:]}",
            "platform": "onebot.v11",
            "account_id": account_id,
            "conversation": {"kind": "private", "id": conversation_id},
            "actor_id": actor_id,
            "timestamp": fixed_time_utc,
            "target_host": host_id,
            "message": [{"type": "text", "text": message}],
            "extensions": {"onebot.v11": {"message_id": f"message-{scenario_id[4:]}"}},
        },
    }


def _has_stable_observation(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_stable_observation(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_stable_observation(child) for child in value)
    if isinstance(value, str):
        return value not in {"", "<volatile:timestamp>", "<volatile:uuid>"} and not value.startswith("<volatile:")
    return value is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-sha256", required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite existing scenario output: {args.output}")
    batch = generate_scenarios(
        inventory_sha256=args.inventory_sha256,
        descriptor=load_command_descriptor(args.descriptor),
        host_id=args.host_id,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(batch.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "scenario_count": len(batch.scenarios), "pairwise": batch.pairwise}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
