"""Start a NoneBot Host with one frozen catalog plugin inside a network-disabled container.

Field-test launcher (ADR 0002): Core runs on the host machine and starts this inside
``docker run -i --network none`` as a Host process. The frozen bundle is mounted
read-only at /input/project (hash-locked requirements plus the matching offline
wheelhouse); everything installs into the /work tmpfs, then the process is replaced by
the stock ``ligamina_nonebot.worker``. Pip output goes to stderr because stdout is the
NDJSON protocol channel.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path("/input/project")
SITE = Path("/work/site-packages")


def main() -> None:
    plugin = os.environ.get("LIGAMINA_FIELD_PLUGIN", "").strip()
    if not plugin:
        raise SystemExit("LIGAMINA_FIELD_PLUGIN must name the frozen plugin module")
    SITE.mkdir(parents=True, exist_ok=True)
    install = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--no-index", "--require-hashes", "--no-deps",
            "--only-binary=:all:", "--find-links", str(PROJECT / "sample/wheelhouse"),
            "--target", str(SITE), "-r", str(PROJECT / "sample/requirements.txt"),
        ],
        stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, check=False,
    )
    if install.returncode:
        raise SystemExit(f"offline locked install failed with exit code {install.returncode}")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/work",
        "PYTHONPATH": f"{PROJECT / 'hosts/nonebot'}{os.pathsep}{SITE}",
        "PYTHONUNBUFFERED": "1",
        "LIGAMINA_NONEBOT_PLUGINS": plugin,
        "LIGAMINA_NONEBOT_NICKNAMES": os.environ.get("LIGAMINA_NONEBOT_NICKNAMES", "Bot"),
        "LIGAMINA_NONEBOT_DATA_DIR": "/work/nonebot-data",
    }
    os.chdir("/work")
    os.execve(sys.executable, [sys.executable, "-m", "ligamina_nonebot.worker"], environment)


if __name__ == "__main__":
    main()
