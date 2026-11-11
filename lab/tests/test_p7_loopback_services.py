from __future__ import annotations

import builtins
import importlib.util
import json
import urllib.error
import urllib.request
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from lab.automation import p7_loopback_services
from lab.automation.p7_loopback_services import (
    LoopbackApiFixture,
    profile_binding,
    validate_bound_profile,
)


REGISTRY_ENTRY_ID = "nb-3ebafd8e699febd7a7e4e52b"
PACKAGE = "nonebot_plugin_warframe_mode"
VERSION = "1.1.0"
ARTIFACT_SHA256 = "086911421ce02e46a11c923005ed5028ca3a74ff0cd6f5ee7a69d42386a96827"
PROFILE_SCHEMA_PATH = p7_loopback_services.ROOT / "contracts/lab/automation/p7-loopback-service-profiles/v1.schema.json"


def _candidate() -> dict[str, str]:
    return {
        "ecosystem": "nonebot",
        "registry_entry_id": REGISTRY_ENTRY_ID,
        "package": PACKAGE,
        "version": VERSION,
        "artifact_sha256": ARTIFACT_SHA256,
    }


class P7LoopbackServiceProfileTests(unittest.TestCase):
    def test_manifest_matches_its_committed_schema(self) -> None:
        schema = json.loads(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        manifest = json.loads(p7_loopback_services.PROFILE_MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual(list(Draft202012Validator(schema).iter_errors(manifest)), [])

    def test_worker_profile_module_loads_without_jsonschema(self) -> None:
        module_path = Path(__file__).parents[1] / "automation/p7_loopback_services.py"
        spec = importlib.util.spec_from_file_location("p7_loopback_services_worker_test", module_path)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def reject_jsonschema(name: str, *args: object, **kwargs: object) -> object:
            if name == "jsonschema" or name.startswith("jsonschema."):
                raise ModuleNotFoundError("jsonschema is unavailable in the locked runtime image")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_jsonschema):
            spec.loader.exec_module(module)
            self.assertIsNotNone(module.profile_binding(_candidate()))

    def test_manifest_rejects_a_changed_wheel_source_hash(self) -> None:
        manifest = json.loads(p7_loopback_services.PROFILE_MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest["source_evidence"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(p7_loopback_services, "PROFILE_MANIFEST_PATH", path):
                with self.assertRaisesRegex(ValueError, "source hashes differ"):
                    p7_loopback_services.validate_profile_manifest()

    def test_profile_is_bound_to_the_exact_frozen_artifact(self) -> None:
        binding = profile_binding(_candidate())

        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding["fixture_host"], "nymph.rbq.life")
        self.assertEqual(binding["fixture_address"], "127.0.0.1")
        self.assertEqual(binding["fixture_port"], 3000)
        self.assertEqual(binding["execution_scope"], "linux_worker_only_network_disabled")
        validate_bound_profile(
            binding,
            {
                "sample_id": f"p7-{REGISTRY_ENTRY_ID}",
                "package": PACKAGE,
                "version": VERSION,
                "_artifact_hash": ARTIFACT_SHA256,
            },
        )

        wrong_artifact = dict(binding, artifact_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "profile hash mismatch"):
            validate_bound_profile(
                wrong_artifact,
                {
                    "sample_id": f"p7-{REGISTRY_ENTRY_ID}",
                    "package": PACKAGE,
                    "version": VERSION,
                    "_artifact_hash": ARTIFACT_SHA256,
                },
            )

    def test_profile_does_not_apply_to_an_unbound_candidate(self) -> None:
        candidate = _candidate()
        candidate["registry_entry_id"] = "nb-not-bound"
        self.assertIsNone(profile_binding(candidate))

    def test_loopback_fixture_serves_only_bound_prefixes_and_redacts_requests(self) -> None:
        binding = profile_binding(_candidate())
        assert binding is not None
        fixture = LoopbackApiFixture(binding, port_override=0)
        fixture.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            url = f"http://127.0.0.1:{fixture.bound_port}/wf/robot/news?query=synthetic-private-value"
            with opener.open(url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("p7 synthetic API fixture", response.read().decode("utf-8"))

            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(f"http://127.0.0.1:{fixture.bound_port}/not-bound?secret=value", timeout=2)
            self.assertEqual(error.exception.code, 404)

            evidence = fixture.evidence()
        finally:
            fixture.close()

        self.assertEqual(evidence["request_count"], 2)
        self.assertEqual(evidence["route_counts"], {"warframe-data": 1})
        self.assertEqual(evidence["unmatched_request_count"], 1)
        self.assertNotIn("synthetic-private-value", str(evidence))
        self.assertNotIn("secret=value", str(evidence))


if __name__ == "__main__":
    unittest.main()
