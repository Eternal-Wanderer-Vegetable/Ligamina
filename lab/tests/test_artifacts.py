from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.artifacts import (
    _choose_pypi_file,
    _metadata_failure_status,
    _npm_release_status,
    _release_status,
    _requires_python_allows,
    _verify_upstream_digest,
    download_artifact,
)


class ArtifactSelectionTests(unittest.TestCase):
    def test_prerelease_is_terminal_and_never_silently_substituted(self) -> None:
        self.assertEqual(_release_status("1.2.0rc1"), "prerelease_advertised")
        self.assertEqual(_npm_release_status("1.2.0-beta.1"), "prerelease_advertised")
        self.assertIsNone(_release_status("1.2.0.post1"))
        self.assertEqual(_npm_release_status("1.2.0+build.7"), None)

    def test_yanked_and_withdrawn_releases_are_distinguished(self) -> None:
        yanked = {"urls": [{
            "filename": "demo-1.0-py3-none-any.whl",
            "packagetype": "bdist_wheel",
            "yanked": True,
        }]}
        withdrawn = {"urls": []}
        self.assertEqual(_choose_pypi_file(yanked, "windows-x86_64"), (None, "yanked"))
        self.assertEqual(_choose_pypi_file(withdrawn, "linux-x86_64"), (None, "withdrawn"))

    def test_requires_python_mismatch_is_not_replaced_with_another_version(self) -> None:
        self.assertFalse(_requires_python_allows("<3.12"))
        self.assertTrue(_requires_python_allows(">=3.12"))
        release = {"urls": [{
            "filename": "demo-1.0-py3-none-any.whl",
            "packagetype": "bdist_wheel",
            "requires_python": "<3.12",
            "yanked": False,
        }]}
        self.assertEqual(_choose_pypi_file(release, "windows-x86_64"), (None, "requires_python_mismatch"))

    def test_sdist_only_release_is_locked_as_source_build_required(self) -> None:
        release = {"urls": [{
            "filename": "demo-1.0.tar.gz",
            "packagetype": "sdist",
            "yanked": False,
            "requires_python": ">=3.9",
        }]}
        record, reason = _choose_pypi_file(release, "linux-x86_64")
        self.assertEqual(record["filename"], "demo-1.0.tar.gz")
        self.assertEqual(reason, "source_build_required")

    def test_registry_digest_is_checked_against_archive_bytes(self) -> None:
        payload = b"locked artifact bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.whl"
            artifact.write_bytes(payload)
            actual = _verify_upstream_digest(artifact, [{"algorithm": "sha256", "hex": digest}])
            self.assertEqual(actual["sha256"], digest)
            with self.assertRaisesRegex(Exception, "do not match"):
                _verify_upstream_digest(artifact, [{"algorithm": "sha256", "hex": "0" * 64}])

    def test_cached_tarball_size_uses_archive_bytes_not_unpacked_metadata(self) -> None:
        payload = b"compressed npm tarball"
        digest = hashlib.sha512(payload).digest()
        expected = [{"algorithm": "sha512", "hex": digest.hex()}]
        with tempfile.TemporaryDirectory() as directory:
            cache_root = Path(directory)
            cache_path = cache_root / "sha256" / digest.hex()[:2] / digest.hex()
            cache_path.parent.mkdir(parents=True)
            cache_path.write_bytes(payload)
            resolved, error = download_artifact({
                "url": "https://registry.npmjs.org/demo/-/demo.tgz",
                "upstream_digests": expected,
                "size_bytes": 987654,
                "unpacked_size_bytes": 987654,
            }, cache_root, allowed_hosts={"registry.npmjs.org"})
        self.assertIsNone(error)
        self.assertEqual(resolved["size_bytes"], len(payload))
        self.assertEqual(resolved["unpacked_size_bytes"], 987654)

    def test_downloaded_tarball_size_uses_archive_bytes(self) -> None:
        payload = b"new compressed npm tarball"
        digest = hashlib.sha512(payload).digest()

        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": str(len(payload))}

            def geturl(self) -> str:
                return "https://registry.npmjs.org/demo/-/demo.tgz"

        with tempfile.TemporaryDirectory() as directory:
            with patch("lab.artifacts.urllib.request.urlopen", return_value=Response(payload)):
                resolved, error = download_artifact({
                    "url": "https://registry.npmjs.org/demo/-/demo.tgz",
                    "upstream_digests": [{"algorithm": "sha512", "hex": digest.hex()}],
                    "size_bytes": 654321,
                    "unpacked_size_bytes": 654321,
                }, Path(directory), allowed_hosts={"registry.npmjs.org"})
        self.assertIsNone(error)
        self.assertEqual(resolved["size_bytes"], len(payload))
        self.assertEqual(resolved["unpacked_size_bytes"], 654321)

    def test_missing_release_is_not_misreported_as_a_network_failure(self) -> None:
        self.assertEqual(_metadata_failure_status(RuntimeError("metadata HTTP 404: https://pypi.org/...")), ("release_unavailable", "release_unavailable"))
        self.assertEqual(_metadata_failure_status(RuntimeError("metadata HTTP 410: https://pypi.org/...")), ("release_withdrawn", "release_withdrawn"))
        self.assertEqual(_metadata_failure_status(RuntimeError("metadata fetch failed after 3 attempts"))[0], "metadata_unavailable")


if __name__ == "__main__":
    unittest.main()
