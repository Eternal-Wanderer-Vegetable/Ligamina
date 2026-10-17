from __future__ import annotations

import base64
import hashlib
import unittest

from lab.profiles import (
    _direct_requirement_block,
    _integrity_matches,
    _npm_failure,
    _npm_lock_has_integrities,
    _profile_result_reusable,
    _python_failure,
    _python_lock_has_hashes,
)


class ProfileResolutionTests(unittest.TestCase):
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
