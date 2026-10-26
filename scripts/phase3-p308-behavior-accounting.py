#!/usr/bin/env python3
"""Freeze P3-08 position/attempt accounting without dispatching plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.behavior_campaign import BehaviorCampaignError, behavior_accounting_matches_frozen, build_behavior_accounting_report
from lab.automation.evidence_hash import matches_text_sha256


def _atomic_write(path: Path, value: bytes) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(value)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="verify the report and SHA-256 sidecar without writing")
    options = parser.parse_args(argv)
    try:
        report = build_behavior_accounting_report()
        encoded = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path = options.report.expanduser().absolute()
        sidecar = path.with_suffix(path.suffix + ".sha256")
        file_sha = hashlib.sha256(encoded).hexdigest()
        canonical_sha = report["canonical_sha256"]
        if options.check:
            if not path.is_file() or not sidecar.is_file():
                raise BehaviorCampaignError("P3-08 report or SHA-256 sidecar is missing")
            sidecar_sha = sidecar.read_text(encoding="ascii").strip().split()[0]
            frozen = json.loads(path.read_text(encoding="utf-8"))
            if not matches_text_sha256(path.read_bytes(), sidecar_sha):
                raise BehaviorCampaignError("P3-08 report file does not match its sidecar")
            if not behavior_accounting_matches_frozen(report, frozen):
                raise BehaviorCampaignError("P3-08 report differs from frozen inputs or its sidecar")
            file_sha = sidecar_sha
            canonical_sha = frozen["canonical_sha256"]
        else:
            _atomic_write(path, encoded)
            _atomic_write(sidecar, f"{file_sha}  {path.name}\n".encode("ascii"))
        print(json.dumps({
            "status": report["status"],
            "output": str(path),
            "canonical_sha256": canonical_sha,
            "file_sha256": file_sha,
            "positions": report["scope"]["required_positions"],
            "attempt_slots": report["scope"]["planned_attempt_slots"],
            "dispatched_attempts": report["accounting"]["dispatched_attempts"],
            "behavior_compatibility": report["behavior_compatibility"],
        }, ensure_ascii=False))
        return 0
    except (OSError, BehaviorCampaignError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
