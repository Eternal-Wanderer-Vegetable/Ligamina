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
from lab.pilot_workers import _load_catalog_context, run_koishi_windows_load_probe


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

    execution_kind = "phase3_p305_windows_pilot_execution_v2"
    if manifest_kind == "phase3_p305_windows_affected_manifest_v1":
        execution_kind = "phase3_p305_windows_affected_execution_v1"
        if manifest.get("ecosystem") != "nonebot":
            raise ValueError("Windows affected manifest must select only the NoneBot ecosystem")
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
    if manifest_kind == "phase3_p305_windows_affected_manifest_v1":
        excluded = manifest.get("exclusions", {}).get("source_identity_sha256", [])
        selected_source_hashes = [row.get("source_identity_sha256") for row in selection]
        if (
            len(selection) != 786
            or any(row["identity"].get("ecosystem") != "nonebot" for row in selection)
            or len(excluded) != 10
            or len(set(excluded)) != 10
            or set(excluded).intersection(selected_source_hashes)
        ):
            raise ValueError("Windows affected selection overlaps the closed pilot or has an unexpected denominator")
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
            expected_profile = f"koishi-core-only-native-pilot-v1-{identity['registry_entry_id']}"
            if binding.get("host_service_profile_id") != expected_profile or binding.get("host_service_plugins") != []:
                raise ValueError(f"Koishi Host-service profile is not explicitly bound: {identity['registry_entry_id']}")
            if binding.get("probe_semantics_version") != 3:
                raise ValueError(f"Koishi probe semantics version mismatch: {identity['registry_entry_id']}")

    nonebot_context = _catalog_context(snapshot, platform="windows", profile_lock_path=profile_path)
    koishi_context = _load_catalog_context(snapshot, "windows", profile_lock_path=profile_path)
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
        return {
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
               if manifest_kind == "phase3_p305_windows_affected_manifest_v1" else {}),
            "execution_scope": {
                "worker": "Windows Sandbox only",
                "concurrency": 1,
                "nonebot": "offline pip install and native-load probe; no behavior fixture",
                "koishi": "offline npm install and native-load probe; empty Host-service providers; semantics v3",
                "behavior": "not executed; no entry-bound fixture in P3-05 pilot",
                "local_plugin_execution": False,
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }

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
            if identity["ecosystem"] == "nonebot":
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
    digest = write_json(args.report, final)
    print(json.dumps({"final_report":str(args.report),"report_file_sha256":digest,"manifest_sha256":manifest_sha,"counts":final["unit_counts"]},ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"Windows pilot stopped safely: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2)
