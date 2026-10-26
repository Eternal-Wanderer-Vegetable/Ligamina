#!/usr/bin/env python3
"""Recompute P3-10 gate rulings and emit immutable JSON/Markdown evidence."""

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

from lab.automation.gate_audit import GateAuditError, build_gate_audit


def _atomic_write(path: Path, payload: bytes) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "outputs/acceptance/phase3-p310-gate-audit-20261026.json")
    parser.add_argument("--matrix", type=Path, default=ROOT / "outputs/acceptance/phase3-p310-compatibility-matrix-20261026.md")
    parser.add_argument("--check", action="store_true", help="verify existing outputs and sidecars without writing")
    options = parser.parse_args(argv)
    try:
        report, matrix = build_gate_audit()
        report_bytes = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        matrix_bytes = matrix.encode("utf-8")
        targets = ((options.report.expanduser().absolute(), report_bytes), (options.matrix.expanduser().absolute(), matrix_bytes))
        if options.report.expanduser().absolute() == options.matrix.expanduser().absolute():
            raise GateAuditError("report and matrix paths must be different")
        if options.check:
            for path, expected in targets:
                sidecar = _sidecar(path)
                if not path.is_file() or not sidecar.is_file():
                    raise GateAuditError(f"missing P3-10 output or sidecar: {path}")
                actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                sidecar_sha = sidecar.read_text(encoding="ascii").strip().split()[0]
                if path.read_bytes() != expected or sidecar_sha != actual_sha:
                    raise GateAuditError(f"P3-10 output differs from frozen inputs or sidecar: {path}")
        else:
            if any(path.exists() or _sidecar(path).exists() for path, _ in targets):
                raise GateAuditError("refusing to overwrite existing P3-10 evidence; use --check to verify")
            for path, payload in targets:
                file_sha = hashlib.sha256(payload).hexdigest()
                _atomic_write(path, payload)
                _atomic_write(_sidecar(path), f"{file_sha}  {path.name}\n".encode("ascii"))
        print(json.dumps({
            "status": report["status"],
            "report": options.report.expanduser().absolute().as_posix(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "matrix": options.matrix.expanduser().absolute().as_posix(),
            "matrix_sha256": hashlib.sha256(matrix_bytes).hexdigest(),
            "denominators": report["denominators"],
            "gates": {row["gate"]: row["status"] for row in report["gates"]},
        }, ensure_ascii=False))
        return 0
    except (OSError, GateAuditError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
