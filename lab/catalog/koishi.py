"""Normalization for the Koishi registry and its official-maintenance page."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from .snapshot import canonical_json_bytes, stable_entry_id


PACKAGE_PATTERN = re.compile(r"@koishijs/plugin-[a-z0-9-]+")


def normalize_official_page(raw: bytes) -> list[str]:
    try:
        page = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Koishi official plugins page is not UTF-8") from error
    names = list(dict.fromkeys(PACKAGE_PATTERN.findall(page)))
    if not names:
        raise ValueError("Koishi official plugins page did not contain any package names")
    return names


def normalize_koishi(
    value: Any, official_packages: set[str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(value, dict):
        raise ValueError("Koishi registry root must be an object")
    objects = value.get("objects")
    total = value.get("total")
    if not isinstance(objects, list) or not objects:
        raise ValueError("Koishi registry objects must be a non-empty array")
    if type(total) is not int or total != len(objects):
        raise ValueError(
            f"Koishi registry total does not match the captured objects ({total!r} != {len(objects)})"
        )
    if not isinstance(value.get("version"), (str, int)):
        raise ValueError("Koishi registry is missing its upstream version")

    entries: list[dict[str, Any]] = []
    occurrences: Counter[bytes] = Counter()
    identities: set[bytes] = set()
    package_counts: Counter[str] = Counter()
    official_count = 0
    registry_packages: set[str] = set()
    for ordinal, record in enumerate(objects):
        if not isinstance(record, dict):
            raise ValueError(f"Koishi registry entry {ordinal} must be an object")
        package = record.get("package")
        if not isinstance(package, dict):
            raise ValueError(f"Koishi registry entry {ordinal} has no package metadata object")
        package_name = package.get("name")
        if not isinstance(package_name, str) or not package_name.strip():
            raise ValueError(f"Koishi registry entry {ordinal} has no package.name")
        identity = {"package": package_name.strip()}
        identity_bytes = canonical_json_bytes(identity)
        occurrence = occurrences[identity_bytes]
        occurrences[identity_bytes] += 1
        identities.add(identity_bytes)
        package_counts[package_name] += 1
        registry_packages.add(package_name)
        is_official = package_name in official_packages
        official_count += int(is_official)
        entries.append({
            "registry_entry_id": stable_entry_id("koishi", identity, occurrence),
            "ecosystem": "koishi",
            "source_key": "koishi_registry",
            "source_ordinal": ordinal,
            "registry_identity": identity,
            "duplicate_identity_ordinal": occurrence,
            "source_record_sha256": hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
            "package": package_name,
            "advertised_version": package.get("version"),
            "entry_point": None,
            "official_maintained": is_official,
            "official_maintenance_source": "koishi_official_plugins_page",
            "verified": record.get("verified"),
            "source_record": record,
        })

    return entries, {
        "registry_entries": len(entries),
        "unique_registry_identities": len(identities),
        "duplicate_identity_entries": len(entries) - len(identities),
        "duplicate_package_entries": sum(count - 1 for count in package_counts.values() if count > 1),
        "official_maintained_entries": official_count,
        "official_page_packages": len(official_packages),
        "official_page_packages_missing_from_registry": len(official_packages - registry_packages),
    }
