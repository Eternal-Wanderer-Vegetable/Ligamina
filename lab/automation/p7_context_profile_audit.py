"""Verify context-profile source-audit rows against frozen selection and archives."""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT


ROOT = Path(__file__).resolve().parents[2]


def validate_source_audit_finding(finding: dict[str, Any], selection: dict[str, Any]) -> None:
    """Require report identity, command inventory, artifact bytes, and member hashes to agree."""
    entry_id = finding.get("registry_entry_id")
    sample = next(
        (row for row in selection.get("samples", [])
         if isinstance(row, dict) and row.get("entry", {}).get("registry_entry_id") == entry_id),
        None,
    )
    if not isinstance(sample, dict):
        raise ValueError(f"source audit finding is absent from its bound P7 selection: {entry_id}")
    identity = sample["entry"]
    expected_identity = {
        "registry_entry_id": entry_id,
        "ecosystem": sample.get("ecosystem"),
        "package": identity.get("package"),
        "version": identity.get("version"),
        "artifact_sha256": identity.get("artifact_sha256"),
    }
    if any(finding.get(key) != value for key, value in expected_identity.items()):
        raise ValueError(f"source audit identity differs from its locked selection: {entry_id}")
    static_command_ids = [
        row.get("name")
        for row in sample.get("static_inventory", {}).get("capabilities", {}).get("commands", {}).get("items", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    ]
    runtime_command_ids = [command_id.split(" <", 1)[0] for command_id in static_command_ids]
    if (
        finding.get("static_command_ids") != static_command_ids
        or finding.get("command_ids") != runtime_command_ids
        or len(runtime_command_ids) != len(set(runtime_command_ids))
    ):
        raise ValueError(f"source audit runtime command inventory differs from its locked selection: {entry_id}")

    lock = json.loads((CATALOG_ROOT / "artifacts.lock.json").read_text(encoding="utf-8"))
    lock_entry = next((row for row in lock.get("entries", []) if row.get("registry_entry_id") == entry_id), None)
    platform = "all" if sample.get("ecosystem") == "koishi" else "linux-x86_64"
    artifact = next((
        row.get("artifact")
        for row in (lock_entry or {}).get("artifacts", [])
        if row.get("platform") == platform and isinstance(row.get("artifact"), dict)
    ), None)
    if not isinstance(artifact, dict):
        raise ValueError(f"source audit has no frozen artifact lock: {entry_id}")
    artifact_path = (ARTIFACT_CACHE / artifact["cache_path"]).resolve(strict=True)
    artifact_raw = artifact_path.read_bytes()
    if hashlib.sha256(artifact_raw).hexdigest() != identity["artifact_sha256"]:
        raise ValueError(f"source audit locked artifact bytes changed: {entry_id}")

    for source in finding.get("source_files", []):
        member_name = source.get("path")
        if zipfile.is_zipfile(artifact_path):
            with zipfile.ZipFile(artifact_path) as archive:
                member_raw = archive.read(member_name)
        else:
            with tarfile.open(artifact_path) as archive:
                matches = [name for name in archive.getnames() if name.endswith(member_name)]
                if len(matches) != 1:
                    raise ValueError(f"source audit archive member is missing or ambiguous: {entry_id}:{member_name}")
                member = archive.extractfile(matches[0])
                if member is None:
                    raise ValueError(f"source audit archive member is not a file: {entry_id}:{member_name}")
                member_raw = member.read()
        if hashlib.sha256(member_raw).hexdigest() != source.get("sha256"):
            raise ValueError(f"source audit archive member hash changed: {entry_id}:{member_name}")
