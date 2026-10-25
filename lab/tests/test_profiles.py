from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.profiles import (
    _direct_requirement_block,
    _integrity_matches,
    _load_windows_runtime_admission,
    _merge_runtime_profiles,
    _npm_failure,
    _npm_lock_has_integrities,
    _profile_result_reusable,
    _python_failure,
    _python_profile_id,
    _python_lock_has_hashes,
)
from lab.artifacts import ArtifactError, canonical_json_bytes, sha256_bytes


class ProfileResolutionTests(unittest.TestCase):
    def test_partial_windows_resolution_preserves_base_runtime_overrides(self) -> None:
        base = {
            "profiles": {
                "nonebot": {
                    "runtime_profiles_by_entry": {
                        "pilot-entry": {"profile_id": "signed-python-3.12.10"},
                    },
                },
            },
        }
        selected = {"remaining-entry": {"profile_id": "signed-python-3.12.10"}}

        merged = _merge_runtime_profiles(base, selected)

        self.assertEqual(merged, {**base["profiles"]["nonebot"]["runtime_profiles_by_entry"], **selected})
        self.assertIsNot(merged["pilot-entry"], base["profiles"]["nonebot"]["runtime_profiles_by_entry"]["pilot-entry"])

    def test_python_profile_identity_encodes_the_selected_patch_runtime(self) -> None:
        self.assertEqual(
            _python_profile_id("3.12.14", "windows-x86_64"),
            "nonebot-2.5.0-onebot-2.4.6-py3.12.14-windows-x86_64",
        )
        self.assertEqual(
            _python_profile_id("3.12.10", "windows-x86_64"),
            "nonebot-2.5.0-onebot-2.4.6-py3.12.10-windows-x86_64",
        )

    def test_admitted_signed_windows_runtime_is_bound_to_its_report_hash(self) -> None:
        runtime = {
            "profile_id": "windows-cpython-3.12.10-official-embed-amd64-psf-signed",
            "version": "3.12.10",
            "architecture": "win_amd64",
            "distribution": "python.org embeddable package",
            "archive_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
            "archive_sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
            "executable_sha256": "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a",
            "authenticode_status": "Valid",
            "signer": "Python Software Foundation",
        }
        runtime["profile_sha256"] = sha256_bytes(canonical_json_bytes(runtime))
        document = {
            "task_id": "P3-04",
            "kind": "phase3_worker_admission",
            "platform": "windows",
            "status": "admitted",
            "execution_scope": "worker_runtime_profile_only_no_catalog_plugins",
            "runtime_profile": runtime,
            "capabilities": {"claim_allowed_for_profile": True, "catalog_plugin_claims_allowed": False},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admission.json"
            path.write_bytes(canonical_json_bytes(document))
            verified = _load_windows_runtime_admission(path)
        self.assertEqual(verified["profile_id"], runtime["profile_id"])
        self.assertEqual(verified["version"], "3.12.10")
        self.assertEqual(verified["admission_report_sha256"], sha256_bytes(canonical_json_bytes(document)))

    def test_windows_runtime_admission_rejects_an_unadmitted_or_tampered_identity(self) -> None:
        runtime = {
            "profile_id": "windows-cpython-3.12.10-official-embed-amd64-psf-signed",
            "version": "3.12.10",
            "architecture": "win_amd64",
            "distribution": "python.org embeddable package",
            "archive_url": "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
            "archive_sha256": "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
            "executable_sha256": "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a",
            "authenticode_status": "Valid",
            "signer": "Python Software Foundation",
        }
        runtime["profile_sha256"] = sha256_bytes(canonical_json_bytes(runtime))
        document = {
            "task_id": "P3-04", "kind": "phase3_worker_admission", "platform": "windows",
            "status": "admitted", "execution_scope": "worker_runtime_profile_only_no_catalog_plugins",
            "runtime_profile": runtime, "capabilities": {"claim_allowed_for_profile": True},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "admission.json"
            document["runtime_profile"]["archive_sha256"] = "0" * 64
            path.write_bytes(canonical_json_bytes(document))
            with self.assertRaisesRegex(ArtifactError, "signed CPython 3.12.10"):
                _load_windows_runtime_admission(path)

    def test_python_lock_direct_package_must_carry_the_selected_artifact_hash(self) -> None:
        lock = """# generated lock
demo_plugin==1.2.3 \\
    --hash=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

requests==2.32.5 \\
    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""
        block = _direct_requirement_block(lock, "demo-plugin")
        self.assertIsNotNone(block)
        self.assertIn("0123456789abcdef", block)
        self.assertIsNone(_direct_requirement_block(lock, "other-plugin"))

    def test_profile_failures_keep_peer_conflict_and_source_build_distinct(self) -> None:
        self.assertEqual(_python_failure("No solution found when resolving dependencies", "")[0], "dependency_conflict")
        self.assertEqual(_python_failure("No wheels are available; source distribution requires build", "")[0], "dependency_source_build_required")
        self.assertEqual(_npm_failure("", "npm ERR! ERESOLVE could not resolve peer dependency"), "peer_dependency_conflict")
        self.assertEqual(_npm_failure("", "npm ERR! code EBADENGINE"), "requires_node_mismatch")
        self.assertEqual(_npm_failure("", "npm error code E404"), "dependency_package_unavailable")
        self.assertEqual(_npm_failure("", "npm error code ETARGET"), "dependency_version_unavailable")
        self.assertEqual(_npm_failure("", "npm error code EUNSUPPORTEDPROTOCOL workspace:^"), "unsupported_workspace_dependency")

    def test_npm_lock_integrity_must_match_frozen_registry_digest(self) -> None:
        payload = b"npm archive"
        digest = hashlib.sha512(payload).hexdigest()
        encoded = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        self.assertTrue(_integrity_matches(f"sha512-{encoded}", [{"algorithm": "sha512", "hex": digest}]))
        self.assertFalse(_integrity_matches("sha512-not-the-same", [{"algorithm": "sha512", "hex": digest}]))
        self.assertTrue(_npm_lock_has_integrities({"packages": {
            "": {},
            "node_modules/demo": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/demo/-/demo.tgz", "integrity": "sha512-x"},
        }}))
        self.assertFalse(_npm_lock_has_integrities({"packages": {
            "": {},
            "node_modules/demo": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/demo/-/demo.tgz"},
        }}))
        self.assertTrue(_npm_lock_has_integrities({"packages": {
            "": {},
            "node_modules/git-tarball": {"version": "0.3.80", "resolved": "https://codeload.github.com/example/repo/tar.gz/abcdef0123456789abcdef0123456789abcdef01", "integrity": "sha512-x"},
        }}))
        self.assertFalse(_npm_lock_has_integrities({"packages": {
            "": {},
            "node_modules/http": {"version": "1.0.0", "resolved": "http://registry.npmjs.org/demo.tgz", "integrity": "sha512-x"},
        }}))

    def test_every_python_lock_entry_must_have_a_sha256_hash(self) -> None:
        lock = "demo==1.0 " + "\\" + "\n    --hash=sha256:" + "a" * 64 + "\n"
        self.assertTrue(_python_lock_has_hashes(lock))
        self.assertFalse(_python_lock_has_hashes("demo==1.0\n"))

    def test_resume_reuses_only_closed_terminal_profiles(self) -> None:
        result = {"registry_entry_id": "x", "profiles": {
            "windows-x86_64": {"status": "prerelease_advertised", "dependency_lock": None},
            "linux-x86_64": {"status": "prerelease_advertised", "dependency_lock": None},
        }}
        self.assertTrue(_profile_result_reusable(result, __import__("pathlib").Path(".")))
        result["profiles"]["linux-x86_64"]["status"] = "profile_resolution_failed"
        self.assertFalse(_profile_result_reusable(result, __import__("pathlib").Path(".")))


if __name__ == "__main__":
    unittest.main()
