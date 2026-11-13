"""Create a read-only, hash-bound diagnosis of P7 no-operation observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PILOT = ROOT / "outputs/acceptance/p7-p704-differential-pilot-20261111-empty-input-v3.json"
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/p7-p704-no-operation-source-diagnostic-20261111.json"
DEFAULT_PILOT_SHA256 = "4962fc3080afdd6aa8dcd233f7ad28d51ba69fc78609260a377bce222db1557b"
EMPTY_OBSERVATION_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"

SOURCE_AUDITS: dict[str, dict[str, Any]] = {
    "nb-47f239e4b2ed71a255157441": {
        "package": "nonebot_plugin_antiinsult",
        "version": "0.7.1",
        "summary": (
            "Selected administrative commands are guarded by SUPERUSER. The P7 runner uses an empty NoneBot config, "
            "so this test actor is not configured as a superuser. This explains a missing test precondition, not each "
            "individual unknown result."
        ),
        "files": {
            "nonebot_plugin_antiinsult/__init__.py": ["permission=SUPERUSER"],
        },
    },
    "nb-ed10c5014456c4e265167187": {
        "package": "nonebot_plugin_antirecall",
        "version": "0.1.4",
        "summary": (
            "Selected administrative commands are guarded by SUPERUSER or SUPERUSER | GROUP_OWNER. P7 does not set a "
            "test superuser, and the EventEnvelope-to-OneBot mapping does not carry sender role."
        ),
        "files": {
            "nonebot_plugin_antirecall/__init__.py": ["permission = SUPERUSER", "SUPERUSER | GROUP_OWNER"],
        },
    },
    "nb-250c92c282d77dcf502b9dbf": {
        "package": "nonebot_plugin_bilichat",
        "version": "6.5.0",
        "summary": (
            "otp and otp_qrcode require SUPERUSER and return outside private conversations, while P7 emits group-only "
            "batches. The command group defaults command_to_me to true, but the scenario message is text-only and has "
            "no mention segment. These are source-bound missing-context candidates; the pilot has no per-event matcher trace."
        ),
        "files": {
            "nonebot_plugin_bilichat/command/otp.py": ["permission=SUPERUSER", "if not target.private"],
            "nonebot_plugin_bilichat/command/base.py": ["rule=to_me() if ConfigCTX.get().nonebot.command_to_me else None"],
            "nonebot_plugin_bilichat/model/config.py": ["command_to_me: bool = Field(", "default=True"],
        },
    },
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _references(path: Path, needles: list[str]) -> dict[str, Any]:
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    matches = []
    for needle in needles:
        line_numbers = [index + 1 for index, line in enumerate(lines) if needle in line]
        if not line_numbers:
            raise ValueError(f"expected source text not found: {path}:{needle}")
        matches.append({"text": needle, "line_numbers": line_numbers})
    return {"sha256": sha256(raw), "references": matches}


def _wheel_source(
    wheelhouse: Path, package: str, members: dict[str, list[str]], artifact_sha256: str
) -> dict[str, Any]:
    matches = list(wheelhouse.glob(f"*{package}*.whl"))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one wheel for {package}; found {len(matches)}")
    wheel = matches[0]
    wheel_sha = sha256(wheel.read_bytes())
    if wheel_sha != artifact_sha256:
        raise ValueError(f"wheel hash does not match pilot artifact identity for {package}")
    with zipfile.ZipFile(wheel) as archive:
        source_files = []
        for member, needles in members.items():
            raw = archive.read(member)
            lines = raw.decode("utf-8").splitlines()
            references = []
            for needle in needles:
                line_numbers = [i + 1 for i, line in enumerate(lines) if needle in line]
                if not line_numbers:
                    raise ValueError(f"expected wheel source text not found: {wheel.name}:{member}:{needle}")
                references.append({"text": needle, "line_numbers": line_numbers})
            source_files.append({
                "package_source": member,
                "package_source_sha256": sha256(raw),
                "references": references,
            })
    return {"wheel": wheel.name, "wheel_sha256": wheel_sha, "source_files": source_files}


def build_report(pilot_path: Path, bundle_root: Path, expected_pilot_sha256: str) -> dict[str, Any]:
    pilot_raw = pilot_path.read_bytes()
    pilot_sha = sha256(pilot_raw)
    if pilot_sha != expected_pilot_sha256:
        raise ValueError(f"pilot SHA mismatch: expected {expected_pilot_sha256}, got {pilot_sha}")
    pilot = json.loads(pilot_raw)

    bucket_names = (
        (0, 0, "empty_0_bytes"),
        (1, 127, "short_1_to_127_bytes"),
        (128, 2047, "medium_128_to_2047_bytes"),
        (2048, 2048, "synthetic_2048_byte_token"),
        (2049, None, "over_2048_bytes"),
    )
    bucket_counts = {name: Counter() for _, _, name in bucket_names}
    outcomes: Counter[str] = Counter()
    command_unknowns: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for entry in pilot["entries"]:
        scenario_by_id = {item["scenario_id"]: item for item in entry["scenarios"]}
        for round_result in entry["rounds"]:
            for result in round_result["scenario_results"]:
                scenario = scenario_by_id[result["scenario_id"]]
                byte_length = scenario["arguments_bytes"]
                bucket = next(
                    name for minimum, maximum, name in bucket_names
                    if byte_length >= minimum and (maximum is None or byte_length <= maximum)
                )
                reason = result["reason_code"]
                outcome = reason or result["status"]
                bucket_counts[bucket][outcome] += 1
                outcomes[outcome] += 1
                if reason == "no_native_gateway_operation_observed":
                    command = command_unknowns[entry["registry_entry_id"]][result["command_id"]]
                    command["scenario_rounds"] += 1
                    command[f"case_kind:{scenario['kind']}"] += 1
                    command[f"argument_bytes:{byte_length}"] += 1
                    if result["federated_observation_sha256"] != EMPTY_OBSERVATION_SHA256:
                        command["federated_nonempty_scenario_rounds"] += 1

    findings = []
    bundle_by_id = {item["registry_entry_id"]: item for item in pilot["bundle_preparation"]}
    for registry_id, audit in SOURCE_AUDITS.items():
        entry = next(item for item in pilot["entries"] if item["registry_entry_id"] == registry_id)
        bundle = bundle_by_id[registry_id]
        wheelhouse = bundle_root / registry_id / "project" / "sample" / "wheelhouse"
        wheel_source = _wheel_source(
            wheelhouse, audit["package"], audit["files"], entry["artifact_sha256"]
        )
        commands = []
        for command_id, counts in sorted(command_unknowns.get(registry_id, {}).items()):
            commands.append({
                "command_id": command_id,
                "no_native_operation_scenario_rounds": counts["scenario_rounds"],
                "case_kinds": sorted(key.removeprefix("case_kind:") for key in counts if key.startswith("case_kind:")),
                "argument_bytes": sorted(int(key.removeprefix("argument_bytes:")) for key in counts if key.startswith("argument_bytes:")),
                "federated_nonempty_scenario_rounds": counts["federated_nonempty_scenario_rounds"],
            })
        findings.append({
            "registry_entry_id": registry_id,
            "ecosystem": entry["ecosystem"],
            "package": audit["package"],
            "version": audit["version"],
            "artifact_sha256": entry["artifact_sha256"],
            "bundle_manifest_sha256": bundle["manifest_sha256"],
            "profile_sha256": bundle["context_files"]["profile_sha256"],
            "profile_lock_sha256": bundle["asset_summary"]["profile_lock_sha256"],
            "source_summary": audit["summary"],
            "commands_with_native_no_operation": commands,
            "wheel": wheel_source,
        })

    source_context = {}
    references = {
        "lab/automation/p7_differential_campaign.py": [
            '"nonebot_config": {}',
            'conversation_kind="group"',
            '"message": [{"type": "text", "text": message}]',
        ],
        "lab/baselines/nonebot/runner.py": [
            'nonebot.init(driver="~fastapi", nickname={"Bot"}',
        ],
        "lab/scenario_generator.py": [
            '"x" * 2048',
            'empty_input_budget = int(max_scenarios > 1)',
        ],
        "hosts/nonebot/ligamina_nonebot/adapter.py": [
            'sender = metadata.get("sender")',
            '"sender": {"user_id": actor_id, **({"nickname": sender_name} if sender_name else {})}',
        ],
        "contracts/v0.1/event-envelope.schema.json": [
            '"actor_id": { "$ref": "#/$defs/id" }',
            '"extensions": { "type": "object", "additionalProperties": true }',
        ],
    }
    for relative, needles in references.items():
        source_context[relative] = _references(ROOT / relative, needles)

    return {
        "schema_version": 1,
        "kind": "p7-p704-source-bound-no-operation-diagnostic",
        "inputs": {
            "pilot_path": pilot_path.resolve().relative_to(ROOT).as_posix(),
            "pilot_file_sha256": pilot_sha,
            "selection_path": pilot["scope"]["selection_path"],
            "selection_sha256": pilot["scope"]["selection_sha256"],
            "generator_version": pilot["scope"]["generator_version"],
            "bundle_root": str(bundle_root.resolve()),
            "pilot_status": pilot["status"],
        },
        "scope": {
            "entries": pilot["counts"]["entries"],
            "generated_scenarios": pilot["counts"]["scenarios"],
            "scenario_round_observations": sum(outcomes.values()),
            "worker_runs": pilot["counts"]["worker_runs"],
            "cleanup_unconfirmed": pilot["counts"]["cleanup_unconfirmed"],
            "unknown_command_count": pilot["summary"]["commands_unknown"],
            "results_by_observation_reason_or_status": dict(sorted(outcomes.items())),
            "results_by_argument_length_bytes": {
                name: dict(sorted(counter.items())) for name, counter in bucket_counts.items()
            },
        },
        "source_bound_findings": findings,
        "runner_context_source": source_context,
        "interpretation": [
            "The 2,048-byte raw_text value is the generator's synthetic x*2048 stress token. No-native-operation observations also occur for empty and 1-127 byte inputs, so the long token alone does not explain the no-operation cluster; length correlations are descriptive, not causal.",
            "Exact locked-source review confirms permission/context prerequisites for selected command clusters and confirms that the current scenario contract carries no OneBot sender role. The P7 report records operations, not matcher rule failures, response text, or permission denials; these source findings do not adjudicate individual unknown scenarios.",
            "For the selected permission/context-gated commands, Native and Federated observations are both empty in the pilot; no Federated-only operation was observed in these clusters.",
        ],
        "limitations": [
            "Read-only review of the already completed 30+30 pilot and exact cached wheel artifacts only. No third-party plugin was executed, no Worker/profile was changed, and no external network or service was used.",
            "This diagnostic explains representative command clusters only; it does not attribute all 1,941 no_native_gateway_operation_observed scenario-rounds.",
            "All pilot unknowns and the 65 not_assessed commands remain unchanged; this is not an acceptance or support claim.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-pilot-sha256", default=DEFAULT_PILOT_SHA256)
    parser.add_argument("--check", action="store_true", help="verify report and sidecar without writing")
    args = parser.parse_args()

    report = build_report(args.pilot, args.bundle_root, args.expected_pilot_sha256)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = sha256(raw)
    sidecar = args.output.with_suffix(args.output.suffix + ".sha256")
    sidecar_raw = f"{digest}  {args.output.name}\n".encode("ascii")
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != raw:
            raise SystemExit("diagnostic report does not match regenerated content")
        if not sidecar.is_file() or sidecar.read_bytes() != sidecar_raw:
            raise SystemExit("diagnostic report SHA-256 sidecar mismatch")
        print(f"P7 source diagnostic verified: {digest}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(raw)
        sidecar.write_bytes(sidecar_raw)
        print(f"P7 source diagnostic written: {args.output} SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
