"""AB-04: deterministic stratified pilot selection and requirement-name parsing."""

from __future__ import annotations

import unittest

from lab.astrbot_census import _requirement_name, locked_names, select_pilot


def _entry(entry_id: str, *notes: str, status: str = "frozen") -> dict:
    return {"registry_entry_id": entry_id, "status": status, "hosting_notes": list(notes)}


class AstrBotCensusSelectionTests(unittest.TestCase):
    def test_selection_is_deterministic_stratified_and_skips_unfrozen_entries(self) -> None:
        lock = {"entries": [
            _entry("ab-04", "has_requirements"),
            _entry("ab-01"),
            _entry("ab-03", "platform_specific_code", "has_requirements"),
            _entry("ab-02"),
            _entry("ab-05", "llm_dependent"),
            _entry("ab-00", status="download_failed"),
        ]}
        chosen = select_pilot(lock, per_stratum=1)
        self.assertEqual([(stratum, entry["registry_entry_id"]) for stratum, entry in chosen], [
            ("plain", "ab-01"), ("requirements", "ab-04"), ("platform_specific", "ab-03"), ("llm_dependent", "ab-05"),
        ])
        self.assertEqual(chosen, select_pilot(lock, per_stratum=1))

    def test_requirement_names_are_normalized_and_runtime_packages_known(self) -> None:
        self.assertEqual(_requirement_name("Pillow>=9; python_version>'3'"), "pillow")
        self.assertEqual(_requirement_name("jinja2[i18n]==3"), "jinja2")
        self.assertEqual(_requirement_name("Some_Package.Name~=1"), "some-package-name")
        self.assertIn("astrbot", locked_names())
        self.assertIn("aiohttp", locked_names())


if __name__ == "__main__":
    unittest.main()
