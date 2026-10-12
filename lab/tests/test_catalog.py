from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any

from lab.catalog.koishi import normalize_koishi, normalize_official_page
from lab.catalog.nonebot import normalize_nonebot
from lab.catalog.snapshot import CatalogError, fetch_document, verify_snapshot, write_snapshot


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def nonebot_records(name: str = "Alpha") -> list[dict[str, Any]]:
    return [
        {"module_name": "nonebot_plugin_alpha", "project_link": "https://example.test/alpha", "name": name,
         "version": "1.0.0", "is_official": True, "supported_adapters": ["~onebot.v11"]},
        {"module_name": "nonebot_plugin_alpha", "project_link": "https://example.test/alpha", "name": name,
         "version": "1.0.0", "is_official": True, "supported_adapters": ["~onebot.v11"]},
        {"module_name": "nonebot_plugin_alpha", "project_link": "https://example.test/fork", "name": "Fork",
         "version": "0.2.0", "is_official": False, "supported_adapters": []},
    ]


def koishi_index(total: int = 2, second_name: str = "@community/test") -> dict[str, Any]:
    objects = [
        {"package": {"name": "@koishijs/plugin-console", "version": "5.0.0", "dist-tags": {"latest": "5.0.0"}},
         "manifest": {"service": {"implements": ["console"]}}, "verified": True},
        {"package": {"name": second_name, "version": "1.2.3", "dist-tags": {"latest": "1.2.3"}},
         "manifest": {"service": {"required": []}}, "verified": False},
    ]
    return {"version": "fixture-1", "time": "2026-10-12T00:00:00Z", "total": total, "objects": objects}


def documents(
    *, nonebot_name: str = "Alpha", official_page: bytes = b'<a>@koishijs/plugin-console</a>',
    nonebot_url: str = "https://registry.nonebot.dev/plugins.json",
) -> dict[str, tuple[bytes, dict[str, Any]]]:
    values = {
        "nonebot_registry": (json_bytes(nonebot_records(nonebot_name)), nonebot_url),
        "koishi_registry": (json_bytes(koishi_index()), "https://registry.koishi.chat/index.json"),
        "koishi_official_plugins_page": (official_page, "https://koishi.chat/zh-CN/plugins/"),
    }
    return {
        key: (raw, {"source_key": key, "requested_url": url, "final_url": url, "http_status": 200,
                    "fetched_at_utc": "2026-10-12T00:00:00.000Z", "response_headers": {}, "attempts": 1})
        for key, (raw, url) in values.items()
    }


class FakeResponse:
    def __init__(self, body: bytes, *, url: str, status: int = 200, headers: dict[str, str] | None = None):
        self.body = body
        self.status = status
        self.url = url
        self.headers = headers or {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class CatalogSnapshotTests(unittest.TestCase):
    def test_nonebot_duplicates_are_preserved_and_official_subset_is_explicit(self) -> None:
        entries, counts = normalize_nonebot(nonebot_records())
        self.assertEqual(counts["registry_entries"], 3)
        self.assertEqual(counts["unique_registry_identities"], 2)
        self.assertEqual(counts["duplicate_identity_entries"], 1)
        self.assertEqual(counts["duplicate_package_entries"], 2)
        self.assertEqual(counts["official_maintained_entries"], 2)
        self.assertEqual(len({entry["registry_entry_id"] for entry in entries}), 3)
        self.assertEqual([entry["source_ordinal"] for entry in entries], [0, 1, 2])

    def test_koishi_official_membership_comes_from_page_not_verified_flag(self) -> None:
        names = set(normalize_official_page(b"<p>@koishijs/plugin-console</p>"))
        entries, counts = normalize_koishi(koishi_index(), names)
        self.assertTrue(entries[0]["official_maintained"])
        self.assertTrue(entries[0]["verified"])
        self.assertFalse(entries[1]["official_maintained"])
        self.assertEqual(counts["official_maintained_entries"], 1)
        self.assertEqual(counts["duplicate_package_entries"], 0)
        self.assertEqual(counts["official_page_packages_missing_from_registry"], 0)

    def test_koishi_total_mismatch_rejects_a_missing_page_or_partial_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "total does not match"):
            normalize_koishi(koishi_index(total=3), {"@koishijs/plugin-console"})

    def test_empty_and_malformed_official_sources_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty JSON array"):
            normalize_nonebot([])
        with self.assertRaisesRegex(ValueError, "did not contain"):
            normalize_official_page(b"<html>temporarily empty</html>")

    def test_http_429_honors_retry_after_and_retries_without_empty_success(self) -> None:
        attempts = 0
        delays: list[float] = []

        def opener(*_: Any, **__: Any) -> FakeResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.HTTPError(
                    "https://registry.example.test/plugins.json", 429, "rate limited",
                    {"Retry-After": "0"}, io.BytesIO(b""),
                )
            return FakeResponse(b"[]", url="https://registry.example.test/plugins.json", headers={"ETag": '"abc"'})

        raw, metadata = fetch_document(
            "fixture", "https://registry.example.test/plugins.json", opener=opener, sleeper=delays.append
        )
        self.assertEqual(raw, b"[]")
        self.assertEqual(attempts, 2)
        self.assertEqual(metadata["attempts"], 2)
        self.assertEqual(metadata["response_headers"]["ETag"], '"abc"')
        self.assertEqual(delays, [0.0])

    def test_empty_http_200_is_not_treated_as_an_empty_catalog(self) -> None:
        def opener(*_: Any, **__: Any) -> FakeResponse:
            return FakeResponse(b"", url="https://registry.example.test/plugins.json")

        with self.assertRaisesRegex(CatalogError, "empty document"):
            fetch_document("fixture", "https://registry.example.test/plugins.json", opener=opener, sleeper=lambda _: None)

    def test_snapshot_preserves_raw_bytes_and_offline_verification_recomputes_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_snapshot(documents(), Path(temporary), created_at_utc="2026-10-12T00:00:00.000Z", git={})
            result = verify_snapshot(path)
            self.assertEqual(result["registry_entries"], 5)
            self.assertEqual(result["nonebot_official_maintained"], 2)
            self.assertEqual(result["koishi_official_maintained"], 1)
            provenance = json.loads((path / "provenance.json").read_text(encoding="utf-8"))
            for source in provenance["sources"]:
                raw = (path / source["raw_path"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), source["sha256"])
            changes = json.loads((path / "changes.json").read_text(encoding="utf-8"))
            self.assertEqual(changes["counts"]["added"], 5)
            self.assertEqual(changes["entry_delta"]["added"], [])
            self.assertEqual(len(changes["entry_delta"]["baseline_entry_id_set_sha256"]), 64)

    def test_snapshot_change_and_source_switch_are_recorded_without_collapsing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = write_snapshot(documents(), root, created_at_utc="2026-10-12T00:00:00.000Z", git={})
            changed_docs = documents(nonebot_name="Renamed", nonebot_url="https://mirror.example.test/plugins.json")
            second = write_snapshot(changed_docs, root, created_at_utc="2026-10-12T00:01:00.000Z", git={})
            self.assertNotEqual(first.name, second.name)
            changes = json.loads((second / "changes.json").read_text(encoding="utf-8"))
            self.assertEqual(changes["previous_snapshot_id"], first.name)
            self.assertEqual(changes["counts"]["changed"], 2)
            nonebot_source = next(item for item in changes["source_changes"] if item["source_key"] == "nonebot_registry")
            self.assertEqual(nonebot_source["change"], "source_and_content_changed")
            self.assertEqual(len(json.loads((second / "catalog.json").read_text(encoding="utf-8"))["entries"]), 5)

    def test_raw_tampering_fails_integrity_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_snapshot(documents(), Path(temporary), git={})
            raw = path / "raw" / "nonebot-plugins.json"
            raw.write_bytes(raw.read_bytes() + b" ")
            with self.assertRaisesRegex(CatalogError, "raw integrity mismatch"):
                verify_snapshot(path)


if __name__ == "__main__":
    unittest.main()
