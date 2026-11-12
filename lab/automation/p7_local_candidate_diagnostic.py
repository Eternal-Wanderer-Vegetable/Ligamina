"""Build and validate the exact two-entry P7 local-candidate diagnostic."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json"
SOURCE_AUDIT_PATH = ROOT / "outputs/acceptance/p7-p704-nontrivial-unknown-source-audit-20261112.json"
STIMULUS_PROFILE_PATH = ROOT / "lab/automation/data/p7-local-candidate-stimuli-v1.json"
CONTEXT_PROFILE_PATH = ROOT / "lab/automation/data/p7-nonebot-source-bound-context-v1.json"
EXPECTED_IDS = {"ko-919bd13715bb892a7a8675e3", "nb-250c92c282d77dcf502b9dbf"}
DEFAULT_SELECTION_OUTPUT = ROOT / "outputs/acceptance/p7-p704-local-candidate-selection-20261112.json"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _hash_bound_json(reference: dict[str, Any], *, label: str) -> tuple[Path, dict[str, Any]]:
    path = (ROOT / reference["path"]).resolve(strict=True)
    raw = path.read_bytes()
    if _sha256(raw) != reference.get("sha256"):
        raise ValueError(f"{label} SHA-256 changed")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return path, value


def _validate_locked_member(candidate: dict[str, Any]) -> None:
    lock = _read_json(CATALOG_ROOT / "artifacts.lock.json")
    row = next(
        (item for item in lock.get("entries", []) if item.get("registry_entry_id") == candidate["registry_entry_id"]),
        None,
    )
    platform = "all" if candidate["ecosystem"] == "koishi" else "linux-x86_64"
    artifact = next(
        (
            item.get("artifact")
            for item in (row or {}).get("artifacts", [])
            if item.get("platform") == platform and isinstance(item.get("artifact"), dict)
        ),
        None,
    )
    if not isinstance(artifact, dict) or artifact.get("computed_digests", {}).get("sha256") != candidate["artifact_sha256"]:
        raise ValueError(f"local-candidate artifact is not bound by its frozen lock: {candidate['registry_entry_id']}")
    archive = (ARTIFACT_CACHE / artifact["cache_path"]).resolve(strict=True)
    expected_member = candidate["source_member"]
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            raw = source.read(expected_member["path"])
    else:
        with tarfile.open(archive) as source:
            matches = [name for name in source.getnames() if name.endswith(expected_member["path"])]
            if len(matches) != 1:
                raise ValueError(f"local-candidate source member is missing or ambiguous: {candidate['registry_entry_id']}")
            stream = source.extractfile(matches[0])
            if stream is None:
                raise ValueError(f"local-candidate source member is not a file: {candidate['registry_entry_id']}")
            raw = stream.read()
    if _sha256(raw) != expected_member["sha256"]:
        raise ValueError(f"local-candidate source member hash changed: {candidate['registry_entry_id']}")


def validate_stimulus_profile(document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed unless the input cycles remain tied to the exact source and profiles."""
    profile = copy.deepcopy(document) if document is not None else _read_json(STIMULUS_PROFILE_PATH)
    if profile.get("schema_version") != 1 or profile.get("kind") != "p7_source_bound_local_candidate_stimuli":
        raise ValueError("unexpected P7 local-candidate stimulus profile kind")
    if profile.get("execution_scope") != "admitted_linux_worker_network_disabled_disposable_config":
        raise ValueError("local-candidate stimulus profile has an unsupported execution scope")
    selection_path, selection = _hash_bound_json(profile["source_selection"], label="source selection")
    audit_path, audit = _hash_bound_json(profile["source_audit"], label="source audit")
    context_path, _ = _hash_bound_json(profile["context_profile"], label="context profile")
    if selection_path != SOURCE_SELECTION_PATH.resolve() or audit_path != SOURCE_AUDIT_PATH.resolve():
        raise ValueError("local-candidate stimulus profile is bound to an unexpected source report")
    if context_path != CONTEXT_PROFILE_PATH.resolve():
        raise ValueError("local-candidate stimulus profile is bound to an unexpected context manifest")
    if audit.get("kind") != "p7_nontrivial_unknown_source_audit":
        raise ValueError("local-candidate stimulus profile source audit has an unexpected kind")
    evidence = audit.get("evidence_bindings", {})
    if evidence.get("selection_path") != profile["source_selection"]["path"] or evidence.get("selection_sha256") != profile["source_selection"]["sha256"]:
        raise ValueError("source audit and local-candidate selection binding differ")

    selected = {
        row["entry"]["registry_entry_id"]: row
        for row in selection.get("samples", [])
        if isinstance(row, dict) and isinstance(row.get("entry"), dict)
    }
    audit_entries = {
        row.get("registry_entry_id"): row
        for row in audit.get("entries", [])
        if isinstance(row, dict)
    }
    if len(profile.get("candidates", [])) != 2:
        raise ValueError("local-candidate stimulus profile must define exactly two candidates")
    if {row.get("registry_entry_id") for row in profile["candidates"]} != EXPECTED_IDS:
        raise ValueError("local-candidate stimulus profile candidate set changed")
    for candidate in profile["candidates"]:
        entry_id = candidate["registry_entry_id"]
        sample = selected.get(entry_id)
        audit_entry = audit_entries.get(entry_id)
        if not isinstance(sample, dict) or not isinstance(audit_entry, dict):
            raise ValueError(f"local-candidate source is missing an exact entry: {entry_id}")
        identity = sample["entry"]
        for key in ("ecosystem", "package", "version", "artifact_sha256"):
            if candidate.get(key) != (sample.get("ecosystem") if key == "ecosystem" else identity.get(key)):
                raise ValueError(f"local-candidate identity differs from frozen selection: {entry_id}:{key}")
            if candidate.get(key) != (audit_entry.get(key)):
                raise ValueError(f"local-candidate identity differs from source audit: {entry_id}:{key}")
        if candidate.get("ecosystem") != audit_entry.get("ecosystem"):
            raise ValueError(f"local-candidate ecosystem differs from source audit: {entry_id}")
        audit_command = next(
            (row for row in audit_entry.get("commands", []) if row.get("command_id") == candidate.get("command_id")),
            None,
        )
        if not isinstance(audit_command, dict) or audit_command.get("status") != "unknown":
            raise ValueError(f"local-candidate command is not an unknown source-audit row: {entry_id}")
        member = candidate.get("source_member", {})
        matching_refs = [
            source
            for source in audit_command.get("source_refs", [])
            if source.get("path") == member.get("path") and source.get("sha256") == member.get("sha256")
        ]
        audited_lines = {line for source in matching_refs for line in source.get("lines", [])}
        if not matching_refs or not set(member.get("lines", [])).issubset(audited_lines):
            raise ValueError(f"local-candidate source member differs from the audit: {entry_id}")
        static_names = {
            row.get("name")
            for row in sample.get("static_inventory", {}).get("capabilities", {}).get("commands", {}).get("items", [])
            if isinstance(row, dict)
        }
        normalized = candidate["command_id"].lstrip(".")
        if not any(name == normalized or str(name).split(" ", 1)[0].lstrip(".") == normalized for name in static_names):
            raise ValueError(f"local-candidate command is absent from static inventory: {entry_id}:{normalized}")
        _validate_locked_member(candidate)
        cycle = candidate.get("input_cycle")
        if not isinstance(cycle, list) or len(cycle) != 2 or any(not isinstance(value, str) or not value.strip() for value in cycle):
            raise ValueError(f"local-candidate input cycle must contain two explicit strings: {entry_id}")
        if entry_id == "nb-250c92c282d77dcf502b9dbf" and (
            candidate.get("context_command_id") != ".unsub"
            or candidate.get("worker_config_scope") != "fresh_docker_worker_localstore_config"
        ):
            raise ValueError("BiliChat .unsub requires its source-bound SUPERUSER profile and disposable Worker config")
    return profile


def stimulus_profile_sha256() -> str:
    validate_stimulus_profile()
    return _sha256(STIMULUS_PROFILE_PATH.read_bytes())


def stimulus_for_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    profile = validate_stimulus_profile()
    row = next(
        (item for item in profile["candidates"] if item["registry_entry_id"] == candidate.get("registry_entry_id")),
        None,
    )
    if row is None:
        return None
    identity = (candidate.get("ecosystem"), candidate.get("package"), candidate.get("version"), candidate.get("artifact_sha256"))
    expected = (row["ecosystem"], row["package"], row["version"], row["artifact_sha256"])
    if identity != expected:
        raise ValueError(f"local-candidate stimulus identity mismatch: {row['registry_entry_id']}")
    return copy.deepcopy(row)


def validate_selection_document(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the derived 1+1 selection against the source selection and stimulus profile."""
    profile = validate_stimulus_profile()
    if document.get("kind") != "p7_p701_static_inventory_sample":
        raise ValueError("local-candidate selection is not derived from the P7-01 frozen sample")
    scope = document.get("diagnostic_scope", {})
    if (
        scope.get("kind") != "p7_source_bound_local_candidate_diagnostic"
        or scope.get("stimulus_profile_sha256") != _sha256(STIMULUS_PROFILE_PATH.read_bytes())
        or scope.get("stimulus_profile_path") != STIMULUS_PROFILE_PATH.relative_to(ROOT).as_posix()
    ):
        raise ValueError("local-candidate selection is not bound to the validated stimulus profile")
    source = document.get("source_selection", {})
    if source != profile["source_selection"]:
        raise ValueError("local-candidate selection source selection binding differs")
    ids = {
        row.get("entry", {}).get("registry_entry_id")
        for row in document.get("samples", [])
        if isinstance(row, dict)
    }
    counts = document.get("selection", {}).get("counts")
    if ids != EXPECTED_IDS or counts != {"koishi": 1, "nonebot": 1}:
        raise ValueError("local-candidate selection must contain exactly the two source-bound entries")
    _, source_selection = _hash_bound_json(profile["source_selection"], label="source selection")
    expected_samples = {
        row["entry"]["registry_entry_id"]: row
        for row in source_selection.get("samples", [])
        if isinstance(row, dict)
        and isinstance(row.get("entry"), dict)
        and row["entry"].get("registry_entry_id") in EXPECTED_IDS
    }
    observed_samples = {
        row["entry"]["registry_entry_id"]: row
        for row in document.get("samples", [])
        if isinstance(row, dict) and isinstance(row.get("entry"), dict)
    }
    if observed_samples != expected_samples:
        raise ValueError("local-candidate selection sample identities differ from the frozen source selection")
    return profile


def build_selection_document() -> dict[str, Any]:
    """Derive an exact 1+1 diagnostic selection without modifying the frozen cohort."""
    profile = validate_stimulus_profile()
    _, source_selection = _hash_bound_json(profile["source_selection"], label="source selection")
    samples = [
        copy.deepcopy(row)
        for row in source_selection["samples"]
        if row.get("entry", {}).get("registry_entry_id") in EXPECTED_IDS
    ]
    if len(samples) != len(EXPECTED_IDS):
        raise ValueError("source selection does not contain exactly one sample for each local candidate")
    document = copy.deepcopy(source_selection)
    document["samples"] = samples
    document["selection"]["per_ecosystem_target"] = 1
    document["selection"]["counts"] = {"koishi": 1, "nonebot": 1}
    document["selection"]["rule"] = "Exact source-bound local candidates filtered from the frozen hash-ranked P7-04 sample; not a cohort resample."
    document["source_selection"] = copy.deepcopy(profile["source_selection"])
    document["stimulus_profile"] = {
        "path": STIMULUS_PROFILE_PATH.relative_to(ROOT).as_posix(),
        "sha256": _sha256(STIMULUS_PROFILE_PATH.read_bytes()),
    }
    document["diagnostic_scope"] = {
        "kind": "p7_source_bound_local_candidate_diagnostic",
        "stimulus_profile_path": document["stimulus_profile"]["path"],
        "stimulus_profile_sha256": document["stimulus_profile"]["sha256"],
        "source_audit_path": profile["source_audit"]["path"],
        "source_audit_sha256": profile["source_audit"]["sha256"],
    }
    validate_selection_document(document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_SELECTION_OUTPUT)
    options = parser.parse_args(argv)
    output = options.output.expanduser().absolute()
    if output.exists():
        parser.error(f"refusing to overwrite an existing local-candidate selection: {output}")
    document = build_selection_document()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.write_bytes(raw)
    print(json.dumps({
        "status": "created",
        "output": output.as_posix(),
        "sha256": _sha256(raw),
        "selected_registry_entry_ids": sorted(EXPECTED_IDS),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
