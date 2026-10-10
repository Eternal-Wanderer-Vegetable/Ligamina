"""Check the loopback Core status endpoint from inside the container."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = os.environ.get("LIGAMINA_ADMIN_TOKEN", "")
    if not token or token == "CHANGE_ME":
        return 1
    request = urllib.request.Request(
        "http://127.0.0.1:26701/v1/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, TimeoutError, OSError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
