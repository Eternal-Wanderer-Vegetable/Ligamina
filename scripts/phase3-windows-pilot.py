from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.selection import object_sha256, validate_manifest
from lab.pilot_nonebot import _catalog_context, run_nonebot_windows_load_probe
from lab.pilot_workers import _load_catalog_context, _load_npm_lock, run_koishi_windows_load_probe


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def verify_sidecar(path: Path) -> None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"missing SHA-256 sidecar: {sidecar}")
    expected = sidecar.read_text(encoding="utf-8").split()[0]
    actual = sha256_file(path)
    if expected != actual:
        raise ValueError(f"SHA-256 sidecar mismatch: {path}")


def acquire_controller_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+b")
    if lock_file.seek(0, os.SEEK_END) == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock_file.close()
        raise ValueError("another Windows pilot controller already owns the campaign lock") from error
    return lock_file


def counts(rows: list[dict]) -> dict[str, int]:
    return {status: sum(row["result"].get("status") == status for row in rows)
            for status in ("passed", "failed", "skipped", "unknown")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute a frozen Phase 3 Windows recovery pilot or affected campaign.")
    parser.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    parser.add_argument("--manifest", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json")
    parser.add_argument("--profile-lock", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-python31210-profile-lock-pilot-v3-20261025.json")
    parser.add_argument("--cache-root", type=Path, default=Path(r"E:\stella\Ligamina-artifact-cache"))
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-pilot-v2-checkpoint-20261025.json")
    parser.add_argument("--report", type=Path, default=ROOT / "outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json")
    parser.add_argument("--validate-only", action="store_true", help="verify frozen inputs and exit before creating reports or running Workers")
    args = parser.parse_args()

    snapshot = args.snapshot.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    profile_path = args.profile_lock.resolve(strict=True)
    cache_root = args.cache_root.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha = manifest.get("manifest_sha256")
    manifest_kind = manifest.get("kind")
    if manifest_kind not in {
        "phase3_p305_windows_pilot_manifest_v2",
        "phase3_p305_windows_affected_manifest_v1",
        "phase3_p305_windows_koishi_affected_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
    } or manifest.get("platform") != "windows":
        raise ValueError("unexpected pilot manifest kind or platform")
    campaign_id = manifest.get("campaign_id")
    if not isinstance(campaign_id, str) or not re.fullmatch(r"[a-z0-9-]{1,128}", campaign_id):
        raise ValueError("campaign ID is not safe for a controller lock file")
    # A per-campaign lock prevents duplicate controllers without serializing
    # unrelated, separately frozen Windows campaigns behind the pilot lock.
    _controller_lock = acquire_controller_lock(ROOT / ".work" / f"{campaign_id}.controller.lock")
    if object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"}) != manifest_sha:
        raise ValueError("pilot manifest canonical SHA-256 mismatch")
    source_path = (ROOT / manifest["source_recovery_manifest_path"]).resolve(strict=True)
    source_manifest = json.loads(source_path.read_text(encoding="utf-8"))
    validate_manifest(source_manifest, verify_source_files=True)
    if object_sha256({key: value for key, value in source_manifest.items() if key != "manifest_sha256"}) != manifest["source_recovery_manifest_sha256"]:
        raise ValueError("source recovery manifest canonical identity changed")
    if sha256_file(source_path) != manifest["source_recovery_manifest_file_sha256"]:
        raise ValueError("source recovery manifest file hash changed")
    if sha256_file(profile_path) != manifest["profile_lock_sha256"]:
        raise ValueError("alternate profile lock hash differs from frozen pilot manifest")
    verify_sidecar(manifest_path)
    runtime_path = (ROOT / manifest["runtime_admission_path"]).resolve(strict=True)
    if sha256_file(runtime_path) != manifest["runtime_admission_sha256"]:
        raise ValueError("P3-04 Windows admission report hash differs from manifest")
    if manifest_kind in {
        "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
    }:
        sources = manifest.get("probe_sources")
        expected_sources = {
            "pilot_driver_path": "scripts/phase3-windows-pilot.py",
            "worker_source_path": "lab/pilot_workers.py",
        }
        if not isinstance(sources, dict) or any(sources.get(key) != value for key, value in expected_sources.items()):
            raise ValueError("Koishi HTTP follow-up does not bind the expected probe source paths")
        driver_path = (ROOT / sources["pilot_driver_path"]).resolve(strict=True)
        worker_source_path = (ROOT / sources["worker_source_path"]).resolve(strict=True)
        if (
            sha256_file(driver_path) != sources.get("pilot_driver_sha256")
            or sha256_file(worker_source_path) != sources.get("worker_source_sha256")
            or manifest.get("probe_semantics_version") != (5 if manifest_kind == "phase3_p305_windows_koishi_http_followup_v4_manifest_v1" else 4)
        ):
            raise ValueError("Koishi HTTP follow-up probe source or semantics identity changed")

    execution_kind = "phase3_p305_windows_pilot_execution_v2"
    affected_kinds = {
        "phase3_p305_windows_affected_manifest_v1",
        "phase3_p305_windows_koishi_affected_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
    }
    if manifest_kind in affected_kinds:
        execution_kind = "phase3_p305_windows_affected_execution_v1"
        expected_ecosystem = "koishi" if manifest_kind in {
            "phase3_p305_windows_koishi_affected_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
        } else "nonebot"
        if manifest.get("ecosystem") != expected_ecosystem:
            raise ValueError(f"Windows affected manifest must select only the {expected_ecosystem} ecosystem")
        if expected_ecosystem == "koishi":
            execution_kind = (
                "phase3_p305_windows_koishi_http_followup_v3_execution_v1"
                if manifest_kind == "phase3_p305_windows_koishi_http_followup_v3_manifest_v1"
                else "phase3_p305_windows_koishi_http_followup_v4_execution_v1"
                if manifest_kind == "phase3_p305_windows_koishi_http_followup_v4_manifest_v1"
                else "phase3_p305_windows_koishi_http_followup_v2_execution_v1"
                if manifest_kind == "phase3_p305_windows_koishi_http_followup_v2_manifest_v1"
                else "phase3_p305_windows_koishi_http_followup_execution_v1"
                if manifest_kind == "phase3_p305_windows_koishi_http_followup_manifest_v1"
                else "phase3_p305_windows_koishi_affected_execution_v1"
            )
        policy_profile = manifest.get("sandbox_policy_profile")
        if not isinstance(policy_profile, dict) or policy_profile.get("profile_id") != "windows-sandbox-default-sac-evaluation-v1":
            raise ValueError("Windows affected manifest has no accepted default Evaluation policy profile")
        policy_path = (ROOT / policy_profile["preflight_report_path"]).resolve(strict=True)
        if sha256_file(policy_path) != policy_profile.get("preflight_report_file_sha256"):
            raise ValueError("Sandbox Evaluation policy preflight hash differs from the manifest")
        verify_sidecar(policy_path)
        for evidence in policy_profile.get("preflight_evidence", {}).values():
            evidence_path = (ROOT / evidence["path"]).resolve(strict=True)
            if sha256_file(evidence_path) != evidence.get("sha256"):
                raise ValueError(f"Sandbox policy evidence hash differs from the manifest: {evidence_path}")
            verify_sidecar(evidence_path)
        policy_evidence = json.loads(policy_path.read_text(encoding="utf-8"))
        evaluation = policy_evidence.get("sac_evaluation_baseline", {})
        if (
            policy_evidence.get("result") != "default WSB configuration produced the expected two SAC Evaluation policy activations; no hash allow rule or policy change is needed for this Evaluation-scoped batch."
            or evaluation.get("base_policy_guid") != "{1283ac0f-fff1-49ae-ada1-8a933130cad6}"
            or evaluation.get("base_options") != "0xb18d2714"
            or evaluation.get("base_audit_mode_enabled") is not True
            or evaluation.get("flight_supplemental_guid") != "{2678656c-05ef-481f-bc5b-ebd8c991502d}"
            or evaluation.get("flight_supplemental_options") != "0xd1892304"
            or evaluation.get("supplemental_audit_mode_enabled") is not True
            or policy_evidence.get("codeintegrity_event_counts") != {"3076": 0, "3077": 0, "3089": 0, "3099": 18}
        ):
            raise ValueError("Sandbox policy evidence no longer matches the frozen Evaluation profile")

    selection = manifest.get("selection")
    if not isinstance(selection, list) or len(selection) != manifest.get("expected_units"):
        raise ValueError("pilot selection denominator mismatch")
    selected_ids = [row["identity"]["registry_entry_id"] for row in selection]
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("pilot manifest contains duplicate registry identities")
    nonebot_context = _catalog_context(snapshot, platform="windows", profile_lock_path=profile_path)
    koishi_context = _load_catalog_context(snapshot, "windows", profile_lock_path=profile_path)
    if manifest_kind in affected_kinds:
        expected_ecosystem = "koishi" if manifest_kind in {
            "phase3_p305_windows_koishi_affected_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
        } else "nonebot"
        if manifest_kind in {
            "phase3_p305_windows_koishi_http_followup_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
        }:
            follow_up = manifest.get("follow_up")
            if not isinstance(follow_up, dict) or follow_up.get("reason_code") != "required_host_service_unavailable":
                raise ValueError("Koishi HTTP follow-up manifest has no frozen prior-skip scope")
            prior_manifest_path = (ROOT / follow_up["prior_manifest_path"]).resolve(strict=True)
            prior_report_path = (ROOT / follow_up["prior_report_path"]).resolve(strict=True)
            prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
            prior_report = json.loads(prior_report_path.read_text(encoding="utf-8"))
            verify_sidecar(prior_manifest_path)
            verify_sidecar(prior_report_path)
            if (
                sha256_file(prior_manifest_path) != follow_up.get("prior_manifest_file_sha256")
                or sha256_file(prior_report_path) != follow_up.get("prior_report_file_sha256")
                or object_sha256({key: value for key, value in prior_manifest.items() if key != "manifest_sha256"}) != prior_manifest.get("manifest_sha256")
                or prior_manifest.get("manifest_sha256") != follow_up.get("prior_manifest_sha256")
                or prior_report.get("status") != "completed"
                or prior_report.get("campaign_id") != follow_up.get("prior_campaign_id")
                or prior_report.get("manifest_sha256") != prior_manifest.get("manifest_sha256")
                or prior_report.get("manifest_file_sha256") != follow_up.get("prior_manifest_file_sha256")
            ):
                raise ValueError("Koishi HTTP follow-up source campaign evidence changed or is not closed")
            if manifest_kind in {
                "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
            }:
                prior_followup = follow_up.get("prior_followup")
                prior_followup_manifest_path = (ROOT / prior_followup["manifest_path"]).resolve(strict=True) if isinstance(prior_followup, dict) else None
                prior_followup_report_path = (ROOT / prior_followup["report_path"]).resolve(strict=True) if isinstance(prior_followup, dict) else None
                if prior_followup_manifest_path is None or prior_followup_report_path is None:
                    raise ValueError("Koishi HTTP follow-up v2 has no closed follow-up v1 evidence")
                prior_followup_manifest = json.loads(prior_followup_manifest_path.read_text(encoding="utf-8"))
                prior_followup_report = json.loads(prior_followup_report_path.read_text(encoding="utf-8"))
                verify_sidecar(prior_followup_manifest_path)
                verify_sidecar(prior_followup_report_path)
                if (
                    sha256_file(prior_followup_manifest_path) != prior_followup.get("manifest_file_sha256")
                    or sha256_file(prior_followup_report_path) != prior_followup.get("report_file_sha256")
                    or object_sha256({key: value for key, value in prior_followup_manifest.items() if key != "manifest_sha256"}) != prior_followup_manifest.get("manifest_sha256")
                    or prior_followup_manifest.get("manifest_sha256") != prior_followup.get("manifest_sha256")
                    or prior_followup_manifest.get("profile_lock_sha256") != prior_followup.get("profile_lock_sha256")
                    or prior_followup_report.get("campaign_id") != prior_followup.get("campaign_id")
                    or prior_followup_report.get("manifest_sha256") != prior_followup_manifest.get("manifest_sha256")
                    or prior_followup_report.get("manifest_file_sha256") != prior_followup.get("manifest_file_sha256")
                    or prior_followup_report.get("profile_lock_sha256") != prior_followup.get("profile_lock_sha256")
                    or prior_followup_report.get("status") != "completed"
                    or prior_followup_report.get("settled_units") != 6
                    or prior_followup_report.get("remaining_units") != 0
                    or prior_followup_report.get("unit_counts") != {"passed": 0, "failed": 6, "skipped": 0, "unknown": 0}
                    or prior_followup_report.get("cleanup_confirmed") != 6
                    or prior_followup_report.get("cleanup_unconfirmed") != 0
                    or prior_followup_manifest.get("kind") != "phase3_p305_windows_koishi_http_followup_manifest_v1"
                ):
                    raise ValueError("Koishi HTTP follow-up v1 evidence changed or is not closed")
                v1_rows = {
                    row.get("result", {}).get("registry_entry_id"): row
                    for row in prior_followup_report.get("results", [])
                }
                v1_manifest_rows = {
                    row["identity"]["registry_entry_id"]: row
                    for row in prior_followup_manifest.get("selection", [])
                }
                if set(v1_rows) != set(prior_followup.get("eligible_registry_entry_ids", [])) or set(v1_manifest_rows) != set(v1_rows):
                    raise ValueError("Koishi HTTP follow-up v1 identities differ from the frozen v2 scope")
                expected_error = "Host service provider @koishijs/plugin-http does not export a Koishi plugin"
                for entry_id, v1_row in v1_rows.items():
                    result = v1_row.get("result", {})
                    worker = result.get("worker")
                    protocol = worker.get("protocol_report") if isinstance(worker, dict) else None
                    native = protocol.get("native_load") if isinstance(protocol, dict) else None
                    native = native if isinstance(native, dict) else {}
                    if (
                        result.get("status") != "failed"
                        or result.get("reason_code") != "native_plugin_load_failed"
                        or native.get("error_name") != "TypeError"
                        or expected_error not in native.get("error_tail", "")
                    ):
                        raise ValueError(f"Koishi HTTP follow-up v1 result does not match the frozen probe defect: {entry_id}")
                prior_followup_rows = follow_up.get("prior_followup_results")
                if (
                    not isinstance(prior_followup_rows, list)
                    or len(prior_followup_rows) != len(v1_rows)
                    or {row.get("registry_entry_id") for row in prior_followup_rows} != set(v1_rows)
                ):
                    raise ValueError("Koishi HTTP follow-up v2 has no per-unit v1 supersession ledger")
                for row in selection:
                    entry_id = row["identity"]["registry_entry_id"]
                    old_v1_manifest_row = v1_manifest_rows.get(entry_id)
                    old_v1_report_row = v1_rows.get(entry_id)
                    old_v1_result = old_v1_report_row.get("result", {}) if old_v1_report_row else {}
                    superseded = next((item for item in prior_followup_rows if item.get("registry_entry_id") == entry_id), {})
                    if (
                        old_v1_manifest_row is None
                        or old_v1_report_row is None
                        or row.get("superseded_followup") != superseded
                        or superseded.get("result_sha256") != object_sha256(old_v1_result)
                        or row.get("source_identity") != old_v1_manifest_row.get("source_identity")
                        or row.get("source_identity_sha256") != old_v1_manifest_row.get("source_identity_sha256")
                        or row.get("root_evaluation") != old_v1_manifest_row.get("prior_evaluation")
                    ):
                        raise ValueError(f"Koishi HTTP follow-up v2 does not preserve v1 and root-unit evidence: {entry_id}")
                if manifest_kind in {
                    "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
                    "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
                }:
                    failed_freeze = follow_up.get("superseded_freeze_attempt")
                    if not isinstance(failed_freeze, dict):
                        raise ValueError("Koishi HTTP follow-up v3 does not bind its blocked v2 validation attempt")
                    failed_manifest_path = (ROOT / failed_freeze["manifest_path"]).resolve(strict=True)
                    failed_preflight_path = (ROOT / failed_freeze["preflight_path"]).resolve(strict=True)
                    failed_manifest = json.loads(failed_manifest_path.read_text(encoding="utf-8"))
                    failed_preflight = json.loads(failed_preflight_path.read_text(encoding="utf-8"))
                    verify_sidecar(failed_manifest_path)
                    verify_sidecar(failed_preflight_path)
                    if (
                        sha256_file(failed_manifest_path) != failed_freeze.get("manifest_file_sha256")
                        or sha256_file(failed_preflight_path) != failed_freeze.get("preflight_file_sha256")
                        or object_sha256({key: value for key, value in failed_manifest.items() if key != "manifest_sha256"}) != failed_manifest.get("manifest_sha256")
                        or failed_manifest.get("manifest_sha256") != failed_freeze.get("manifest_sha256")
                        or failed_manifest.get("campaign_id") != failed_freeze.get("campaign_id")
                        or failed_preflight.get("campaign_id") != failed_freeze.get("campaign_id")
                        or failed_preflight.get("status") != "ready_for_validate_only"
                        or failed_preflight.get("summary", {}).get("worker_started") is not False
                        or failed_preflight.get("summary", {}).get("catalog_plugin_executed") is not False
                        or failed_freeze.get("worker_started") is not False
                        or failed_freeze.get("validation_outcome") != "blocked_before_worker"
                    ):
                        raise ValueError("Koishi HTTP follow-up v2 blocked-freeze evidence changed")
                    for no_run_path in (failed_freeze.get("checkpoint_path"), failed_freeze.get("report_path")):
                        if not isinstance(no_run_path, str) or (ROOT / no_run_path).exists():
                            raise ValueError("Koishi HTTP follow-up v2 unexpectedly wrote an execution checkpoint/report")
                if manifest_kind == "phase3_p305_windows_koishi_http_followup_v4_manifest_v1":
                    prior_v3 = follow_up.get("prior_execution_v3")
                    if not isinstance(prior_v3, dict):
                        raise ValueError("Koishi HTTP follow-up v4 has no closed v3 report binding")
                    prior_v3_manifest_path = (ROOT / prior_v3["manifest_path"]).resolve(strict=True)
                    prior_v3_report_path = (ROOT / prior_v3["report_path"]).resolve(strict=True)
                    prior_v3_profile_path = (ROOT / prior_v3["profile_lock_path"]).resolve(strict=True)
                    prior_v3_manifest = json.loads(prior_v3_manifest_path.read_text(encoding="utf-8"))
                    prior_v3_report = json.loads(prior_v3_report_path.read_text(encoding="utf-8"))
                    verify_sidecar(prior_v3_manifest_path)
                    verify_sidecar(prior_v3_report_path)
                    verify_sidecar(prior_v3_profile_path)
                    prior_v3_rows = {
                        item.get("result", {}).get("registry_entry_id"): item
                        for item in prior_v3_report.get("results", [])
                    }
                    prior_v3_manifest_rows = {
                        item["identity"]["registry_entry_id"]: item
                        for item in prior_v3_manifest.get("selection", [])
                    }
                    expected_v3_ids = set(prior_followup.get("eligible_registry_entry_ids", []))
                    if (
                        sha256_file(prior_v3_manifest_path) != prior_v3.get("manifest_file_sha256")
                        or sha256_file(prior_v3_report_path) != prior_v3.get("report_file_sha256")
                        or sha256_file(prior_v3_profile_path) != prior_v3.get("profile_lock_file_sha256")
                        or object_sha256({key: value for key, value in prior_v3_manifest.items() if key != "manifest_sha256"}) != prior_v3_manifest.get("manifest_sha256")
                        or prior_v3_manifest.get("manifest_sha256") != prior_v3.get("manifest_sha256")
                        or prior_v3_manifest.get("profile_lock_sha256") != prior_v3.get("profile_lock_sha256")
                        or prior_v3_manifest.get("kind") != "phase3_p305_windows_koishi_http_followup_v3_manifest_v1"
                        or prior_v3_report.get("campaign_id") != prior_v3.get("campaign_id")
                        or prior_v3_report.get("manifest_sha256") != prior_v3_manifest.get("manifest_sha256")
                        or prior_v3_report.get("manifest_file_sha256") != prior_v3.get("manifest_file_sha256")
                        or prior_v3_report.get("profile_lock_sha256") != prior_v3.get("profile_lock_sha256")
                        or prior_v3_report.get("status") != "completed"
                        or prior_v3_report.get("settled_units") != 6
                        or prior_v3_report.get("remaining_units") != 0
                        or prior_v3_report.get("unit_counts") != {"passed": 0, "failed": 6, "skipped": 0, "unknown": 0}
                        or prior_v3_report.get("cleanup_confirmed") != 6
                        or prior_v3_report.get("cleanup_unconfirmed") != 0
                        or set(prior_v3_rows) != expected_v3_ids
                        or set(prior_v3_manifest_rows) != expected_v3_ids
                    ):
                        raise ValueError("Koishi HTTP follow-up v3 source evidence changed or is not closed")
                    prior_v3_results = follow_up.get("prior_v3_results")
                    if not isinstance(prior_v3_results, list) or {item.get("registry_entry_id") for item in prior_v3_results} != expected_v3_ids:
                        raise ValueError("Koishi HTTP follow-up v4 has no per-unit v3 supersession ledger")
                    expected_v3_error = "Cordis Service provider @koishijs/plugin-http did not register http"
                    for entry_id, v3_row in prior_v3_rows.items():
                        result = v3_row.get("result", {})
                        protocol = result.get("worker", {}).get("protocol_report", {})
                        native = protocol.get("native_load", {})
                        if (
                            result.get("status") != "failed"
                            or result.get("reason_code") != "native_plugin_load_failed"
                            or native.get("error_name") != "TypeError"
                            or expected_v3_error not in native.get("error_tail", "")
                        ):
                            raise ValueError(f"Koishi HTTP follow-up v3 result differs from the frozen probe defect: {entry_id}")
                    for row in selection:
                        entry_id = row["identity"]["registry_entry_id"]
                        old_v3_row = prior_v3_manifest_rows.get(entry_id)
                        old_v3_report_row = prior_v3_rows.get(entry_id)
                        old_v3_result = old_v3_report_row.get("result", {}) if old_v3_report_row else {}
                        superseded_v3 = next((item for item in prior_v3_results if item.get("registry_entry_id") == entry_id), {})
                        if (
                            old_v3_row is None
                            or old_v3_report_row is None
                            or row.get("superseded_followup_v3") != superseded_v3
                            or superseded_v3.get("result_sha256") != object_sha256(old_v3_result)
                            or superseded_v3.get("selection_identity_sha256") != object_sha256(old_v3_row.get("identity", {}))
                            or row.get("source_identity") != old_v3_row.get("source_identity")
                            or row.get("source_identity_sha256") != old_v3_row.get("source_identity_sha256")
                        ):
                            raise ValueError(f"Koishi HTTP follow-up v4 does not preserve v3 evidence: {entry_id}")
            prior_manifest_rows = {
                row["identity"]["registry_entry_id"]: row
                for row in prior_manifest.get("selection", [])
            }
            prior_report_rows = {
                row.get("result", {}).get("registry_entry_id"): row
                for row in prior_report.get("results", [])
            }
            candidates = []
            for entry_id, result_row in prior_report_rows.items():
                result = result_row.get("result", {})
                worker = result.get("worker")
                protocol = worker.get("protocol_report") if isinstance(worker, dict) else None
                native = protocol.get("native_load") if isinstance(protocol, dict) else None
                native = native if isinstance(native, dict) else {}
                if (
                    result.get("reason_code") == "required_host_service_unavailable"
                    and result.get("status") == "skipped"
                    and native.get("missing_services") == ["http"]
                ):
                    candidates.append(entry_id)
            selected_follow_up_ids = [row["identity"]["registry_entry_id"] for row in selection]
            if (
                sorted(candidates) != sorted(follow_up.get("eligible_registry_entry_ids", []))
                or set(selected_follow_up_ids) != set(candidates)
                or len(selected_follow_up_ids) != len(set(selected_follow_up_ids))
                or len(selection) != manifest.get("expected_units")
            ):
                raise ValueError("Koishi HTTP follow-up selection does not exactly match prior HTTP-service skips")
            prior_results = follow_up.get("prior_results")
            if not isinstance(prior_results, list) or {row.get("registry_entry_id") for row in prior_results} != set(candidates):
                raise ValueError("Koishi HTTP follow-up has no per-unit prior-result identity set")
            prior_results_by_id = {row["registry_entry_id"]: row for row in prior_results}
            for row in selection:
                entry_id = row["identity"]["registry_entry_id"]
                old_manifest_row = prior_manifest_rows.get(entry_id)
                old_report_row = prior_report_rows.get(entry_id)
                prior_meta = prior_results_by_id.get(entry_id, {})
                if (
                    old_manifest_row is None
                    or old_report_row is None
                    or prior_meta.get("result_sha256") != object_sha256(old_report_row.get("result", {}))
                    or prior_meta.get("selection_identity_sha256") != object_sha256(old_manifest_row.get("identity", {}))
                    or row.get("source_identity") != old_manifest_row.get("source_identity")
                    or row.get("source_identity_sha256") != old_manifest_row.get("source_identity_sha256")
                    or row.get("prior_evaluation") != prior_meta
                ):
                    raise ValueError(f"Koishi HTTP follow-up does not preserve prior unit evidence: {entry_id}")
        else:
            excluded = manifest.get("exclusions", {}).get("source_identity_sha256", [])
            selected_source_hashes = [row.get("source_identity_sha256") for row in selection]
            expected_units = 167 if manifest_kind == "phase3_p305_windows_koishi_affected_manifest_v1" else 786
            if (
                len(selection) != expected_units
                or len(excluded) != 10
                or len(set(excluded)) != 10
                or set(excluded).intersection(selected_source_hashes)
            ):
                raise ValueError("Windows affected selection overlaps the closed pilot or has an unexpected denominator")
        if any(row["identity"].get("ecosystem") != expected_ecosystem for row in selection):
            raise ValueError("Windows affected selection contains an unexpected ecosystem")
    for row in selection:
        identity = row["identity"]
        if object_sha256(identity) != row.get("identity_sha256"):
            raise ValueError(f"entry identity hash mismatch: {identity.get('registry_entry_id')}")
        if object_sha256(row["source_identity"]) != row.get("source_identity_sha256"):
            raise ValueError(f"source identity hash mismatch: {identity.get('registry_entry_id')}")
        if manifest_kind == "phase3_p305_windows_affected_manifest_v1":
            binding = row.get("probe_profile", {})
            if (
                identity.get("platform") != "windows"
                or identity.get("profile_lock_sha256") != manifest.get("profile_lock_sha256")
                or binding.get("profile_lock_sha256") != manifest.get("profile_lock_sha256")
                or binding.get("runtime_admission_sha256") != manifest.get("runtime_admission_sha256")
                or binding.get("sandbox_policy_profile_id") != "windows-sandbox-default-sac-evaluation-v1"
            ):
                raise ValueError(f"NoneBot Sandbox profile binding mismatch: {identity.get('registry_entry_id')}")
        binding = row.get("probe_profile", {})
        if identity["ecosystem"] == "koishi":
            if manifest_kind in {
                "phase3_p305_windows_koishi_affected_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
                "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
            }:
                follow_up = manifest_kind in {
                    "phase3_p305_windows_koishi_http_followup_manifest_v1",
                    "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
                    "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
                    "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
                }
                follow_up_v2 = manifest_kind == "phase3_p305_windows_koishi_http_followup_v2_manifest_v1"
                follow_up_v3 = manifest_kind == "phase3_p305_windows_koishi_http_followup_v3_manifest_v1"
                follow_up_v4 = manifest_kind == "phase3_p305_windows_koishi_http_followup_v4_manifest_v1"
                expected_profile_prefix = (
                    "koishi-p305-http-followup-v4-host-profile-v1" if follow_up_v4
                    else
                    "koishi-p305-http-followup-v3-host-profile-v1" if follow_up_v3
                    else
                    "koishi-p305-http-followup-v2-host-profile-v1" if follow_up_v2
                    else "koishi-p305-http-followup-host-profile-v1" if follow_up
                    else "koishi-p305-windows-host-profile-v1"
                )
                expected_host_profile_prefix = (
                    "koishi-p305-http-followup-v4-v1" if follow_up_v4
                    else
                    "koishi-p305-http-followup-v3-v1" if follow_up_v3
                    else
                    "koishi-p305-http-followup-v2-v1" if follow_up_v2
                    else "koishi-p305-http-followup-v1" if follow_up
                    else "koishi-p305-host-services-v1"
                )
                expected_profile = f"{expected_host_profile_prefix}-{identity['registry_entry_id']}"
                plugins = binding.get("host_service_plugins")
                if (
                    identity.get("platform") != "windows"
                    or identity.get("profile_lock_sha256") != manifest.get("profile_lock_sha256")
                    or identity.get("profile_id") != f"{expected_profile_prefix}-{identity['registry_entry_id']}"
                    or binding.get("host_service_profile_id") != expected_profile
                    or binding.get("profile_lock_sha256") != manifest.get("profile_lock_sha256")
                    or binding.get("runtime_admission_sha256") != manifest.get("runtime_admission_sha256")
                    or binding.get("sandbox_policy_profile_id") != "windows-sandbox-default-sac-evaluation-v1"
                    or binding.get("probe_semantics_version") != (5 if follow_up_v4 else 4 if follow_up_v2 or follow_up_v3 else 3)
                    or binding.get("lifecycle_scripts") != "disabled"
                    or not isinstance(plugins, list)
                ):
                    raise ValueError(f"Koishi host profile binding mismatch: {identity['registry_entry_id']}")
                profile_entry = koishi_context.profiles_by_id.get(identity["registry_entry_id"], {})
                profile = profile_entry.get("profiles", {}).get("windows-x86_64", {})
                lock_ref = profile.get("dependency_lock")
                if (
                    profile.get("profile_id") != identity.get("profile_id")
                    or not isinstance(lock_ref, dict)
                    or lock_ref.get("sha256") != identity.get("dependency_lock_sha256")
                ):
                    raise ValueError(f"Koishi target dependency lock differs from its bound identity: {identity['registry_entry_id']}")
                lock_path = (cache_root / lock_ref["cache_path"]).resolve(strict=True)
                lock, _ = _load_npm_lock(lock_path, lock_ref)
                root_dependencies = lock.get("packages", {}).get("", {}).get("dependencies", {})
                if identity["package"] not in root_dependencies:
                    raise ValueError(f"Koishi target is absent from combined npm root dependencies: {identity['registry_entry_id']}")
                provider_ids: set[str] = set()
                provider_packages: set[str] = set()
                for provider in plugins:
                    if not isinstance(provider, dict):
                        raise ValueError("Koishi host service provider binding is malformed")
                    provider_id = provider.get("registry_entry_id")
                    package_name = provider.get("package")
                    package_version = provider.get("version")
                    if (
                        not isinstance(provider_id, str)
                        or provider_id in provider_ids
                        or not isinstance(package_name, str)
                        or package_name in provider_packages
                        or not isinstance(package_version, str)
                        or provider.get("config") != {}
                        or not isinstance(provider.get("provides"), list)
                    ):
                        raise ValueError(f"Koishi provider profile has duplicate or invalid provider: {identity['registry_entry_id']}")
                    catalog_provider = koishi_context.entries_by_id.get(provider_id, {})
                    provider_profile_entry = koishi_context.profiles_by_id.get(provider_id, {})
                    provider_profile = provider_profile_entry.get("profiles", {}).get("windows-x86_64", {})
                    provider_lock_ref = provider_profile.get("dependency_lock")
                    if (
                        catalog_provider.get("package") != package_name
                        or catalog_provider.get("advertised_version") != package_version
                        or provider.get("artifact_sha256") != provider_profile.get("artifact_sha256")
                        or not isinstance(provider_lock_ref, dict)
                        or root_dependencies.get(package_name) != package_version
                    ):
                        raise ValueError(f"Koishi provider identity is absent from the catalog or combined root lock: {provider_id}")
                    provider_ids.add(provider_id)
                    provider_packages.add(package_name)
                preterminal = row.get("pre_dispatch_terminal_result")
                if follow_up and preterminal is None and not any(
                    provider.get("registry_entry_id") == "ko-bcc8a237e43796843e1c8bc1"
                    and provider.get("package") == "@koishijs/plugin-http"
                    and provider.get("version") == "0.6.3"
                    and provider.get("provides") == ["http"]
                    and (
                        (not follow_up_v2 and not follow_up_v3 and not follow_up_v4 and provider.get("service_kind") is None)
                        or ((follow_up_v2 or follow_up_v3 or follow_up_v4) and provider.get("service_kind") == "cordis-service" and provider.get("export_name") == "HTTP")
                    )
                    for provider in plugins
                ):
                    raise ValueError(f"Koishi HTTP follow-up profile lacks the exact http service provider: {identity['registry_entry_id']}")
                if preterminal is not None:
                    if (
                        not isinstance(preterminal, dict)
                        or preterminal.get("status") != "skipped"
                        or preterminal.get("attempted") is not False
                        or preterminal.get("result_id") != f"catalog:{identity['registry_entry_id']}|windows"
                        or preterminal.get("registry_entry_id") != identity["registry_entry_id"]
                        or preterminal.get("ecosystem") != "koishi"
                        or preterminal.get("platform") != "windows"
                        or preterminal.get("reason_code") not in {
                            "frozen_git_ssh_source_not_worker_allowlisted",
                            "required_service_provider_peer_conflict",
                            "required_service_provider_unavailable",
                            "combined_host_profile_peer_conflict",
                        }
                    ):
                        raise ValueError(f"Koishi pre-dispatch terminal classification is invalid: {identity['registry_entry_id']}")
                elif not root_dependencies:
                    raise ValueError(f"Koishi combined dependency root is empty: {identity['registry_entry_id']}")
            else:
                expected_profile = f"koishi-core-only-native-pilot-v1-{identity['registry_entry_id']}"
                if binding.get("host_service_profile_id") != expected_profile or binding.get("host_service_plugins") != []:
                    raise ValueError(f"Koishi Host-service profile is not explicitly bound: {identity['registry_entry_id']}")
                if binding.get("probe_semantics_version") != 3:
                    raise ValueError(f"Koishi probe semantics version mismatch: {identity['registry_entry_id']}")
    expected_results = {f"catalog:{entry_id}|windows" for entry_id in selected_ids}
    if args.validate_only:
        print(json.dumps({
            "valid": True,
            "campaign_id": campaign_id,
            "manifest_sha256": manifest_sha,
            "profile_lock_sha256": sha256_file(profile_path),
            "selected_units": len(selection),
            "ecosystems": sorted({row["identity"]["ecosystem"] for row in selection}),
            "sandbox_policy_profile_id": manifest.get("sandbox_policy_profile", {}).get("profile_id"),
            "worker_will_start": False,
            "reports_written": False,
        }, ensure_ascii=False), flush=True)
        return 0
    if args.report.exists():
        verify_sidecar(args.report)
        final = json.loads(args.report.read_text(encoding="utf-8"))
        final_ids = [item.get("result", {}).get("result_id") for item in final.get("results", [])]
        if final.get("campaign_id") != manifest["campaign_id"] or final.get("manifest_sha256") != manifest_sha:
            raise ValueError("existing final report belongs to a different campaign identity")
        if final.get("status") != "completed" or set(final_ids) != expected_results or len(final_ids) != len(set(final_ids)):
            raise ValueError("existing final report is not a closed terminal report")
        print(json.dumps({
            "already_completed": True,
            "campaign_id": manifest["campaign_id"],
            "settled": final.get("settled_units"),
            "counts": final.get("unit_counts"),
            "report_file_sha256": sha256_file(args.report),
        }, ensure_ascii=False), flush=True)
        return 0
    rows: list[dict] = []
    if args.checkpoint.exists():
        verify_sidecar(args.checkpoint)
        prior = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        if prior.get("campaign_id") != manifest["campaign_id"] or prior.get("manifest_sha256") != manifest_sha:
            raise ValueError("checkpoint belongs to a different Windows pilot manifest")
        prior_rows = prior.get("results")
        if not isinstance(prior_rows, list):
            raise ValueError("checkpoint result list is missing")
        result_ids = [item.get("result", {}).get("result_id") for item in prior_rows]
        if len(result_ids) != len(set(result_ids)) or not set(result_ids).issubset(expected_results):
            raise ValueError("checkpoint contains duplicate or unexpected result identities")
        if any(item.get("result", {}).get("status") not in {"passed", "failed", "skipped", "unknown"} for item in prior_rows):
            raise ValueError("checkpoint contains a non-terminal result")
        rows.extend(prior_rows)

    def report(status: str) -> dict:
        totals = counts(rows)
        settled = len(rows)
        cleanup_unconfirmed = sum(
            item["result"].get("attempted") is True
            and item["result"].get("worker", {}).get("cleanup_confirmed") is not True
            for item in rows
        )
        value = {
            "schema_version": 1,
            "date": "2026-10-25",
            "task_id": "P3-05",
            "kind": execution_kind,
            "status": status,
            "health": "passed" if status == "completed" and not totals["unknown"] and not cleanup_unconfirmed else "blocked",
            "plugin_compatibility": "not_evaluated",
            "campaign_id": manifest["campaign_id"],
            "manifest_path": str(manifest_path.relative_to(ROOT)),
            "manifest_sha256": manifest_sha,
            "manifest_file_sha256": sha256_file(manifest_path),
            "source_recovery_manifest_sha256": manifest["source_recovery_manifest_sha256"],
            "profile_lock_path": str(profile_path.relative_to(ROOT)),
            "profile_lock_sha256": manifest["profile_lock_sha256"],
            "runtime_admission_sha256": manifest["runtime_admission_sha256"],
            "windows_node_runtime": manifest["windows_node_runtime"],
            "worker_policy": manifest["worker_policy"],
            "expected_units": len(selection),
            "settled_units": settled,
            "remaining_units": len(selection) - settled,
            "unit_counts": totals,
            "cleanup_confirmed": sum(item["result"].get("worker", {}).get("cleanup_confirmed") is True for item in rows),
            "cleanup_unconfirmed": cleanup_unconfirmed,
            "results": rows,
            **({"sandbox_policy_profile": manifest["sandbox_policy_profile"]}
               if manifest_kind in affected_kinds else {}),
            "execution_scope": {
                "worker": "Windows Sandbox only",
                "concurrency": 1,
                "nonebot": "offline pip install and native-load probe; no behavior fixture",
                "koishi": (
                    "offline npm install and native-load probe; per-entry frozen Host-service providers; Cordis Service registration; semantics v5"
                    if manifest_kind == "phase3_p305_windows_koishi_http_followup_v4_manifest_v1"
                    else "offline npm install and native-load probe; per-entry frozen Host-service providers; Cordis Service registration; semantics v4"
                    if manifest_kind in {
                        "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
                        "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
                    }
                    else "offline npm install and native-load probe; per-entry frozen Host-service providers; semantics v3"
                ),
                "behavior": "not executed; no entry-bound fixture in P3-05 pilot",
                "local_plugin_execution": False,
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if manifest_kind in {
            "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
            "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
        }:
            value["probe_sources"] = manifest["probe_sources"]
        return value

    done = {item["result"]["result_id"] for item in rows}
    write_json(args.checkpoint, report("in-progress"))
    for index, row in enumerate(selection, start=1):
        identity = row["identity"]
        entry_id = identity["registry_entry_id"]
        result_id = f"catalog:{entry_id}|windows"
        if result_id in done:
            continue
        attempt_id = f"{manifest['campaign_id']}:{entry_id}:{uuid4().hex[:12]}"
        labels = {
            "campaign_id": manifest["campaign_id"],
            "job_id": f"{result_id}|install-native-cleanup",
            "attempt_id": attempt_id,
            "generation": 1,
        }
        try:
            preterminal = row.get("pre_dispatch_terminal_result")
            if preterminal is not None:
                result = dict(preterminal)
            elif identity["ecosystem"] == "nonebot":
                result = run_nonebot_windows_load_probe(
                    snapshot, entry_id, profile_lock_path=profile_path, cache_root=cache_root,
                    timeout_seconds=1200, context=nonebot_context, worker_labels=labels,
                )
            elif identity["ecosystem"] == "koishi":
                binding = row["probe_profile"]
                result = run_koishi_windows_load_probe(
                    snapshot, entry_id, cache_root=cache_root, timeout_seconds=600,
                    memory_mb=manifest["worker_policy"]["sandbox_memory_mb"], worker_labels=labels,
                    probe_semantics_version=binding["probe_semantics_version"],
                    host_service_plugins=binding["host_service_plugins"],
                    host_service_profile_id=binding["host_service_profile_id"],
                    _context=koishi_context,
                )
            else:
                raise ValueError(f"unsupported ecosystem: {identity.get('ecosystem')}")
        except Exception as error:
            result = {
                "schema_version": 1, "task_id": "P3-05", "result_id": result_id,
                "registry_entry_id": entry_id, "ecosystem": identity["ecosystem"],
                "package": identity.get("package"), "version": identity.get("version"),
                "platform": "windows", "status": "unknown", "reason_code": "worker_preparation_failed",
                "attempted": False, "detail": f"{type(error).__name__}: {error}",
                "worker_labels": labels,
            }
        rows.append({
            "selection_identity": identity,
            "selection_identity_sha256": row["identity_sha256"],
            "source_identity": row["source_identity"],
            "source_identity_sha256": row["source_identity_sha256"],
            "selection_reasons": row["source_selection_reasons"],
            "probe_profile": row.get("probe_profile"),
            "worker_labels": labels,
            "result": result,
        })
        done.add(result_id)
        checkpoint_sha = write_json(args.checkpoint, report("in-progress"))
        print(json.dumps({
            "settled": len(rows), "expected": len(selection), "id": entry_id,
            "status": result.get("status"), "reason_code": result.get("reason_code"),
            "cleanup_confirmed": result.get("worker", {}).get("cleanup_confirmed"),
            "counts": counts(rows), "checkpoint_sha256": checkpoint_sha,
        }, ensure_ascii=False), flush=True)

    result_ids = [item["result"]["result_id"] for item in rows]
    if len(rows) != len(selection) or set(result_ids) != expected_results or len(set(result_ids)) != len(result_ids):
        raise ValueError("pilot did not close each selected Windows identity exactly once")
    final = report("completed")
    final["kind"] = execution_kind
    final["driver_source_sha256"] = sha256_file(Path(__file__).resolve())
    if manifest_kind in {
        "phase3_p305_windows_koishi_http_followup_v2_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v3_manifest_v1",
        "phase3_p305_windows_koishi_http_followup_v4_manifest_v1",
    }:
        final["worker_source_sha256"] = manifest["probe_sources"]["worker_source_sha256"]
    digest = write_json(args.report, final)
    print(json.dumps({"final_report":str(args.report),"report_file_sha256":digest,"manifest_sha256":manifest_sha,"counts":final["unit_counts"]},ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Windows pilot stopped safely: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2)
