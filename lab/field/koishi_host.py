"""Start a Koishi Host with one frozen catalog plugin inside a network-disabled container.

Field-test launcher (ADR 0002), the Koishi counterpart of ``nonebot_host.py``: Core runs
on the host machine and starts this inside ``docker run -i --network none --read-only``
as a Host process. The frozen TOOL-04 bundle is mounted read-only at /input/project
(``sample/`` holds the artifact, lockfile and offline npm cache). The profile is built
in the /work tmpfs exactly as ``lab/automation/phase4_worker_side.py`` does, then the
process is replaced by the stock Koishi Host worker baked into the runtime image. npm
output goes to stderr because stdout is the NDJSON protocol channel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

PROJECT = Path("/input/project")
SAMPLE = PROJECT / "sample"
WORK = Path("/work")
PROFILE = WORK / "profile"
HOST = Path("/opt/ligamina/hosts/koishi")
NODE = "/usr/local/bin/node"
NPM = "/usr/local/lib/node_modules/npm/bin/npm-cli.js"
BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(WORK), "TMPDIR": "/tmp", "LANG": "C.UTF-8"}


def run(args: list[str]) -> str:
    result = subprocess.run(
        args, cwd=PROFILE, env=BASE_ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.stderr:
        os.write(2, result.stderr.encode())
    if result.returncode:
        os.write(2, result.stdout.encode())
        raise SystemExit(f"{args[1] if len(args) > 1 else args[0]} failed with exit code {result.returncode}")
    return result.stdout


def main() -> None:
    plugin = os.environ.get("LIGAMINA_FIELD_PLUGIN", "").strip()
    if not plugin:
        raise SystemExit("LIGAMINA_FIELD_PLUGIN must name the frozen plugin package")
    PROFILE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(SAMPLE / "npm-cache.tar.gz") as archive:
        archive.extractall(WORK, filter="data")
    for name in ("package.json", "package-lock.json", "artifact.bin"):
        shutil.copyfile(SAMPLE / name, PROFILE / name)
    output = run([
        NODE, NPM, "ci", "--offline", "--ignore-scripts", "--no-audit", "--fund=false",
        "--strict-peer-deps", "--engine-strict", "--cache", str(WORK / "npm-cache"),
    ])
    os.write(2, output.encode())
    host_link = PROFILE / "ligamina-koishi-host"
    host_link.symlink_to(HOST, target_is_directory=True)
    driver = PROFILE / "node_modules/@minatojs/driver-sqlite"
    if driver.is_symlink() or driver.is_file():
        driver.unlink()
    elif driver.exists():
        shutil.rmtree(driver)
    driver.parent.mkdir(parents=True, exist_ok=True)
    driver.symlink_to(HOST / "node_modules/@minatojs/driver-sqlite", target_is_directory=True)
    url = run([NODE, "--input-type=module", "-e", f"console.log(import.meta.resolve({json.dumps(plugin)}))"]).strip()
    if not url.startswith("file:///work/profile/node_modules/"):
        raise SystemExit(f"plugin resolved outside the profile: {url}")
    environment = {
        **BASE_ENV,
        "LIGAMINA_KOISHI_PLUGINS": url,
        "LIGAMINA_KOISHI_PLUGIN_CONFIG": json.dumps({url: {}}),
        "LIGAMINA_KOISHI_NICKNAMES": os.environ.get("LIGAMINA_KOISHI_NICKNAMES", "Bot"),
        "LIGAMINA_KOISHI_DATA_DIR": str(WORK / "koishi-data"),
    }
    os.chdir(PROFILE)
    os.execve(NODE, [NODE, str(host_link / "dist/worker.js")], environment)


if __name__ == "__main__":
    main()
