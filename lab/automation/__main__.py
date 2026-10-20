"""Command-line entry for generating and validating P3 automation manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .behavior import BehaviorSuiteError, validate_behavior_suite
from .health import collect_linux_health
from .selection import AutomationContractError, ROOT, build_recovery_manifest, validate_manifest


def _write_json(path: Path, value: dict) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="recompute an immutable S2-11 recovery selection")
    plan.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    plan.add_argument("--audit", type=Path, default=ROOT / "outputs/acceptance/phase2-s212-audit-20261017.json")
    plan.add_argument("--attribution", type=Path, default=ROOT / "outputs/acceptance/phase2-s211-failures-20261014.json")
    plan.add_argument("--reports", type=Path, default=ROOT / "outputs/acceptance")
    plan.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="validate a frozen manifest and its source hashes")
    validate.add_argument("manifest", type=Path)
    preflight = commands.add_parser("preflight", help="collect current Linux Worker admission facts")
    preflight.add_argument("--platform", choices=("linux", "windows"), required=True)
    preflight.add_argument("--probe-report", type=Path)
    preflight.add_argument("--output", type=Path, required=True)
    behavior = commands.add_parser("validate-behavior", help="validate the frozen entry-bound behavior suite")
    behavior.add_argument("--suite", type=Path)
    options = parser.parse_args(argv)
    try:
        if options.command == "plan":
            reports = sorted(options.reports.glob("phase2-s211-rerun-census-*-shard*of256-20261014.json"))
            manifest = build_recovery_manifest(options.snapshot, options.audit, options.attribution, reports)
            _write_json(options.output, manifest)
            result = validate_manifest(manifest, verify_source_files=True)
            print(json.dumps({"status": "planned", "output": str(options.output.resolve()), **result}, ensure_ascii=False))
            return 0
        if options.command == "preflight":
            if options.platform == "windows":
                report = {"schema_version": 1, "kind": "phase3_worker_admission", "platform": "windows", "status": "not_tested", "reason_codes": ["windows_runtime_admission_deferred"], "capabilities": {"claim_allowed": False}}
                _write_json(options.output, report)
                print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "reason_codes": report["reason_codes"]}, ensure_ascii=False))
                return 3
            report = collect_linux_health(probe_report_path=options.probe_report)
            _write_json(options.output, report)
            print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "reason_codes": report["reason_codes"], "environment_fingerprint": report["environment_fingerprint"]}, ensure_ascii=False))
            return 0 if report["status"] == "admitted" else 3
        if options.command == "validate-behavior":
            result = validate_behavior_suite(options.suite) if options.suite else validate_behavior_suite()
            print(json.dumps(result, ensure_ascii=False))
            return 0
        manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
        result = validate_manifest(manifest, verify_source_files=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, AutomationContractError, BehaviorSuiteError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
