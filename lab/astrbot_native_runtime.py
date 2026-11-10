"""Prepare the hash-locked AstrBot runtime site-packages mounted into P7-03 Workers.

This builds framework dependencies only. Frozen catalog plugins are never imported
or executed by this command; plugin code is loaded later inside the admitted Worker.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "deploy/lab/linux/astrbot/requirements.lock.txt"
EXPECTED_PYTHON = "3.12.14"
EXPECTED_ASTRBOT = "4.29.1"
MANIFEST = "runtime-manifest.json"


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file() and item.name != MANIFEST), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _metadata_version(site: Path, distribution: str) -> str | None:
    prefix = distribution.replace("-", "_").casefold()
    for metadata in site.glob("*.dist-info/METADATA"):
        headers = metadata.read_text(encoding="utf-8", errors="replace").splitlines()
        name = next((line.partition(":")[2].strip() for line in headers if line.lower().startswith("name:")), "")
        version = next((line.partition(":")[2].strip() for line in headers if line.lower().startswith("version:")), "")
        if name.replace("-", "_").casefold() == prefix:
            return version
    return None


def prepare_runtime(output: Path, *, python: str = sys.executable) -> dict[str, Any]:
    output = output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"refusing to reuse an AstrBot runtime target: {output}")
    version = subprocess.run([python, "--version"], capture_output=True, text=True, timeout=10, check=False)
    observed = (version.stdout or version.stderr).strip().removeprefix("Python ")
    if version.returncode != 0 or observed != EXPECTED_PYTHON:
        raise RuntimeError(f"AstrBot runtime preparation requires Python {EXPECTED_PYTHON}, observed {observed!r}")
    output.mkdir(parents=True)
    completed = subprocess.run(
        [python, "-m", "pip", "install", "--require-hashes", "--no-deps", "--disable-pip-version-check",
         "--no-compile", "--target", str(output), "-r", str(LOCK)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"hash-locked AstrBot runtime install failed: {(completed.stderr or completed.stdout)[-3000:]}")
    astrbot_version = _metadata_version(output, "astrbot")
    if astrbot_version != EXPECTED_ASTRBOT:
        raise RuntimeError(f"expected AstrBot {EXPECTED_ASTRBOT}, observed {astrbot_version!r}")
    manifest = {
        "schema_version": 1,
        "kind": "astrbot_native_test_runtime",
        "python_version": EXPECTED_PYTHON,
        "astrbot_version": EXPECTED_ASTRBOT,
        "requirements_lock_path": LOCK.relative_to(ROOT).as_posix(),
        "requirements_lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
        "site_packages_sha256": _tree_sha256(output),
        "distribution_count": len(list(output.glob("*.dist-info/METADATA"))),
        "plugin_code_executed": False,
    }
    (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def write_runtime_archive(site: Path, archive: Path) -> str:
    """Write a deterministic tar.gz so WSL does one large copy to the mounted Worker bundle."""
    archive = archive.expanduser().absolute()
    if archive.exists():
        raise FileExistsError(f"refusing to overwrite an AstrBot runtime archive: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=6) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for path in sorted(site.rglob("*"), key=lambda item: item.relative_to(site).as_posix()):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise ValueError(f"runtime archive contains a non-regular entry: {path}")
                relative = path.relative_to(site).as_posix()
                info = tar.gettarinfo(str(path), arcname=f"site-packages/{relative}")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o755 if path.is_dir() or (path.stat().st_mode & 0o111) else 0o644
                if path.is_file():
                    with path.open("rb") as content:
                        tar.addfile(info, content)
                else:
                    tar.addfile(info)
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new site-packages directory; must not already exist")
    parser.add_argument("--archive", type=Path, help="optional deterministic tar.gz output for efficient Worker bundling")
    parser.add_argument("--python", default=sys.executable, help="Python 3.12.14 executable")
    options = parser.parse_args(argv)
    manifest = prepare_runtime(options.output, python=options.python)
    if options.archive:
        manifest["archive_path"] = str(options.archive.expanduser().absolute())
        manifest["archive_sha256"] = write_runtime_archive(options.output, options.archive)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
