from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.selection import object_sha256, validate_manifest


DEFAULT_SOURCE = ROOT / "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
DEFAULT_PILOT = ROOT / "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json"
DEFAULT_PILOT_REPORT = ROOT / "outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json"
DEFAULT_PROFILE_LOCK = ROOT / "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-profile-lock-v2-20261025.json"
DEFAULT_ADMISSION = ROOT / "outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json"
DEFAULT_POLICY_REPORT = ROOT / "outputs/acceptance/phase3-p305-windows-default-evaluation-preflight-20261025/preflight-report.json"
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-manifest-20261025.json"
EVALUATION_BASE = "{1283ac0f-fff1-49ae-ada1-8a933130cad6}"
EVALUATION_SUPPLEMENTAL = "{2678656c-05ef-481f-bc5b-ebd8c991502d}"
EVALUATION_BASE_OPTIONS = "0xb18d2714"
EVALUATION_SUPPLEMENTAL_OPTIONS = "0xd1892304"
RUNTIME_PROFILE_ID = "windows-cpython-3.12.10-official-embed-amd64-psf-signed"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def require_sidecar(path: Path) -> None:
    sidecar = path.with_name(f"{path.name}.sha256")
    if not sidecar.is_file():
        raise ValueError(f"missing SHA-256 sidecar: {sidecar}")
    expected = sidecar.read_text(encoding="ascii").split()[0]
    if file_sha256(path) != expected:
        raise ValueError(f"SHA-256 sidecar mismatch: {path}")


def checked_repo_path(value: str) -> Path:
    path = (ROOT / value).resolve(strict=True)
    path.relative_to(ROOT)
    return path


def generate(args: argparse.Namespace) -> dict[str, Any]:
    source_path = args.source.resolve(strict=True)
    pilot_path = args.pilot.resolve(strict=True)
    pilot_report_path = args.pilot_report.resolve(strict=True)
    profile_path = args.profile_lock.resolve(strict=True)
    admission_path = args.admission.resolve(strict=True)
    policy_path = args.policy_report.resolve(strict=True)
    output_path = args.output.resolve()
    output_path.relative_to(ROOT)
    output_sidecar = output_path.with_name(f"{output_path.name}.sha256")
    if output_path.exists() or output_sidecar.exists():
        raise ValueError(f"refusing to overwrite an existing frozen manifest: {output_path}")

    source = read_json(source_path)
    validate_manifest(source, verify_source_files=True)
    if object_sha256({key: value for key, value in source.items() if key != "manifest_sha256"}) != source.get("manifest_sha256"):
        raise ValueError("source recovery manifest canonical hash mismatch")
    require_sidecar(pilot_path)
    require_sidecar(pilot_report_path)
    require_sidecar(profile_path)
    require_sidecar(policy_path)

    pilot = read_json(pilot_path)
    if object_sha256({key: value for key, value in pilot.items() if key != "manifest_sha256"}) != pilot.get("manifest_sha256"):
        raise ValueError("terminal Windows pilot canonical hash mismatch")
    if pilot.get("campaign_id") != "p3-recovery-windows-pilot-v2-9666ebb4609f" or pilot.get("expected_units") != 20:
        raise ValueError("unexpected terminal Windows pilot identity")
    pilot_report = read_json(pilot_report_path)
    if pilot_report.get("campaign_id") != pilot.get("campaign_id") or pilot_report.get("manifest_sha256") != pilot.get("manifest_sha256"):
        raise ValueError("terminal pilot report is not bound to the frozen pilot")
    if pilot_report.get("status") != "completed" or pilot_report.get("settled_units") != 20:
        raise ValueError("terminal Windows pilot is not closed")
    if pilot_report.get("unit_counts") != {"passed": 0, "failed": 2, "skipped": 8, "unknown": 10}:
        raise ValueError("terminal Windows pilot outcomes changed")

    admission = read_json(admission_path)
    if admission.get("status") != "admitted" or admission.get("runtime_profile", {}).get("profile_id") != RUNTIME_PROFILE_ID:
        raise ValueError("P3-04 signed Python runtime profile is not admitted")
    admission_sha = file_sha256(admission_path)

    policy = read_json(policy_path)
    policy_profile = policy.get("sac_evaluation_baseline", {})
    if policy.get("result", "").startswith("default WSB configuration") is not True:
        raise ValueError("default Windows Sandbox Evaluation profile was not confirmed")
    if policy.get("scope", {}).get("catalog_plugins_executed") is not False or policy.get("scope", {}).get("code_integrity_policy_changed") is not False:
        raise ValueError("policy preflight must be diagnostic-only and must not change policy")
    if policy.get("codeintegrity_event_counts") != {"3076": 0, "3077": 0, "3089": 0, "3099": 18}:
        raise ValueError("policy preflight CodeIntegrity event counts changed")
    if (
        policy_profile.get("base_policy_guid") != EVALUATION_BASE
        or policy_profile.get("base_options") != EVALUATION_BASE_OPTIONS
        or policy_profile.get("base_audit_mode_enabled") is not True
        or policy_profile.get("flight_supplemental_guid") != EVALUATION_SUPPLEMENTAL
        or policy_profile.get("flight_supplemental_options") != EVALUATION_SUPPLEMENTAL_OPTIONS
        or policy_profile.get("supplemental_audit_mode_enabled") is not True
    ):
        raise ValueError("default Sandbox Evaluation policy identities/options do not match the accepted profile")
    policy_sha = file_sha256(policy_path)
    policy_dir = policy_path.parent
    policy_evidence = {}
    for name in ("guest-policy-inventory.json", "CodeIntegrity-Operational.evtx", "codeintegrity-events.json"):
        evidence_path = policy_dir / name
        require_sidecar(evidence_path)
        policy_evidence[name] = {
            "path": evidence_path.relative_to(ROOT).as_posix(),
            "sha256": file_sha256(evidence_path),
        }

    profile_lock = read_json(profile_path)
    profile_sha = file_sha256(profile_path)
    if profile_lock.get("snapshot_id") != source.get("snapshot_id"):
        raise ValueError("profile lock is for a different frozen catalog snapshot")
    entries = {row.get("registry_entry_id"): row for row in profile_lock.get("entries", []) if isinstance(row, dict)}
    old_source_hashes = {
        row.get("source_identity_sha256")
        for row in pilot.get("selection", [])
        if isinstance(row, dict) and row.get("identity", {}).get("ecosystem") == "nonebot"
    }
    if len(old_source_hashes) != 10 or None in old_source_hashes:
        raise ValueError("pilot exclusion set is incomplete")
    source_rows = [
        row for row in source.get("selection", [])
        if row.get("identity", {}).get("platform") == "windows"
        and row.get("identity", {}).get("ecosystem") == "nonebot"
    ]
    if len(source_rows) != 796:
        raise ValueError(f"unexpected frozen Windows NoneBot denominator: {len(source_rows)}")
    selected_rows = [row for row in source_rows if row.get("identity_sha256") not in old_source_hashes]
    if len(selected_rows) != 786:
        raise ValueError(f"remaining NoneBot denominator changed: {len(selected_rows)}")

    selection: list[dict[str, Any]] = []
    for row in selected_rows:
        old_identity = row["identity"]
        profile_entry = entries.get(old_identity["registry_entry_id"])
        windows_profile = (profile_entry or {}).get("profiles", {}).get("windows-x86_64", {})
        if windows_profile.get("status") != "locked":
            raise ValueError(f"Windows 3.12.10 profile is not locked: {old_identity['registry_entry_id']}")
        if windows_profile.get("runtime_profile_id") != RUNTIME_PROFILE_ID:
            raise ValueError(f"unexpected admitted runtime profile: {old_identity['registry_entry_id']}")
        if windows_profile.get("artifact_sha256") != old_identity.get("artifact_sha256"):
            raise ValueError(f"artifact identity changed: {old_identity['registry_entry_id']}")
        dependency_lock = windows_profile.get("dependency_lock")
        if not isinstance(dependency_lock, dict) or not isinstance(dependency_lock.get("sha256"), str):
            raise ValueError(f"frozen dependency lock is missing: {old_identity['registry_entry_id']}")
        identity = dict(old_identity)
        identity.update({
            "profile_id": windows_profile["profile_id"],
            "profile_lock_sha256": profile_sha,
            "dependency_lock_sha256": dependency_lock["sha256"],
        })
        selection.append({
            "selection_kind": "affected_windows_remaining_nonebot",
            "identity": identity,
            "identity_sha256": object_sha256(identity),
            "source_identity": old_identity,
            "source_identity_sha256": row["identity_sha256"],
            "source_selection_reasons": row["selection_reasons"],
            "source": row,
            "old_to_new_identity": {
                "source_identity_sha256": row["identity_sha256"],
                "profile_id_changed": old_identity.get("profile_id") != identity.get("profile_id"),
                "profile_lock_sha256_changed": old_identity.get("profile_lock_sha256") != profile_sha,
                "dependency_lock_sha256_changed": old_identity.get("dependency_lock_sha256") != dependency_lock["sha256"],
            },
            "probe_profile": {
                "kind": "nonebot-windows-offline-native-load-v1",
                "profile_lock_sha256": profile_sha,
                "runtime_admission_sha256": admission_sha,
                "sandbox_policy_profile_id": "windows-sandbox-default-sac-evaluation-v1",
            },
        })

    old_report_sha = file_sha256(pilot_report_path)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract": "phase3-windows-affected-nonebot-v1",
        "kind": "phase3_p305_windows_affected_manifest_v1",
        "campaign_id": "p3-recovery-windows-nonebot-evaluation-20261025",
        "snapshot_id": source["snapshot_id"],
        "platform": "windows",
        "ecosystem": "nonebot",
        "expected_units": len(selection),
        "required_stage_positions": len(selection) * 3,
        "stage_scope": ["install", "native", "cleanup"],
        "probe_semantics_version": 3,
        "source_recovery_manifest_path": source_path.relative_to(ROOT).as_posix(),
        "source_recovery_manifest_sha256": source["manifest_sha256"],
        "source_recovery_manifest_file_sha256": file_sha256(source_path),
        "artifact_lock_path": "catalogs/p2-41649744e057d61031d5/artifacts.lock.json",
        "artifact_lock_sha256": file_sha256(ROOT / "catalogs/p2-41649744e057d61031d5/artifacts.lock.json"),
        "profile_lock_path": profile_path.relative_to(ROOT).as_posix(),
        "profile_lock_sha256": profile_sha,
        "runtime_admission_path": admission_path.relative_to(ROOT).as_posix(),
        "runtime_admission_sha256": admission_sha,
        "windows_node_runtime": {
            "version": "24.20.0",
            "archive_sha256": "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
            "node_executable_sha256": "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5",
        },
        "worker_policy": {
            "sandbox_memory_mb": 8192,
            "cpu_cores": 2,
            "process_limit": 256,
            "network": "disabled",
            "input": "read-only",
            "pip_install": "offline --require-hashes --only-binary",
            "windows_sandbox_required": True,
            "sandbox_policy_profile_id": "windows-sandbox-default-sac-evaluation-v1",
        },
        "sandbox_policy_profile": {
            "profile_id": "windows-sandbox-default-sac-evaluation-v1",
            "source": "default Windows Sandbox configuration; no App Control policy is installed or modified by this campaign",
            "mode": "Smart App Control Evaluation (Audit Mode)",
            "preflight_report_path": policy_path.relative_to(ROOT).as_posix(),
            "preflight_report_file_sha256": policy_sha,
            "preflight_evidence": policy_evidence,
            "guest_os_build": policy.get("guest", {}).get("BuildNumber"),
            "base_policy_guid": EVALUATION_BASE,
            "base_options": EVALUATION_BASE_OPTIONS,
            "base_audit_mode_enabled": True,
            "flight_supplemental_guid": EVALUATION_SUPPLEMENTAL,
            "flight_supplemental_options": EVALUATION_SUPPLEMENTAL_OPTIONS,
            "supplemental_audit_mode_enabled": True,
            "mode_claim": "evaluation-scoped load evidence only; not enforcement-mode acceptance",
        },
        "exclusions": {
            "terminal_campaign_id": pilot["campaign_id"],
            "terminal_manifest_sha256": pilot["manifest_sha256"],
            "terminal_report_path": pilot_report_path.relative_to(ROOT).as_posix(),
            "terminal_report_file_sha256": old_report_sha,
            "source_identity_sha256": sorted(old_source_hashes),
            "excluded_units": len(old_source_hashes),
            "outcomes_immutable": True,
        },
        "selection_algorithm": "retain source recovery manifest order for Windows NoneBot stage_unknown:install rows; exclude the 10 source identities already terminal in the immutable Windows pilot",
        "profile_binding_correction": "each remaining entry is rebound to its locked CPython 3.12.10 signed runtime dependency lock and the admitted Windows Sandbox Evaluation policy profile before dispatch",
        "selection": selection,
    }
    manifest["manifest_sha256"] = object_sha256(manifest)
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    output_sidecar.write_text(f"{hashlib.sha256(raw).hexdigest()}  {output_path.name}\n", encoding="ascii")
    return {
        "manifest_path": output_path.relative_to(ROOT).as_posix(),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_file_sha256": hashlib.sha256(raw).hexdigest(),
        "expected_units": len(selection),
        "required_stage_positions": manifest["required_stage_positions"],
        "excluded_terminal_units": len(old_source_hashes),
        "profile_lock_sha256": profile_sha,
        "policy_profile_id": manifest["sandbox_policy_profile"]["profile_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the remaining P3-05 Windows NoneBot affected campaign.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--pilot", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--pilot-report", type=Path, default=DEFAULT_PILOT_REPORT)
    parser.add_argument("--profile-lock", type=Path, default=DEFAULT_PROFILE_LOCK)
    parser.add_argument("--admission", type=Path, default=DEFAULT_ADMISSION)
    parser.add_argument("--policy-report", type=Path, default=DEFAULT_POLICY_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args()
    try:
        result = generate(options)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Windows affected manifest refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
