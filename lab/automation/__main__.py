"""Command-line entry for generating and validating P3 automation manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .behavior import BehaviorSuiteError, validate_behavior_suite
from .behavior_v2 import (
    MATRIX_PATH,
    P402_MATRIX_PATH,
    P402_SUITE_PATH,
    SUITE_PATH,
    BehaviorV2Error,
    build_phase4_suite,
    build_phase4_p402_suite,
    dry_plan as dry_plan_behavior_v2,
    validate_phase4_suite,
)
from .health import collect_linux_health
from .phase4_campaign import execute_phase4_campaign, write_phase4_evidence_manifest
from .phase4_p402_campaign import execute_phase4_p402_campaign, write_phase4_p402_manifest
from .phase4_p403 import (
    Phase4P403Error,
    build_p403_linux_executor_manifest,
    execute_p403_linux_recovery,
    reconcile_p403_linux_runner_state,
    validate_p403_linux_executor_manifest,
    validate_p403_recovery_manifest,
)
from .phase4_capability_probes import run_service_capability_probe
from .recovery import DEFAULT_CACHE_ROOT, RecoveryPilotError, build_linux_pilot_manifest, execute_linux_recovery_pilot, validate_linux_pilot_manifest
from .selection import AutomationContractError, ROOT, build_recovery_manifest, validate_manifest


def _write_json(path: Path, value: dict) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_json_with_sha(path: Path, value: dict) -> None:
    _write_json(path, value)
    path = path.expanduser().absolute()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")


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
    behavior_v2_build = commands.add_parser("build-behavior-v2", help="build the reviewed Phase 4 migration draft without dispatching plugins")
    behavior_v2_build.add_argument("--suite-output", type=Path, required=True)
    behavior_v2_build.add_argument("--matrix-output", type=Path, required=True)
    behavior_p402_build = commands.add_parser("build-behavior-p402", help="freeze the P4-02 fixed-four Linux behavior suite without changing P4-01")
    behavior_p402_build.add_argument("--suite-output", type=Path, default=P402_SUITE_PATH)
    behavior_p402_build.add_argument("--matrix-output", type=Path, default=P402_MATRIX_PATH)
    behavior_v2_validate = commands.add_parser("validate-behavior-v2", help="validate the Phase 4 behavior suite and frozen source identities")
    behavior_v2_validate.add_argument("--suite", type=Path)
    behavior_v2_validate.add_argument("--matrix", type=Path)
    behavior_v2_validate.add_argument("--skip-artifact-cache", action="store_true", help="check contracts and lock identities without requiring cached package archives")
    behavior_v2_plan = commands.add_parser("plan-behavior-v2", help="print eligible Linux behavior positions; never starts a worker")
    behavior_v2_plan.add_argument("--suite", type=Path)
    behavior_v2_plan.add_argument("--output", type=Path)
    behavior_v2_execute = commands.add_parser("execute-behavior-v2", help="execute the frozen P4-01 behavior campaign in network-disabled Linux Workers")
    behavior_v2_execute.add_argument("--admission", type=Path, required=True)
    behavior_v2_execute.add_argument("--runtime-image", required=True, help="locally available production runtime image pinned by sha256 digest")
    behavior_v2_execute.add_argument("--asset-root", type=Path, required=True, help="external root containing the lock-bound offline Python wheels and npm cache")
    behavior_v2_execute.add_argument("--bundle-root", type=Path, required=True, help="new external directory for Oracle-free Worker input bundles")
    behavior_v2_execute.add_argument("--attempts", type=int, choices=(3,), default=3)
    behavior_v2_execute.add_argument("--service-probe-report", type=Path, required=True, help="hash-verified report from the real Rust Core NoneBot-to-Koishi service probe")
    behavior_v2_execute.add_argument("--output", type=Path, required=True)
    behavior_p402_execute = commands.add_parser("execute-behavior-p402", help="execute the P4-02 Linux campaign through the durable fenced queue")
    behavior_p402_execute.add_argument("--admission", type=Path, required=True)
    behavior_p402_execute.add_argument("--runtime-image", required=True)
    behavior_p402_execute.add_argument("--asset-root", type=Path, required=True)
    behavior_p402_execute.add_argument("--bundle-root", type=Path, required=True)
    behavior_p402_execute.add_argument("--queue-root", type=Path, required=True, help="external directory on the Linux filesystem, such as WSL /home/<user>/.local/share/ligamina/p402")
    behavior_p402_execute.add_argument("--service-probe-report", type=Path, required=True)
    behavior_p402_execute.add_argument("--campaign-id")
    behavior_p402_execute.add_argument("--workers", type=int, choices=(1, 2), default=1)
    behavior_p402_execute.add_argument("--output", type=Path, required=True)
    p402_manifest = commands.add_parser("manifest-behavior-p402", help="materialize the complete P4-02 Linux attempt-slot evidence ledger")
    p402_manifest.add_argument("--campaign-report", type=Path, required=True)
    p402_manifest.add_argument("--output", type=Path, required=True)
    phase4_service_probe = commands.add_parser("probe-phase4-service", help="run the real two-Host Rust Core service capability probe")
    phase4_service_probe.add_argument("--node-executable", type=Path, help="exact Node 24.20.0 executable used by the Koishi Host integration test")
    phase4_service_probe.add_argument("--output", type=Path, required=True)
    phase4_manifest = commands.add_parser("manifest-behavior-v2", help="materialize a hash-bound terminal evidence index for a completed P4-01 campaign")
    phase4_manifest.add_argument("--campaign-report", type=Path, required=True)
    phase4_manifest.add_argument("--output", type=Path, required=True)
    recovery_plan = commands.add_parser("plan-recovery-pilot", help="freeze the Linux-only affected-set pilot and failed controls")
    recovery_plan.add_argument("--manifest", type=Path, default=ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json")
    recovery_plan.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    recovery_plan.add_argument("--reports", type=Path, default=ROOT / "outputs/acceptance")
    recovery_plan.add_argument("--nonebot-image", help="optional digest-pinned local NoneBot Worker image override")
    recovery_plan.add_argument("--koishi-image", help="optional digest-pinned local Koishi Worker image override")
    recovery_plan.add_argument("--archive-cache-root", type=Path, help="verify and bind exact cached archive sizes into a new pilot manifest")
    recovery_plan.add_argument("--worker-timeout-seconds", type=int, default=240, help="bind the per-entry Worker timeout into the pilot manifest")
    recovery_plan.add_argument("--probe-semantics-version", type=int, choices=(1, 2, 3), default=2, help="freeze the Koishi Worker probe contract version")
    recovery_plan.add_argument("--selection-scope", choices=("pilot", "affected-linux"), default="pilot", help="select the stratified 20-unit pilot or all locked P3-01 affected Linux entries")
    recovery_plan.add_argument("--worker-policy-id", default="s205-locked-profile-images-v1")
    recovery_plan.add_argument("--output", type=Path, required=True)
    recovery_validate = commands.add_parser("validate-recovery-pilot", help="validate frozen Linux pilot identities")
    recovery_validate.add_argument("manifest", type=Path)
    recovery_execute = commands.add_parser("execute-recovery-pilot", help="execute frozen Linux pilot in a new fenced campaign")
    recovery_execute.add_argument("--manifest", type=Path, required=True)
    recovery_execute.add_argument("--admission", type=Path, required=True)
    recovery_execute.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    recovery_execute.add_argument("--cache-root", type=Path)
    recovery_execute.add_argument("--queue-root", type=Path, help="queue directory; defaults to .work/automation under the repository")
    recovery_execute.add_argument("--evidence-root", type=Path, default=ROOT.parent / "Ligamina-automation-evidence")
    recovery_execute.add_argument("--timeout", type=int, help="must match the timeout frozen in the pilot manifest")
    recovery_execute.add_argument("--output", type=Path, required=True)
    p403_validate = commands.add_parser("validate-p403-recovery", help="verify the exact 53 P4-03 unknown parent identities and source hashes")
    p403_validate.add_argument("manifest", type=Path, default=ROOT / "outputs/acceptance/phase4-p403-recovery-manifest-20261027.json", nargs="?")
    p403_validate.add_argument("--output", type=Path, required=True)
    p403_plan = commands.add_parser("plan-p403-linux-recovery", help="freeze a Linux executor manifest for exactly the 39 P4-03 parent unknowns")
    p403_plan.add_argument("--manifest", type=Path, default=ROOT / "outputs/acceptance/phase4-p403-recovery-manifest-20261027.json")
    p403_plan.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    p403_plan.add_argument("--reports", type=Path, default=ROOT / "outputs/acceptance")
    p403_plan.add_argument("--worker-timeout-seconds", type=int, default=240)
    p403_plan.add_argument("--output", type=Path, required=True)
    p403_validate_executor = commands.add_parser("validate-p403-linux-executor", help="verify a P4-03 Linux execution manifest is bound to all 39 parent rows")
    p403_validate_executor.add_argument("--manifest", type=Path, required=True)
    p403_validate_executor.add_argument("--executor-manifest", type=Path, required=True)
    p403_validate_executor.add_argument("--output", type=Path, required=True)
    p403_execute = commands.add_parser("execute-p403-linux-recovery", help="run only the 39 Linux P4-03 parent unknowns in a fresh admitted worker campaign")
    p403_execute.add_argument("--manifest", type=Path, required=True)
    p403_execute.add_argument("--executor-manifest", type=Path, required=True)
    p403_execute.add_argument("--admission", type=Path, required=True)
    p403_execute.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    p403_execute.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    p403_execute.add_argument("--queue-root", type=Path, required=True)
    p403_execute.add_argument("--evidence-root", type=Path, required=True)
    p403_execute.add_argument("--timeout", type=int)
    p403_execute.add_argument("--output", type=Path, required=True)
    p403_reconcile = commands.add_parser("reconcile-p403-linux-runner", help="reconcile only exact P4-03 Docker labels after a worker exit or interruption")
    p403_reconcile.add_argument("--manifest", type=Path, required=True)
    p403_reconcile.add_argument("--executor-manifest", type=Path, required=True)
    p403_reconcile.add_argument("--queue-root", type=Path, required=True)
    p403_reconcile.add_argument("--output", type=Path, required=True)
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
        if options.command == "build-behavior-v2":
            suite, matrix = build_phase4_suite()
            suite_data = json.dumps(suite, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            matrix_data = json.dumps(matrix, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            for output, data in ((options.suite_output, suite_data), (options.matrix_output, matrix_data)):
                output = output.expanduser().absolute()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                output.with_suffix(output.suffix + ".sha256").write_text(
                    f"{__import__('hashlib').sha256(data).hexdigest()}  {output.name}\n", encoding="ascii"
                )
            result = validate_phase4_suite(options.suite_output, options.matrix_output, verify_artifacts=True)
            print(json.dumps({"status": "built", "suite_output": str(options.suite_output.resolve()), "matrix_output": str(options.matrix_output.resolve()), **result}, ensure_ascii=False))
            return 0
        if options.command == "build-behavior-p402":
            suite, matrix = build_phase4_p402_suite()
            outputs = ((options.suite_output, suite), (options.matrix_output, matrix))
            from .selection import sha256
            normalized_outputs = [(path.expanduser().absolute(), document) for path, document in outputs]
            for path, _document in normalized_outputs:
                if path.exists() or path.with_suffix(path.suffix + ".sha256").exists():
                    raise FileExistsError(f"refusing to overwrite a frozen P4-02 suite or matrix: {path}")
            written = []
            for path, document in normalized_outputs:
                sidecar = path.with_suffix(path.suffix + ".sha256")
                data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                digest = sha256(data)
                sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
                written.append({"path": str(path), "sha256": digest})
            result = validate_phase4_suite(options.suite_output, options.matrix_output, verify_artifacts=True)
            print(json.dumps({"status": "built", "outputs": written, **result}, ensure_ascii=False))
            return 0
        if options.command == "validate-behavior-v2":
            result = validate_phase4_suite(
                options.suite if options.suite else SUITE_PATH,
                options.matrix if options.matrix else MATRIX_PATH,
                verify_artifacts=not options.skip_artifact_cache,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if options.command == "plan-behavior-v2":
            result = dry_plan_behavior_v2(options.suite if options.suite else SUITE_PATH)
            if options.output:
                _write_json(options.output, result)
                from .selection import sha256

                output = options.output.expanduser().absolute()
                output.with_suffix(output.suffix + ".sha256").write_text(
                    f"{sha256(output.read_bytes())}  {output.name}\n", encoding="ascii"
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if options.command == "execute-behavior-v2":
            report = execute_phase4_campaign(
                admission_path=options.admission,
                runtime_image=options.runtime_image,
                asset_root=options.asset_root,
                bundle_root=options.bundle_root,
                output_path=options.output,
                service_probe_report_path=options.service_probe_report,
                attempts_per_position=options.attempts,
            )
            print(json.dumps({
                "status": report["status"],
                "output": str(options.output.resolve()),
                "campaign_id": report["campaign_id"],
                "counts": report["counts"],
                "gate": report["gate"],
            }, ensure_ascii=False))
            return 0 if report["gate"]["p4_fixture_gate"] == "passed" else 3
        if options.command == "execute-behavior-p402":
            report = execute_phase4_p402_campaign(
                admission_path=options.admission,
                runtime_image=options.runtime_image,
                asset_root=options.asset_root,
                bundle_root=options.bundle_root,
                queue_root=options.queue_root,
                service_probe_report_path=options.service_probe_report,
                output_path=options.output,
                campaign_id=options.campaign_id,
                worker_count=options.workers,
            )
            print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "campaign_id": report["campaign_id"], "counts": report["counts"], "gate": report["gate"]}, ensure_ascii=False))
            return 0 if report["ledger_complete"] and report["gate"]["p4_linux_accounting"] == "passed" else 3
        if options.command == "manifest-behavior-p402":
            result = write_phase4_p402_manifest(options.campaign_report, options.output)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if options.command == "probe-phase4-service":
            report = run_service_capability_probe(options.output, options.node_executable)
            print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "probe_id": report["probe_id"], "runtime": report["runtime"]}, ensure_ascii=False))
            return 0 if report["status"] == "passed" else 3
        if options.command == "manifest-behavior-v2":
            result = write_phase4_evidence_manifest(options.campaign_report, options.output)
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
                archive_cache_root=options.archive_cache_root,
                worker_timeout_seconds=options.worker_timeout_seconds,
                probe_semantics_version=options.probe_semantics_version,
                selection_scope=options.selection_scope,
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
                "queue_root": options.queue_root,
                "timeout_seconds": options.timeout,
            }
            if options.cache_root:
                arguments["cache_root"] = options.cache_root
            report = execute_linux_recovery_pilot(**arguments)
            _write_json(options.output, report)
            print(json.dumps({"status": report["status"], "output": str(options.output.resolve()), "campaign_id": report["campaign_id"], "counts": report["counts"], "gate": report["gate"]}, ensure_ascii=False))
            return 0 if report["status"] == "completed" else 3
        if options.command == "validate-p403-recovery":
            result = validate_p403_recovery_manifest(options.manifest)
            _write_json_with_sha(options.output, result)
            print(json.dumps({**result, "output": str(options.output.resolve())}, ensure_ascii=False))
            return 0
        if options.command == "plan-p403-linux-recovery":
            executor_manifest = build_p403_linux_executor_manifest(
                options.manifest, snapshot_dir=options.snapshot, reports_dir=options.reports,
                worker_timeout_seconds=options.worker_timeout_seconds,
            )
            _write_json_with_sha(options.output, executor_manifest)
            validation = validate_p403_linux_executor_manifest(options.manifest, options.output)
            print(json.dumps({**validation, "output": str(options.output.resolve()), "campaign_id": executor_manifest["campaign_id"]}, ensure_ascii=False))
            return 0
        if options.command == "validate-p403-linux-executor":
            result = validate_p403_linux_executor_manifest(options.manifest, options.executor_manifest)
            _write_json_with_sha(options.output, result)
            print(json.dumps({**result, "output": str(options.output.resolve())}, ensure_ascii=False))
            return 0
        if options.command == "execute-p403-linux-recovery":
            report = execute_p403_linux_recovery(
                options.manifest, options.executor_manifest, options.admission,
                snapshot_dir=options.snapshot, cache_root=options.cache_root,
                evidence_root=options.evidence_root, queue_root=options.queue_root,
                timeout_seconds=options.timeout,
            )
            _write_json_with_sha(options.output, report)
            print(json.dumps({"status": report["status"], "automation_gate": report["automation_gate"], "output": str(options.output.resolve()), "campaign_id": report["campaign_id"], "counts": report["counts"]}, ensure_ascii=False))
            return 0 if report["status"] == "completed" else 3
        if options.command == "reconcile-p403-linux-runner":
            report = reconcile_p403_linux_runner_state(
                options.manifest, options.executor_manifest, options.queue_root,
            )
            _write_json_with_sha(options.output, report)
            print(json.dumps({"status": report["status"], "cleanup_confirmed": report["cleanup_confirmed"], "attempts_reconciled": report["attempts_reconciled"], "output": str(options.output.resolve())}, ensure_ascii=False))
            return 0 if report["status"] in {"confirmed", "not_started"} else 3
        manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
        result = validate_manifest(manifest, verify_source_files=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, AutomationContractError, BehaviorSuiteError, BehaviorV2Error, RecoveryPilotError, Phase4P403Error) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
