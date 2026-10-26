"""Line-ending-tolerant verification for UTF-8 text evidence."""

from __future__ import annotations

import hashlib
import re


def matches_text_sha256(data: bytes, expected: str) -> bool:
    """Match raw bytes or the LF/CRLF form of the same valid UTF-8 text.

    Structured evidence and logs are tracked as text in Git. Their byte hash
    may differ between a CRLF Windows checkout and an LF checkout even when
    the text is otherwise identical. This accepts only that line-ending
    variation; invalid UTF-8, isolated carriage returns, and content changes
    remain mismatches.
    """
    digest = lambda value: hashlib.sha256(value).hexdigest()
    if digest(data) == expected:
        return True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if text.encode("utf-8") != data or re.search(r"\r(?!\n)", text):
        return False
    lf = text.replace("\r\n", "\n").encode("utf-8")
    crlf = lf.replace(b"\n", b"\r\n")
    return digest(lf) == expected or digest(crlf) == expected
