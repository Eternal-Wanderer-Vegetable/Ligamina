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
from lab.automation.evidence_hash import matches_text_sha256


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


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(canonical)


def _verify_frozen_report(rebuilt: dict, frozen: dict) -> None:
    """Compare the logical report while accepting only text line-ending changes."""
    rebuilt_sources = rebuilt.get("evidence_manifest", [])
    frozen_sources = frozen.get("evidence_manifest", [])
    if len(rebuilt_sources) != len(frozen_sources):
        raise GateAuditError("P3-10 evidence manifest source count changed")
    for current, recorded in zip(rebuilt_sources, frozen_sources, strict=True):
        relative = recorded.get("path")
        if current.get("path") != relative:
            raise GateAuditError("P3-10 evidence manifest source order or identity changed")
        source_path = ROOT / relative
        if not source_path.is_file() or not matches_text_sha256(source_path.read_bytes(), recorded.get("file_sha256", "")):
            raise GateAuditError(f"P3-10 source evidence content changed: {relative}")
        recorded_sidecar = recorded.get("sidecar_path")
        if current.get("sidecar_path") != recorded_sidecar:
            raise GateAuditError(f"P3-10 source sidecar identity changed: {relative}")
        if recorded_sidecar:
            sidecar_path = ROOT / recorded_sidecar
            try:
                sidecar_bytes = sidecar_path.read_bytes()
                token = sidecar_bytes.decode("ascii").strip().split()[0]
            except (OSError, UnicodeDecodeError, IndexError) as error:
                raise GateAuditError(f"invalid P3-10 source sidecar: {recorded_sidecar}") from error
            if token != recorded.get("file_sha256") or not matches_text_sha256(
                sidecar_bytes, recorded.get("sidecar_sha256", "")
            ):
                raise GateAuditError(f"P3-10 source sidecar content changed: {recorded_sidecar}")
        # These fields describe serialized bytes. Keep the frozen values after
        # verifying that current files are an LF/CRLF-equivalent text form.
        for key in ("file_sha256", "size_bytes", "sidecar_sha256"):
            if key in current or key in recorded:
                current[key] = recorded.get(key)

    frozen_body = {key: value for key, value in frozen.items() if key != "report_sha256"}
    if frozen.get("report_sha256") != _canonical_sha256(frozen_body):
        raise GateAuditError("P3-10 frozen report canonical digest is invalid")
    rebuilt_body = {key: value for key, value in rebuilt.items() if key != "report_sha256"}
    rebuilt_digest = _canonical_sha256(rebuilt_body)
    if rebuilt_digest != frozen.get("report_sha256"):
        raise GateAuditError("P3-10 rebuilt report differs beyond text line-ending variations")
    rebuilt["report_sha256"] = rebuilt_digest
    if rebuilt != frozen:
        raise GateAuditError("P3-10 rebuilt report does not match the frozen report")


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
            frozen_report = None
            sidecar_tokens: dict[Path, str] = {}
            for path, _expected in targets:
                sidecar = _sidecar(path)
                if not path.is_file() or not sidecar.is_file():
                    raise GateAuditError(f"missing P3-10 output or sidecar: {path}")
                actual_bytes = path.read_bytes()
                sidecar_sha = sidecar.read_text(encoding="ascii").strip().split()[0]
                if len(sidecar_sha) != 64 or any(character not in "0123456789abcdef" for character in sidecar_sha):
                    raise GateAuditError(f"invalid P3-10 output digest sidecar: {sidecar}")
                if not matches_text_sha256(actual_bytes, sidecar_sha):
                    raise GateAuditError(f"P3-10 output or sidecar content changed: {path}")
                sidecar_tokens[path] = sidecar_sha
                if path == options.report.expanduser().absolute():
                    frozen_report = json.loads(actual_bytes.decode("utf-8"))
                else:
                    expected_matrix_sha = hashlib.sha256(_expected).hexdigest()
                    if not matches_text_sha256(actual_bytes, expected_matrix_sha):
                        raise GateAuditError(f"P3-10 compatibility matrix differs from frozen inputs: {path}")
            if not isinstance(frozen_report, dict):
                raise GateAuditError("P3-10 frozen report is not a JSON object")
            if not matches_text_sha256(
                targets[1][0].read_bytes(), frozen_report.get("matrix", {}).get("file_sha256", "")
            ):
                raise GateAuditError("P3-10 frozen matrix digest does not match its output")
            _verify_frozen_report(report, frozen_report)
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
            "report_sha256": sidecar_tokens.get(options.report.expanduser().absolute(), hashlib.sha256(report_bytes).hexdigest()),
            "matrix": options.matrix.expanduser().absolute().as_posix(),
            "matrix_sha256": sidecar_tokens.get(options.matrix.expanduser().absolute(), hashlib.sha256(matrix_bytes).hexdigest()),
            "denominators": report["denominators"],
            "gates": {row["gate"]: row["status"] for row in report["gates"]},
        }, ensure_ascii=False))
        return 0
    except (OSError, GateAuditError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
