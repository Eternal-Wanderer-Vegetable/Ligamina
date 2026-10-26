from __future__ import annotations

import hashlib
import unittest

from lab.automation.evidence_hash import matches_text_sha256


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class EvidenceHashTests(unittest.TestCase):
    def test_lf_checkout_matches_crlf_digest(self) -> None:
        self.assertTrue(matches_text_sha256(b"line one\nline two\n", digest(b"line one\r\nline two\r\n")))

    def test_crlf_checkout_matches_lf_digest(self) -> None:
        self.assertTrue(matches_text_sha256(b"line one\r\nline two\r\n", digest(b"line one\nline two\n")))

    def test_text_changes_do_not_match(self) -> None:
        self.assertFalse(matches_text_sha256(b"line one\nchanged\n", digest(b"line one\r\nline two\r\n")))

    def test_invalid_utf8_and_isolated_carriage_return_do_not_match(self) -> None:
        self.assertFalse(matches_text_sha256(b"\xff\n", "0" * 64))
        self.assertFalse(matches_text_sha256(b"line\rbreak", "0" * 64))


if __name__ == "__main__":
    unittest.main()
