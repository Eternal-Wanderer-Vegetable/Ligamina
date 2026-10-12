from __future__ import annotations

import unittest

from lab.pilot import select_stratified_entries


def _synthetic_cohort(ecosystem: str, count: int = 40):
    entries = []
    facts = {}
    for index in range(count):
        entry_id = f"{ecosystem}-{index:03d}"
        categories = ["message"] if index % 6 == 0 else ["storage"] if index % 7 == 0 else []
        entry = {"ecosystem": ecosystem, "registry_entry_id": entry_id}
        entries.append(entry)
        facts[entry_id] = {
            "categories": categories,
            "unmapped_labels": [],
            "classification_reason_code": None,
            "registry_failure": index % 11 == 0,
            "registry_quality": "known_failure" if index % 11 == 0 else "not_marked_failed",
            "official_maintained": index % 4 == 0,
            "profile_or_artifact_failures": ["profile_conflict"] if index % 5 == 0 else [],
            "profile_ready_both_os": index % 5 != 0,
            "message_metadata_candidate": index % 6 == 0,
            "non_message_metadata_candidate": index % 7 == 0,
            "behavior_fixture_status": "missing_fixture",
        }
    return entries, facts


class StratifiedPilotSelectionTests(unittest.TestCase):
    def test_selection_is_deterministic_and_covers_required_strata(self) -> None:
        entries = []
        facts = {}
        for ecosystem in ("nonebot", "koishi"):
            cohort, cohort_facts = _synthetic_cohort(ecosystem)
            entries.extend(cohort)
            facts.update(cohort_facts)

        selected, reasons, summaries = select_stratified_entries(entries, facts, snapshot_id="snapshot-a")
        reversed_selected, reversed_reasons, reversed_summaries = select_stratified_entries(
            list(reversed(entries)), facts, snapshot_id="snapshot-a",
        )

        self.assertEqual(selected, reversed_selected)
        self.assertEqual(reasons, reversed_reasons)
        self.assertEqual(summaries, reversed_summaries)
        for ecosystem in ("nonebot", "koishi"):
            cohort_ids = {entry["registry_entry_id"] for entry in entries if entry["ecosystem"] == ecosystem}
            selected_ids = cohort_ids & selected
            self.assertEqual(len(selected_ids), 30)
            selected_reasons = {reason for entry_id in selected_ids for reason in reasons[entry_id]}
            self.assertTrue({"official_maintained", "community", "known_registry_failure", "known_lock_failure"} <= selected_reasons)
            self.assertTrue({"message_metadata_candidate", "non_message_metadata_candidate", "condition_missing_fixture", "unmapped_category"} <= selected_reasons)

    def test_cohort_size_below_minimum_fails_closed(self) -> None:
        entries, facts = _synthetic_cohort("nonebot", count=3)
        with self.assertRaisesRegex(ValueError, "needs 30"):
            select_stratified_entries(entries, facts, snapshot_id="snapshot-a")


if __name__ == "__main__":
    unittest.main()
