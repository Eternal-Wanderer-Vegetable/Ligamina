from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class P7SourceAuditCacheTests(unittest.TestCase):
    def test_required_artifacts_are_derived_from_bound_source_reports_and_catalog_lock(self) -> None:
        from lab.automation.p7_source_audit_cache import required_artifact_targets

        targets = required_artifact_targets()
        self.assertEqual(
            {(row["package"], row["artifact_sha256"]) for row in targets},
            {
                ("koishi-plugin-iirose-trace", "702f50402f9f850a61b6b130fec9d65a7a698dbbbca04e53d93d695f97b5f36f"),
                ("@deepseaxx/koishi-plugin-subculture-id-tool", "edb9f833306554a68fd4ca6371256762bfd71ccda93464482f28d3035a0ab452"),
                ("koishi-plugin-xun-jiangshanfuzhu", "f50452569da7fae897dc35addd00d758f20dc6096611bfc26a770d4bd3dd4917"),
                ("nonebot-plugin-bilibili-image", "a9c165f99d4cf41a2364141050e4646a7223fab3c152aecc2f975739938fec20"),
                ("nonebot_plugin_bilichat", "2fd7ac90900b4039fb71765ebf53886f6ade3593eb466fed5e615d2d82e02bb0"),
                ("nonebot_plugin_kawaii_robot", "dc92f205b3b10091bac1ae6ea6b77be998b62ac7e20966a552a445086374d81a"),
                ("nonebot_plugin_soup", "5e978e4d49fda164666da94e1a15d198a54412255d8340021be605aa9b25ecbd"),
                ("nonebot_plugin_zsmeme", "fc32f126f7ea4e59b48ab42078e7608c6d5af7f67205cad69895cea9c13270d3"),
            },
        )
        self.assertEqual(len(targets), 8)
        self.assertTrue(all(row["artifact_sha256"] for row in targets))

    def test_required_profile_locks_are_hash_bound_to_source_audit_entries(self) -> None:
        from lab.automation.p7_source_audit_cache import required_profile_lock_targets

        targets = required_profile_lock_targets()

        self.assertEqual(len(targets), 8)
        self.assertTrue(all(row["status"] == "verified_source_lock" for row in targets))
        self.assertTrue(all(row["sha256"] == row["path"].name for row in targets))
        self.assertTrue(all(row["path"].is_file() for row in targets))

    def test_prepare_copies_verified_profile_locks_to_external_cache(self) -> None:
        import hashlib

        from lab.automation.p7_source_audit_cache import (
            prepare_source_audit_cache,
            required_profile_lock_targets,
        )

        def download(artifact: dict, _cache_root: Path, *, allowed_hosts: set[str]):
            return (
                {
                    **artifact,
                    "computed_digests": {"sha256": artifact["sha256"]},
                    "size_bytes": 1,
                },
                None,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with patch("lab.automation.p7_source_audit_cache.download_artifact", side_effect=download):
                prepare_source_audit_cache(Path(temporary))

            for target in required_profile_lock_targets():
                installed = Path(temporary) / target["cache_path"]
                self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(), target["sha256"])
                self.assertEqual(installed.stat().st_size, target["size_bytes"])

    def test_prepare_uses_locked_registries_and_returns_verified_artifact_hashes(self) -> None:
        from lab.automation.p7_source_audit_cache import prepare_source_audit_cache

        calls: list[tuple[str, str]] = []

        def download(artifact: dict, _cache_root: Path, *, allowed_hosts: set[str]):
            host = next(iter(allowed_hosts))
            calls.append((artifact["registry_entry_id"], host))
            return (
                {
                    **artifact,
                    "computed_digests": {"sha256": artifact["sha256"]},
                    "size_bytes": 1,
                },
                None,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with patch("lab.automation.p7_source_audit_cache.download_artifact", side_effect=download):
                result = prepare_source_audit_cache(Path(temporary))

        self.assertEqual(len(result), 8)
        self.assertEqual(len(calls), 8)
        self.assertEqual(
            {host for entry_id, host in calls if entry_id.startswith("nb-")},
            {"files.pythonhosted.org"},
        )
        self.assertEqual(
            {host for entry_id, host in calls if entry_id.startswith("ko-")},
            {"registry.npmjs.org"},
        )
        self.assertTrue(all(row["status"] == "verified" for row in result))

    def test_prepare_fails_closed_when_locked_download_cannot_be_verified(self) -> None:
        from lab.automation.p7_source_audit_cache import prepare_source_audit_cache

        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "lab.automation.p7_source_audit_cache.download_artifact",
                return_value=(None, "artifact_hash_mismatch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "artifact_hash_mismatch"):
                    prepare_source_audit_cache(Path(temporary))


if __name__ == "__main__":
    unittest.main()
