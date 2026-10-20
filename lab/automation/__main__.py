"""Command-line entry for generating and validating P3 automation manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .behavior import BehaviorSuiteError, validate_behavior_suite
from .health import collect_linux_health
from .recovery import RecoveryPilotError, build_linux_pilot_manifest, execute_linux_recovery_pilot, validate_linux_pilot_manifest
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
    recovery_plan = commands.add_parser("plan-recovery-pilot", help="freeze the Linux-only affected-set pilot and failed controls")
    recovery_plan.add_argument("--manifest", type=Path, default=ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json")
    recovery_plan.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    recovery_plan.add_argument("--reports", type=Path, default=ROOT / "outputs/acceptance")
    recovery_plan.add_argument("--nonebot-image", help="optional digest-pinned local NoneBot Worker image override")
    recovery_plan.add_argument("--koishi-image", help="optional digest-pinned local Koishi Worker image override")
    recovery_plan.add_argument("--worker-policy-id", default="s205-locked-profile-images-v1")
    recovery_plan.add_argument("--output", type=Path, required=True)
    recovery_validate = commands.add_parser("validate-recovery-pilot", help="validate frozen Linux pilot identities")
    recovery_validate.add_argument("manifest", type=Path)
    recovery_execute = commands.add_parser("execute-recovery-pilot", help="execute frozen Linux pilot in a new fenced campaign")
    recovery_execute.add_argument("--manifest", type=Path, required=True)
    recovery_execute.add_argument("--admission", type=Path, required=True)
    recovery_execute.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    recovery_execute.add_argument("--cache-root", type=Path)
    recovery_execute.add_argument("--evidence-root", type=Path, default=ROOT.parent / "Ligamina-automation-evidence")
    recovery_execute.add_argument("--timeout", type=int, default=1200)
    recovery_execute.add_argument("--output", type=Path, required=True)
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
        if options.command == "plan-recovery-pilot":
            worker_images = None
            if options.nonebot_image or options.koishi_image:
                from .recovery import _read_json
                lock, _ = _read_json(ROOT / "lab/baselines/runtime-images.lock.json")
                worker_images = {
                    "nonebot": options.nonebot_image or lock["profile_images"]["nonebot"],
                    "koishi": options.koishi_image or lock["profile_images"]["koishi"],
                }
            manifest = build_linux_pilot_manifest(
                options.manifest, options.snapshot, options.reports,
                worker_images=worker_images, worker_policy_id=options.worker_policy_id,
            )
            _write_json(options.output, manifest)
            result = validate_linux_pilot_manifest(manifest)
            print(json.dumps({"status": "planned", "output": str(options.output.resolve()), **result}, ensure_ascii=False))
            return 0
        if options.command == "validate-recovery-pilot":
            result = validate_linux_pilot_manifest(json.loads(options.manifest.read_text(encoding="utf-8")))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if options.command == "execute-recovery-pilot":
            arguments = {
                "manifest_path": options.manifest,
                "admission_path": options.admission,
                "snapshot_dir": options.snapshot,
                "evidence_root": options.evidence_root,
                "timeout_seconds": options.timeout,
            }
            if options.cache_root:
                arguments["cache_root"] = options.cache_root
            report = execute_linux_recovery_pilot(**arguments)
            _write_json(options.output, report)
            print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "campaign_id": report["campaign_id"], "counts": report["counts"], "gate": report["gate"]}, ensure_ascii=False))
            return 0 if report["status"] == "completed" else 3
        manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
        result = validate_manifest(manifest, verify_source_files=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, AutomationContractError, BehaviorSuiteError, RecoveryPilotError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
