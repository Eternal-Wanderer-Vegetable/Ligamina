"""AB-04: deterministic stratified pilot selection and requirement-name parsing."""

from __future__ import annotations

import unittest

from lab.astrbot_census import _requirement_name, locked_names, metadata_dir_name, select_pilot


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

    def test_plugin_directory_follows_astrbot_metadata_name_rule(self) -> None:
        self.assertEqual(metadata_dir_name("name: astrbot_plugin_gallery\nversion: 2.1.0\n"), "astrbot_plugin_gallery")
        self.assertEqual(metadata_dir_name('desc: x\nname: "plugin_x"  # comment\n'), "plugin_x")
        self.assertIsNone(metadata_dir_name("  name: nested_key_only\n"))
        self.assertIsNone(metadata_dir_name("name: has-dash\n"))
        self.assertIsNone(metadata_dir_name("name: class\n"))
        self.assertIsNone(metadata_dir_name("version: 1\n"))
        self.assertEqual(metadata_dir_name("\ufeffname: astrbot_plugin_teamup\n"), "astrbot_plugin_teamup")


if __name__ == "__main__":
    unittest.main()
