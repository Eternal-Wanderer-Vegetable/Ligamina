from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator

from lab.capability_inventory import scan_archive

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "contracts/lab/inventory/v1/capability-inventory.schema.json").read_text())
FIXTURES = ROOT / "contracts/lab/inventory/v1/fixtures"


class CapabilityInventoryTests(unittest.TestCase):
    def test_positive_fixture_is_valid_and_negative_fixture_is_rejected(self):
        validator = Draft202012Validator(SCHEMA)
        positive = json.loads((FIXTURES / "positive.json").read_text())
        negative = json.loads((FIXTURES / "negative.json").read_text())

        self.assertEqual(list(validator.iter_errors(positive)), [])
        self.assertTrue(list(validator.iter_errors(negative)))

    def test_static_scan_finds_python_and_javascript_literals_without_importing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("plugin/main.py", "from nonebot import on_command, on_keyword\ncmd = on_command('hello')\ndynamic = on_command(command_name)\nmatcher = on_keyword({'hi'})\nmatcher = on_alconna(Alconna('alc.echo'))\nraise RuntimeError('must not execute')\n")
                output.writestr("plugin/bom.py", b"\xef\xbb\xbf@filter.command('hello-bom')\ndef handler():\n    pass\n")
                output.writestr("plugin/index.js", "ctx.command('echo <text>')\nctx.on('message', handler)\nctx.cron('0 * * * *', run)\nSchema.string().description('not an event').on('decorator')\n")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            inventory = scan_archive(
                archive,
                ecosystem="nonebot",
                package="fixture",
                version="1.0.0",
                registry_entry_id="fixture-entry",
                artifact_sha256=digest,
            )

        commands = inventory["capabilities"]["commands"]["items"]
        handlers = inventory["capabilities"]["handlers"]["items"]
        schedulers = inventory["capabilities"]["schedulers"]["items"]
        self.assertEqual(
            {item["name"] for item in commands},
            {"hello", "on_command", "alc.echo", "echo <text>", "hello-bom"},
        )
        dynamic = next(item for item in commands if item["name"] == "on_command")
        self.assertEqual(dynamic["attributes"]["name_confidence"], "unknown")
        self.assertIn(("keyword", "hi"), {(item["kind"], item["name"]) for item in handlers})
        self.assertIn(("event_listener", "message"), {(item["kind"], item["name"]) for item in handlers})
        self.assertNotIn(("event_listener", "decorator"), {(item["kind"], item["name"]) for item in handlers})
        self.assertIn(("cron", "0 * * * *"), {(item["kind"], item["name"]) for item in schedulers})
        self.assertEqual(inventory["source"]["kind"], "static")
        self.assertEqual(inventory["plugin"]["artifact_sha256"], digest)
        self.assertEqual(list(Draft202012Validator(SCHEMA).iter_errors(inventory)), [])

    def test_scan_refuses_a_mismatched_artifact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("main.py", "")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                scan_archive(
                    archive,
                    ecosystem="astrbot",
                    package="fixture",
                    version="1.0.0",
                    registry_entry_id="fixture-entry",
                    artifact_sha256="0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
