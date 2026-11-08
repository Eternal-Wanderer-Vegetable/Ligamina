"""AB-03: static AstrBot archive inspection and hosting classification."""

from __future__ import annotations

import io
import unittest
import zipfile

from lab.astrbot_catalog import _simple_yaml, classify, inspect_archive


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class AstrBotCatalogTests(unittest.TestCase):
    def test_metadata_requirements_and_markers_are_read_without_extraction(self) -> None:
        data = _zip({
            "astrbot_plugin_x-1.0/main.py": (
                "from astrbot.api.event import filter\n"
                "class P(Star):\n"
                "    @filter.command('hello')\n"
                "    async def h(self, event): ...\n"
                "    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)\n"
                "    async def q(self, event: AiocqhttpMessageEvent): ...\n"
            ),
            "astrbot_plugin_x-1.0/metadata.yaml": "name: astrbot_plugin_x\nversion: 1.0\nsupport_platforms:\n  - aiocqhttp\n",
            "astrbot_plugin_x-1.0/requirements.txt": "# deps\naiohttp>=3\n",
        })
        result = inspect_archive(data)
        self.assertEqual(result["status"], "inspected")
        self.assertEqual(result["plugin_root"], "astrbot_plugin_x-1.0")
        self.assertEqual(result["metadata"]["support_platforms"], ["aiocqhttp"])
        self.assertEqual(result["requirements"], ["aiohttp>=3"])
        self.assertEqual(result["markers"]["command"], 1)
        self.assertEqual(result["markers"]["aiocqhttp_event"], 1)
        notes = classify({"source_record": {}}, result)
        self.assertIn("platform_specific_code", notes)
        self.assertIn("declares_specific_platforms", notes)
        self.assertIn("has_requirements", notes)

    def test_archives_without_main_or_with_unsafe_paths_are_terminal(self) -> None:
        self.assertEqual(inspect_archive(_zip({"README.md": "x"}))["status"], "invalid_archive")
        self.assertEqual(inspect_archive(_zip({"../evil/main.py": "x"}))["status"], "invalid_archive")
        self.assertEqual(inspect_archive(b"not a zip")["status"], "invalid_archive")

    def test_simple_yaml_reads_scalars_and_lists(self) -> None:
        parsed = _simple_yaml("name: a\ndesc: 'hello'  # comment\ntags: [x, y]\nsupport_platforms:\n  - aiocqhttp\n  - telegram\n")
        self.assertEqual(parsed, {"name": "a", "desc": "hello", "tags": ["x", "y"], "support_platforms": ["aiocqhttp", "telegram"]})


if __name__ == "__main__":
    unittest.main()
