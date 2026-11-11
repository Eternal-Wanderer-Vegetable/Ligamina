from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import argparse
import tarfile
from typing import Any
import zipfile
from pathlib import Path


_UNKNOWN_STATUSES = {"unknown", "nondeterministic"}


def collect_nontrivial_unknown_candidates(
    pilot: dict[str, Any], selection: dict[str, Any]
) -> list[dict[str, Any]]:
    selected = {
        (
            sample.get("ecosystem"),
            sample.get("entry", {}).get("registry_entry_id"),
            sample.get("entry", {}).get("package"),
            sample.get("entry", {}).get("version"),
            sample.get("entry", {}).get("artifact_sha256"),
        )
        for sample in selection.get("samples", [])
    }
    candidates: list[dict[str, Any]] = []
    for entry in pilot.get("entries", []):
        identity = (
            entry.get("ecosystem"),
            entry.get("registry_entry_id"),
            entry.get("package"),
            entry.get("version"),
            entry.get("artifact_sha256"),
        )
        rows = entry.get("commands", {}).get("items", [])
        row_by_id = {row.get("command_id"): row for row in rows}
        command_ids = [
            row.get("command_id")
            for row in rows
            if row.get("status") in _UNKNOWN_STATUSES
            and row.get("nontrivial") == "not_assessed"
            and row.get("scenarios", 0) > 0
        ]
        command_id_set = set(command_ids)
        declared_count = entry.get("commands", {}).get("nontrivial_unknown", 0)
        if len(command_ids) != declared_count:
            raise ValueError(
                f"{entry.get('registry_entry_id')} nontrivial_unknown count differs "
                f"from command rows: {declared_count} != {len(command_ids)}"
            )
        if command_ids and identity not in selected:
            raise ValueError(
                f"{entry.get('registry_entry_id')} source audit entry is not bound to selection"
            )
        discovered = set(entry.get("native_discovery", {}).get("runtime_command_ids", []))
        if not command_id_set.issubset(discovered):
            raise ValueError(
                f"{entry.get('registry_entry_id')} nontrivial_unknown command is absent from native discovery"
            )
        reason_counts: dict[str, Counter[str]] = {
            command_id: Counter() for command_id in command_ids
        }
        for round_result in entry.get("rounds", []):
            for scenario in round_result.get("scenario_results", []):
                command_id = scenario.get("command_id")
                if command_id in reason_counts:
                    reason_counts[command_id][scenario.get("reason_code", "unspecified")] += 1
        for command_id in command_ids:
            row = row_by_id[command_id]
            candidates.append(
                {
                    "registry_entry_id": entry["registry_entry_id"],
                    "ecosystem": entry["ecosystem"],
                    "package": entry["package"],
                    "version": entry["version"],
                    "artifact_sha256": entry["artifact_sha256"],
                    "command_id": command_id,
                    "status": row["status"],
                    "descriptor_sha256": row["descriptor_sha256"],
                    "scenarios": row["scenarios"],
                    "reason_counts": dict(sorted(reason_counts[command_id].items())),
                }
            )
    return candidates


def validate_archive_source_reference(
    archive_path: Path, reference: dict[str, Any]
) -> dict[str, Any]:
    member_path = reference.get("path")
    expected_sha256 = reference.get("sha256")
    lines = reference.get("lines")
    if not isinstance(member_path, str) or not member_path:
        raise ValueError("source member path must be a non-empty string")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("source member SHA-256 must contain 64 hexadecimal characters")
    try:
        int(expected_sha256, 16)
    except ValueError as error:
        raise ValueError("source member SHA-256 must contain 64 hexadecimal characters") from error
    if (
        not isinstance(lines, list)
        or len(lines) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) for value in lines)
        or lines[0] < 1
        or lines[1] < lines[0]
    ):
        raise ValueError("source line range must be a positive inclusive [start, end] pair")

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            matches = [item for item in archive.infolist() if item.filename == member_path]
            if len(matches) != 1:
                raise ValueError(f"source member must exist exactly once: {member_path}")
            member_bytes = archive.read(matches[0])
    else:
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                matches = [
                    item for item in archive.getmembers()
                    if item.name == member_path and item.isfile()
                ]
                if len(matches) != 1:
                    raise ValueError(f"source member must exist exactly once: {member_path}")
                stream = archive.extractfile(matches[0])
                if stream is None:
                    raise ValueError(f"source member is not readable: {member_path}")
                member_bytes = stream.read()
        except tarfile.TarError as error:
            raise ValueError(f"archive is neither a readable ZIP nor TAR: {archive_path}") from error

    observed_sha256 = hashlib.sha256(member_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ValueError(f"source member SHA-256 changed: {member_path}")
    try:
        decoded = member_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"source member is not UTF-8 text: {member_path}") from error
    line_count = len(decoded.splitlines())
    if lines[1] > line_count:
        raise ValueError(
            f"source line range is outside {member_path}: {lines[0]}-{lines[1]} > {line_count}"
        )
    return {"member_sha256": observed_sha256, "line_count": line_count}


def build_source_audit_report(
    pilot_path: Path,
    selection_path: Path,
    facts_path: Path,
    archive_cache_root: Path,
    catalog_root: Path,
) -> dict[str, Any]:
    pilot_bytes = pilot_path.read_bytes()
    selection_bytes = selection_path.read_bytes()
    facts_bytes = facts_path.read_bytes()
    pilot = json.loads(pilot_bytes)
    selection = json.loads(selection_bytes)
    facts = json.loads(facts_bytes)
    if facts.get("schema_version") != "ligamina.p7.nontrivial-unknown-source-facts.v1":
        raise ValueError("unsupported source facts schema")
    if hashlib.sha256(pilot_bytes).hexdigest() != facts["pilot"]["sha256"]:
        raise ValueError("pinned pilot SHA-256 changed")
    if hashlib.sha256(selection_bytes).hexdigest() != facts["selection"]["sha256"]:
        raise ValueError("pinned selection SHA-256 changed")

    candidates = collect_nontrivial_unknown_candidates(pilot, selection)
    pilot_entries = {row.get("registry_entry_id"): row for row in pilot.get("entries", [])}
    candidate_by_key = {
        (candidate["registry_entry_id"], candidate["command_id"]): candidate
        for candidate in candidates
    }
    fact_commands = {
        (entry["registry_entry_id"], command["command_id"]): (entry, command)
        for entry in facts.get("entries", [])
        for command in entry.get("commands", [])
    }
    if candidate_by_key.keys() != fact_commands.keys():
        missing = sorted(candidate_by_key.keys() - fact_commands.keys())
        extra = sorted(fact_commands.keys() - candidate_by_key.keys())
        raise ValueError(f"source fact command set differs from pilot: missing={missing}, extra={extra}")

    gate = facts["cohort_gate"]
    counts = pilot.get("summary", {})
    total_commands = counts.get("commands_discovered")
    nontrivial_commands = counts.get("commands_nontrivial")
    required_commands = math.ceil(total_commands * gate["required_nontrivial_percent"] / 100)
    if (
        total_commands != gate["reported_commands"]
        or nontrivial_commands != gate["reported_nontrivial"]
        or required_commands != gate["required_commands"]
    ):
        raise ValueError("cohort nontrivial gate differs from pinned source-audit facts")

    lock_path = catalog_root / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_entries = {row["registry_entry_id"]: row for row in lock.get("entries", [])}
    report_entries: list[dict[str, Any]] = []
    classification_counts: Counter[str] = Counter()
    for entry_facts in facts["entries"]:
        entry_id = entry_facts["registry_entry_id"]
        pilot_entry = pilot_entries.get(entry_id)
        if pilot_entry is None:
            raise ValueError(f"{entry_id} is absent from the pinned pilot")
        candidate_entry = next(
            row for row in candidates if row["registry_entry_id"] == entry_id
        )
        for field in ("ecosystem", "package", "version", "artifact_sha256"):
            if candidate_entry[field] != entry_facts[field]:
                raise ValueError(f"{entry_id} {field} differs from source facts")
        lock_entry = lock_entries.get(entry_id)
        if lock_entry is None or lock_entry.get("package") != entry_facts["package"]:
            raise ValueError(f"{entry_id} is absent from the locked artifact catalog")
        platform = "all" if entry_facts["ecosystem"] == "koishi" else "linux-x86_64"
        artifact_records = [
            row["artifact"]
            for row in lock_entry.get("artifacts", [])
            if row.get("platform") == platform
        ]
        if len(artifact_records) != 1:
            raise ValueError(f"{entry_id} has no unique {platform} locked artifact")
        artifact_path = archive_cache_root / artifact_records[0]["cache_path"]
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if artifact_sha256 != entry_facts["artifact_sha256"]:
            raise ValueError(f"{entry_id} artifact SHA-256 changed")

        report_commands: list[dict[str, Any]] = []
        for command_facts in entry_facts["commands"]:
            key = (entry_id, command_facts["command_id"])
            candidate = candidate_by_key[key]
            validated_refs = []
            for source_ref in command_facts["source_refs"]:
                validation = validate_archive_source_reference(artifact_path, source_ref)
                validated_refs.append(
                    {
                        "path": source_ref["path"],
                        "sha256": validation["member_sha256"],
                        "lines": source_ref["lines"],
                        "member_line_count": validation["line_count"],
                    }
                )
            classification = command_facts["candidate_classification"]
            classification_counts[classification] += 1
            report_commands.append(
                {
                    "command_id": candidate["command_id"],
                    "status": candidate["status"],
                    "descriptor_sha256": candidate["descriptor_sha256"],
                    "scenarios": candidate["scenarios"],
                    "reason_counts": candidate["reason_counts"],
                    "source_conclusion": command_facts["source_conclusion"],
                    "candidate_classification": classification,
                    "candidate_prerequisites": command_facts["candidate_prerequisites"],
                    "source_refs": validated_refs,
                }
            )
        execution_rounds = []
        for round_data in pilot_entry.get("rounds", []):
            sides = {}
            for side_name, side_data in round_data.get("fresh_workers", {}).items():
                side = {
                    "status": side_data.get("status"),
                    "profile_setup_status": side_data.get("profile_setup_status"),
                    "gateway_operation_count": side_data.get("gateway_operation_count"),
                    "cleanup_confirmed": side_data.get("outer_worker", {}).get("cleanup_confirmed"),
                }
                diagnostic = side_data.get("diagnostic_summary")
                if side_data.get("status") != "completed" and diagnostic:
                    side["diagnostic_summary"] = str(diagnostic)[:1200]
                sides[side_name] = side
            execution_rounds.append(
                {
                    "round": round_data.get("round"),
                    "cleanup_confirmed": round_data.get("cleanup_confirmed"),
                    "federated_host_reaped": round_data.get("federated_host_reaped"),
                    "sides": sides,
                }
            )
        report_entries.append(
            {
                "registry_entry_id": entry_id,
                "ecosystem": entry_facts["ecosystem"],
                "package": entry_facts["package"],
                "version": entry_facts["version"],
                "artifact_sha256": artifact_sha256,
                "execution_rounds": execution_rounds,
                "commands": report_commands,
            }
        )

    gap = required_commands - nontrivial_commands
    return {
        "kind": "p7_nontrivial_unknown_source_audit",
        "schema_version": "ligamina.p7.nontrivial-unknown-source-audit.v1",
        "audit_scope": "read_only_exact_artifact_source_reachability",
        "evidence_bindings": {
            "pilot_path": facts["pilot"]["path"],
            "pilot_sha256": hashlib.sha256(pilot_bytes).hexdigest(),
            "selection_path": facts["selection"]["path"],
            "selection_sha256": hashlib.sha256(selection_bytes).hexdigest(),
            "source_facts_path": facts["path"],
            "source_facts_sha256": hashlib.sha256(facts_bytes).hexdigest(),
            "artifact_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        },
        "cohort_gate": {
            "required_nontrivial_percent": gate["required_nontrivial_percent"],
            "required_commands": required_commands,
            "reported_nontrivial": nontrivial_commands,
            "reported_commands": total_commands,
            "gap": max(0, gap),
            "p7_09_remains_blocked": gap > 0,
        },
        "audit_summary": {
            "entries": len(report_entries),
            "commands": len(candidates),
            "candidate_classifications": dict(sorted(classification_counts.items())),
            "offline_input_sensitive_candidates": sum(
                count
                for classification, count in classification_counts.items()
                if classification.startswith("offline_input_sensitive_candidate")
            ),
        },
        "execution": {
            "worker_dispatches": 0,
            "plugin_archive_mutations": 0,
            "external_network_requests": 0,
            "behavior_status_changed": False,
            "denominator_changed": False,
        },
        "entries": report_entries,
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT

    repository_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Build/check a read-only P7 source reachability audit.")
    parser.add_argument("--pilot", type=Path, default=repository_root / "outputs/acceptance/p7-p704-differential-pilot-20261111-feat061-source-context-rerun2.json")
    parser.add_argument("--selection", type=Path, default=repository_root / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json")
    parser.add_argument("--facts", type=Path, default=repository_root / "outputs/acceptance/p7-p704-nontrivial-unknown-source-facts-20261112.json")
    parser.add_argument("--output", type=Path, default=repository_root / "outputs/acceptance/p7-p704-nontrivial-unknown-source-audit-20261112.json")
    parser.add_argument("--archive-cache-root", type=Path, default=ARTIFACT_CACHE)
    parser.add_argument("--catalog-root", type=Path, default=CATALOG_ROOT)
    parser.add_argument("--check", action="store_true", help="verify existing report and SHA-256 sidecar without writing")
    args = parser.parse_args()
    report = build_source_audit_report(
        args.pilot, args.selection, args.facts, args.archive_cache_root, args.catalog_root
    )
    report_bytes = _json_bytes(report)
    digest = hashlib.sha256(report_bytes).hexdigest()
    sidecar = args.output.with_suffix(args.output.suffix + ".sha256")
    sidecar_bytes = f"{digest}  {args.output.name}\n".encode("ascii")
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != report_bytes:
            raise SystemExit("source audit report differs from its reproducible inputs")
        if not sidecar.exists() or sidecar.read_bytes() != sidecar_bytes:
            raise SystemExit("source audit report SHA-256 sidecar is missing or changed")
        action = "checked"
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(report_bytes)
        sidecar.write_bytes(sidecar_bytes)
        action = "written"
    print(
        json.dumps(
            {
                "action": action,
                "output": str(args.output),
                "sha256": digest,
                "entries": report["audit_summary"]["entries"],
                "commands": report["audit_summary"]["commands"],
                "nontrivial_gap": report["cohort_gate"]["gap"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
