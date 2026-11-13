from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

class P7NontrivialUnknownSourceAuditTests(unittest.TestCase):
    def test_candidate_collector_requires_unknown_or_nondeterministic_not_assessed_commands(self) -> None:
        from lab.automation.p7_nontrivial_unknown_source_audit import collect_nontrivial_unknown_candidates

        entry_id = "nb-example"
        command_rows = [
            {"command_id": "unknown", "status": "unknown", "nontrivial": "not_assessed", "descriptor_sha256": "a" * 64, "scenarios": 2},
            {"command_id": "nondeterministic", "status": "nondeterministic", "nontrivial": "not_assessed", "descriptor_sha256": "b" * 64, "scenarios": 3},
            {"command_id": "already-trivial", "status": "unknown", "nontrivial": "trivial_for_generated_normal_vs_edge_inputs", "descriptor_sha256": "c" * 64, "scenarios": 4},
            {"command_id": "not-exercised", "status": "unknown", "nontrivial": "not_assessed", "descriptor_sha256": "d" * 64, "scenarios": 0},
        ]
        entry = {
            "registry_entry_id": entry_id,
            "ecosystem": "nonebot",
            "package": "sample-plugin",
            "version": "1.0.0",
            "artifact_sha256": "f" * 64,
            "commands": {"nontrivial_unknown": 2, "items": command_rows},
            "native_discovery": {"runtime_command_ids": ["unknown", "nondeterministic", "already-trivial"]},
            "rounds": [
                {"scenario_results": [
                    {"command_id": "unknown", "reason_code": "no_native_gateway_operation_observed"},
                    {"command_id": "nondeterministic", "reason_code": "native_repetitions_differ"},
                ]},
                {"scenario_results": [
                    {"command_id": "unknown", "reason_code": "no_native_gateway_operation_observed"},
                    {"command_id": "nondeterministic", "reason_code": "native_repetitions_differ"},
                ]},
                {"scenario_results": [
                    {"command_id": "unknown", "reason_code": "no_native_gateway_operation_observed"},
                    {"command_id": "nondeterministic", "reason_code": "native_repetitions_differ"},
                ]},
            ],
            "scenarios": [],
        }
        selection = {
            "samples": [{
                "ecosystem": "nonebot",
                "entry": {
                    "registry_entry_id": entry_id,
                    "package": "sample-plugin",
                    "version": "1.0.0",
                    "artifact_sha256": "f" * 64,
                },
            }]
        }
        pilot = {"entries": [entry]}

        candidates = collect_nontrivial_unknown_candidates(pilot, selection)

        self.assertEqual([row["command_id"] for row in candidates], ["unknown", "nondeterministic"])
        self.assertEqual(candidates[0]["reason_counts"], {"no_native_gateway_operation_observed": 3})
        self.assertEqual(candidates[1]["reason_counts"], {"native_repetitions_differ": 3})
        self.assertEqual(candidates[0]["artifact_sha256"], "f" * 64)

    def test_candidate_collector_rejects_unknown_count_drift(self) -> None:
        from lab.automation.p7_nontrivial_unknown_source_audit import collect_nontrivial_unknown_candidates

        entry = {
            "registry_entry_id": "nb-example",
            "ecosystem": "nonebot",
            "package": "sample-plugin",
            "version": "1.0.0",
            "artifact_sha256": "f" * 64,
            "commands": {
                "nontrivial_unknown": 2,
                "items": [{"command_id": "only-one", "status": "unknown", "nontrivial": "not_assessed", "descriptor_sha256": "a" * 64, "scenarios": 1}],
            },
            "native_discovery": {"runtime_command_ids": ["only-one"]},
            "rounds": [],
            "scenarios": [],
        }
        selection = {"samples": [{"ecosystem": "nonebot", "entry": {"registry_entry_id": "nb-example", "package": "sample-plugin", "version": "1.0.0", "artifact_sha256": "f" * 64}}]}

        with self.assertRaisesRegex(ValueError, "nontrivial_unknown count differs"):
            collect_nontrivial_unknown_candidates({"entries": [entry]}, selection)

    def test_source_reference_hashes_exact_archive_bytes_and_validates_line_range(self) -> None:
        from lab.automation.p7_nontrivial_unknown_source_audit import validate_archive_source_reference

        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "sample.whl"
            member_path = "sample_plugin/__init__.py"
            member_bytes = b"def command():\n    return 'ok'\n"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(member_path, member_bytes)
            reference = {
                "path": member_path,
                "sha256": hashlib.sha256(member_bytes).hexdigest(),
                "lines": [1, 2],
            }

            observed = validate_archive_source_reference(archive_path, reference)

            self.assertEqual(observed["line_count"], 2)
            self.assertEqual(observed["member_sha256"], reference["sha256"])
            with self.assertRaisesRegex(ValueError, "line range is outside"):
                validate_archive_source_reference(archive_path, {**reference, "lines": [1, 3]})
            with self.assertRaisesRegex(ValueError, "source member SHA-256 changed"):
                validate_archive_source_reference(archive_path, {**reference, "sha256": "0" * 64})


if __name__ == "__main__":
    unittest.main()
