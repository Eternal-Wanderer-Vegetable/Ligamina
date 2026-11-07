from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.pilot_nonebot import DEFAULT_CACHE_ROOT, ROOT, run_nonebot_windows_load_probe


SNAPSHOT = ROOT / "catalogs/p2-41649744e057d61031d5"
ENTRY_ID = "nb-16c80a826ca035f6c49ad69a"
PROFILE_LOCK = ROOT / "outputs/acceptance/phase4-p404-nonebot-python31210-profile-lock-20261028.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the exact P4-04 NoneBot roll Windows Sandbox load pilot using the signed CPython 3.12.10 profile."
    )
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path; existing evidence is never overwritten.")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    options = parser.parse_args()

    output = options.output if options.output.is_absolute() else ROOT / options.output
    if output.exists() or Path(str(output) + ".sha256").exists():
        parser.error(f"refusing to overwrite report or sidecar: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    report = run_nonebot_windows_load_probe(
        SNAPSHOT,
        ENTRY_ID,
        profile_lock_path=PROFILE_LOCK,
        cache_root=options.cache_root,
        timeout_seconds=options.timeout_seconds,
    )
    report["p4_probe_binding"] = {
        "entrypoint_script": "scripts/phase4-p404-nonebot-load-pilot.py",
        "entrypoint_script_sha256": _sha256(Path(__file__).resolve()),
        "pilot_module": "lab/pilot_nonebot.py",
        "pilot_module_sha256": _sha256(ROOT / "lab/pilot_nonebot.py"),
        "profile_lock_path": PROFILE_LOCK.relative_to(ROOT).as_posix(),
        "profile_lock_document_sha256": _sha256(PROFILE_LOCK),
    }
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    Path(str(output) + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(json.dumps({
        "report": output.relative_to(ROOT).as_posix() if output.is_relative_to(ROOT) else str(output),
        "sha256": digest,
        "status": report.get("status"),
        "reason_code": report.get("reason_code"),
        "cleanup_confirmed": report.get("worker", {}).get("cleanup_confirmed"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
