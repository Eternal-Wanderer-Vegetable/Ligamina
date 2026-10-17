"""Normalization for the public NoneBot plugin registry."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from .snapshot import canonical_json_bytes, stable_entry_id


def normalize_nonebot(value: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(value, list) or not value:
        raise ValueError("NoneBot registry must be a non-empty JSON array")

    entries: list[dict[str, Any]] = []
    occurrences: Counter[bytes] = Counter()
    identities: set[bytes] = set()
    package_counts: Counter[str] = Counter()
    official_count = 0
    for ordinal, record in enumerate(value):
        if not isinstance(record, dict):
            raise ValueError(f"NoneBot registry entry {ordinal} must be an object")
        module_name = record.get("module_name")
        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError(f"NoneBot registry entry {ordinal} has no module_name")
        official = record.get("is_official")
        if official is not None and type(official) is not bool:
            raise ValueError(f"NoneBot registry entry {ordinal}.is_official must be boolean or null")

        identity = {
            "module_name": module_name.strip(),
            "project_link": record.get("project_link") if isinstance(record.get("project_link"), str) else None,
        }
        identity_bytes = canonical_json_bytes(identity)
        occurrence = occurrences[identity_bytes]
        occurrences[identity_bytes] += 1
        identities.add(identity_bytes)
        package_counts[module_name] += 1
        source_record_sha256 = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
        official_count += int(official is True)
        entries.append({
            "registry_entry_id": stable_entry_id("nonebot", identity, occurrence),
            "ecosystem": "nonebot",
            "source_key": "nonebot_registry",
            "source_ordinal": ordinal,
            "registry_identity": identity,
            "duplicate_identity_ordinal": occurrence,
            "source_record_sha256": source_record_sha256,
            "package": module_name,
            "advertised_version": record.get("version"),
            "entry_point": module_name,
            "official_maintained": official is True,
            "official_maintenance_source": "registry.is_official",
            "source_record": record,
        })

    return entries, {
        "registry_entries": len(entries),
        "unique_registry_identities": len(identities),
        "duplicate_identity_entries": len(entries) - len(identities),
        "duplicate_package_entries": sum(count - 1 for count in package_counts.values() if count > 1),
        "official_maintained_entries": official_count,
    }
