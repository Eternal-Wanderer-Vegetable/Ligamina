"""AB-05: Oracle citations are bound to the frozen archive; attempts are judged strictly."""

from __future__ import annotations

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from lab.astrbot_tool_campaign import evaluate, prepare, tool_name_part, validate_cases

SOURCE = '''from astrbot.api.star import Star


class Plugin(Star):
    async def menu(self, event):
        return event.plain_result(
            "line one\\n"
            "line two"
        )

    async def greet(self, event, name: str = ""):
        return event.plain_result(f"hello {name}")
'''.encode("utf-8")
ROOT = "plugin-abc123"


def _fixture(directory: Path) -> tuple[dict, dict]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{ROOT}/main.py", SOURCE)
        archive.writestr(f"{ROOT}/metadata.yaml", "name: plugin\n")
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    (directory / "sha256").mkdir()
    (directory / "sha256" / digest).write_bytes(data)
    lock = {"snapshot_id": "ab-test", "entries": [{
        "registry_entry_id": "ab-1", "status": "frozen", "inspection": {"plugin_root": ROOT},
        "artifact": {"cache_path": f"sha256/{digest}", "computed_digests": {"sha256": digest}}}]}
    main_sha = hashlib.sha256(SOURCE).hexdigest()
    cases = {"snapshot_id": "ab-test", "host_id": "astrbot-test", "global_oracle": {"gateway_operation_count": 0, "refused_operations": 0},
             "entries": [{"registry_entry_id": "ab-1", "plugin": "plugin", "artifact_sha256": digest, "plugin_root": ROOT, "cases": [{
                 "case_id": "t-positive", "class": "positive",
                 "stimulus": {"tool_calls": [[{"command_id": "菜单", "arguments": {"arguments": ""}}],
                                             [{"command_id": "greet", "arguments": {"name": "x"}},
                                              {"command_id": "greet", "arguments": {"arguments": ""}}]]},
                 "oracle": {"calls": [
                     [{"replies": [{"text": "line one\nline two", "sources": [
                         {"file": "main.py", "file_sha256": main_sha, "lines": [6, 9], "check": "string_constant"}]}]}],
                     [{"replies": [{"pattern": "hello [a-z]+", "sources": [
                         {"file": "main.py", "file_sha256": main_sha, "lines": [12, 12], "check": "contains", "text": 'f"hello {name}"'}]}]},
                      {"replies": [], "silent_sources": [
                          {"file": "main.py", "file_sha256": main_sha, "lines": [11, 12], "check": "contains", "text": "async def greet"}]}]]}}]}]}
    return cases, lock


def _observed(cases: dict, replies: list[list[list[str]]]) -> dict:
    case = cases["entries"][0]["cases"][0]
    steps = [[{"replies": call, "refused_operations": [], "is_error": False} for call in step] for step in replies]
    listed = sorted({f"cmd.{tool_name_part('astrbot-test')}.{tool_name_part(call['command_id'])}"
                     for step in case["stimulus"]["tool_calls"] for call in step})
    return {"status": "completed", "host_alive_after_shutdown": False, "gateway_operation_count": 0, "gateway_errors": [],
            "tool_calls": {"listed_command_tools": listed, "steps": steps}}


class AstrBotToolCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name)
        self.cases, self.lock = _fixture(self.cache)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_citations_are_checked_against_the_frozen_archive(self) -> None:
        result = validate_cases(self.cases, self.lock, self.cache)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["cases"][0]["citations_checked"], 3)

    def test_oracle_drift_and_source_drift_are_rejected(self) -> None:
        drifted = copy.deepcopy(self.cases)
        drifted["entries"][0]["cases"][0]["oracle"]["calls"][0][0]["replies"][0]["text"] = "line one\nline 2"
        with self.assertRaisesRegex(ValueError, "string constant"):
            validate_cases(drifted, self.lock, self.cache)
        wrong_file = copy.deepcopy(self.cases)
        wrong_file["entries"][0]["cases"][0]["oracle"]["calls"][0][0]["replies"][0]["sources"][0]["file_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "differs from the cited digest"):
            validate_cases(wrong_file, self.lock, self.cache)
        uncited = copy.deepcopy(self.cases)
        uncited["entries"][0]["cases"][0]["oracle"]["calls"][1][1].pop("silent_sources")
        with self.assertRaisesRegex(ValueError, "expected silence"):
            validate_cases(uncited, self.lock, self.cache)

    def test_attempts_pass_only_on_exact_replies_patterns_and_silence(self) -> None:
        case = self.cases["entries"][0]["cases"][0]
        oracle = self.cases["global_oracle"]
        good = _observed(self.cases, [[["line one\nline two"]], [["hello x"], []]])
        self.assertEqual(evaluate(case, "astrbot-test", good, oracle, True), [])
        self.assertTrue(evaluate(case, "astrbot-test", good, oracle, False))
        for replies in ([[["line one\nline two "]], [["hello x"], []]],
                        [[["line one\nline two"]], [["hello X"], []]],
                        [[["line one\nline two"]], [["hello x"], ["unexpected"]]]):
            self.assertTrue(evaluate(case, "astrbot-test", _observed(self.cases, replies), oracle, True), replies)
        leaked = _observed(self.cases, [[["line one\nline two"]], [["hello x"], []]])
        leaked["gateway_operation_count"] = 1
        self.assertTrue(any(issue.startswith("gateway") for issue in evaluate(case, "astrbot-test", leaked, oracle, True)))
        alive = dict(good, host_alive_after_shutdown=True)
        self.assertTrue(any(issue.startswith("host_not_reaped") for issue in evaluate(case, "astrbot-test", alive, oracle, True)))

    def test_tool_names_match_the_core_rule(self) -> None:
        self.assertEqual(tool_name_part("组队菜单"), "____-fda2f352")
        self.assertEqual(tool_name_part("炒股.菜单"), "__.__-ff5e4afa")
        self.assertEqual(tool_name_part("astrbot-ab05"), "astrbot-ab05")

    def test_federated_bundle_contains_the_complete_astrbot_worker_package(self) -> None:
        entry = self.cases["entries"][0]
        lock_entry = self.lock["entries"][0]
        bundle = self.cache / "federated-bundle"

        prepare(entry, lock_entry, bundle, self.cache)

        host_package = bundle / "project/hosts/astrbot/ligamina_astrbot"
        self.assertTrue((host_package / "worker.py").is_file())
        self.assertTrue((host_package / "inventory.py").is_file())


if __name__ == "__main__":
    unittest.main()
