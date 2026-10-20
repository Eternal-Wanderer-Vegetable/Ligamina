from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from lab.automation.selection import AutomationContractError, build_recovery_manifest, object_sha256, validate_manifest

ROOT = Path(__file__).resolve().parents[2]


class AutomationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads((ROOT / "contracts/lab/automation/v1/manifest.schema.json").read_text(encoding="utf-8"))

    def test_minimal_fixture_is_schema_valid(self) -> None:
        fixture = json.loads((ROOT / "contracts/lab/automation/v1/fixtures/manifest.minimal.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(self.schema).iter_errors(fixture))
        self.assertEqual(errors, [])

    def test_validator_rejects_duplicate_missing_tampered_and_cross_identity_rows(self) -> None:
        fixture = json.loads((ROOT / "contracts/lab/automation/v1/fixtures/manifest.minimal.json").read_text(encoding="utf-8"))
        # The golden fixture intentionally carries placeholder digests; make a self-consistent test manifest.
        item = fixture["selection"][0]
        item["identity_sha256"] = object_sha256(item["identity"])
        fixture["gate"]["scope_hash"] = object_sha256({
            "selected": [item["result_id"]], "static_skipped": [],
            "gate": {key: value for key, value in fixture["gate"].items() if key != "scope_hash"},
        })
        fixture["manifest_sha256"] = object_sha256({key: value for key, value in fixture.items() if key != "manifest_sha256"})
        self.assertEqual(validate_manifest(fixture)["selected_stage_rows"], 6)

        duplicate = copy.deepcopy(fixture)
        duplicate["selection"].append(copy.deepcopy(duplicate["selection"][0]))
        duplicate["expected"]["selected_units"] = 2
        duplicate["expected"]["selected_stage_rows"] = 12
        with self.assertRaisesRegex(AutomationContractError, "duplicate"):
            validate_manifest(duplicate)

        missing = copy.deepcopy(fixture)
        missing["expected"]["selected_stage_rows"] = 5
        with self.assertRaisesRegex(AutomationContractError, "denominator"):
            validate_manifest(missing)

        tampered = copy.deepcopy(fixture)
        tampered["selection"][0]["source"]["stage_statuses"]["cleanup"] = "passed"
        with self.assertRaisesRegex(AutomationContractError, "manifest SHA-256"):
            validate_manifest(tampered)

        swapped = copy.deepcopy(fixture)
        swapped["selection"][0]["identity"]["platform"] = "windows"
        with self.assertRaisesRegex(AutomationContractError, "identity"):
            validate_manifest(swapped)

    def test_old_and_new_snapshot_identity_cannot_be_merged(self) -> None:
        fixture = json.loads((ROOT / "contracts/lab/automation/v1/fixtures/manifest.minimal.json").read_text(encoding="utf-8"))
        item = fixture["selection"][0]
        item["identity"]["snapshot_id"] = "old-snapshot"
        item["identity_sha256"] = object_sha256(item["identity"])
        with self.assertRaisesRegex(AutomationContractError, "another snapshot"):
            validate_manifest(fixture)

    def test_s211_full_source_recomputes_stage_selection(self) -> None:
        reports = sorted((ROOT / "outputs/acceptance").glob("phase2-s211-rerun-census-*-shard*of256-20261014.json"))
        common = (
            ROOT / "catalogs/p2-41649744e057d61031d5",
            ROOT / "outputs/acceptance/phase2-s212-audit-20261017.json",
            ROOT / "outputs/acceptance/phase2-s211-failures-20261014.json",
        )
        with self.assertRaisesRegex(AutomationContractError, "exactly 512"):
            build_recovery_manifest(*common, reports[:-1])
        with self.assertRaisesRegex(AutomationContractError, "exactly 512"):
            build_recovery_manifest(*common, reports[:-1] + [reports[0]])
        manifest = build_recovery_manifest(
            *common,
            reports,
        )
        errors = list(Draft202012Validator(self.schema).iter_errors(manifest))
        self.assertEqual(errors, [])
        verified = validate_manifest(manifest, verify_source_files=True)
        self.assertEqual(verified["selected_units"], manifest["expected"]["selected_units"])
        self.assertEqual(verified["selected_stage_rows"], verified["selected_units"] * 6)
        self.assertEqual(manifest["expected"]["source_units"], 11380)
        self.assertTrue(any("stage_unknown:cleanup" in item["selection_reasons"] for item in manifest["selection"]))
        self.assertEqual(manifest["source_identity"]["profile_lock_sha256"], "68a7391620a06e7141d949519333a1f4fc5ccee7abd84dc036c46d09fdae8d38")
        tampered_source = copy.deepcopy(manifest)
        tampered_source["source_files"][0]["sha256"] = "0" * 64
        tampered_source["manifest_sha256"] = object_sha256({key: value for key, value in tampered_source.items() if key != "manifest_sha256"})
        with self.assertRaisesRegex(AutomationContractError, "source evidence hash changed"):
            validate_manifest(tampered_source, verify_source_files=True)


if __name__ == "__main__":
    unittest.main()
