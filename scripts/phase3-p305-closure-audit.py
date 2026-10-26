from __future__ import annotations

import hashlib
import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.automation.selection import object_sha256


ACCEPTANCE = ROOT / "outputs/acceptance"
OUTPUT_PATH = ACCEPTANCE / "phase3-p305-overall-closure-20261026.json"

RECOVERY_PATH = ACCEPTANCE / "phase3-p301-recovery-manifest-20261020.json"
LINUX_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-linux-affected-v3-manifest-20261025.json"
LINUX_REPORT_PATH = ACCEPTANCE / "phase3-p305-linux-affected-v3-execution-resume6-20261025.json"
PILOT_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-pilot-manifest-v2-20261025.json"
PILOT_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-pilot-v2-execution-20261025.json"
NONEBOT_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-nonebot-evaluation-manifest-20261025.json"
NONEBOT_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-nonebot-evaluation-execution-20261025.json"
KOISHI_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-manifest-v1-20261026.json"
KOISHI_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-evaluation-execution-v1-20261026.json"
HTTP_V1_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-manifest-v1-20261026.json"
HTTP_V1_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-execution-v1-20261026.json"
HTTP_V3_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-manifest-v3-20261026.json"
HTTP_V3_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-execution-v3-20261026.json"
HTTP_V4_MANIFEST_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-manifest-v4-20261026.json"
HTTP_V4_REPORT_PATH = ACCEPTANCE / "phase3-p305-windows-koishi-http-followup-execution-v4-20261026.json"


class ClosureAuditError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_sidecar(path: Path, *, required: bool = True) -> str | None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        if required:
            raise ClosureAuditError(f"missing SHA-256 sidecar: {sidecar}")
        return None
    expected = sidecar.read_text(encoding="ascii").split()[0]
    actual = sha256_file(path)
    if expected != actual:
        raise ClosureAuditError(f"SHA-256 sidecar mismatch: {path}")
    return actual


def load_manifest(
    path: Path,
    expected_sha256: str | None = None,
    *,
    require_sidecar: bool = True,
    require_profile_lock: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_json(path)
    file_sha = verify_sidecar(path, required=require_sidecar) or sha256_file(path)
    canonical_sha = object_sha256({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if canonical_sha != manifest.get("manifest_sha256"):
        raise ClosureAuditError(f"manifest canonical SHA mismatch: {path}")
    if expected_sha256 is not None and canonical_sha != expected_sha256:
        raise ClosureAuditError(f"manifest identity differs from expected frozen SHA: {path}")
    profile_path = None
    profile_file_sha = None
    if require_profile_lock:
        profile_path = (ROOT / manifest["profile_lock_path"]).resolve(strict=True)
        profile_file_sha = verify_sidecar(profile_path)
        if profile_file_sha != manifest.get("profile_lock_sha256"):
            raise ClosureAuditError(f"manifest profile-lock file SHA mismatch: {path}")
    return manifest, {
        "path": path.relative_to(ROOT).as_posix(),
        "canonical_sha256": canonical_sha,
        "file_sha256": file_sha,
        "profile_lock_path": profile_path.relative_to(ROOT).as_posix() if profile_path else None,
        "profile_lock_sha256": profile_file_sha,
    }


def load_report(
    path: Path,
    manifest: dict[str, Any],
    *,
    manifest_path: Path | None = None,
    windows_schema: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    report = load_json(path)
    file_sha = verify_sidecar(path, required=False)
    if not windows_schema:
        expected_report_sha = object_sha256({key: value for key, value in report.items() if key != "report_sha256"})
        if expected_report_sha != report.get("report_sha256"):
            raise ClosureAuditError("Linux final report self-hash mismatch")
    if (
        report.get("campaign_id") != manifest.get("campaign_id")
        or report.get("manifest_sha256") != manifest.get("manifest_sha256")
        or report.get("status") != "completed"
    ):
        raise ClosureAuditError(f"report is not closed against its manifest: {path}")
    if windows_schema:
        if manifest_path is None:
            raise ClosureAuditError(f"manifest path is required to verify Windows report: {path}")
        recorded_manifest_path = str(report.get("manifest_path", "")).replace("\\", "/")
        expected_manifest_path = manifest_path.relative_to(ROOT).as_posix()
        if recorded_manifest_path != expected_manifest_path:
            raise ClosureAuditError(f"report references a different manifest path: {path}")
        manifest_sha = sha256_file(manifest_path)
        if report.get("manifest_file_sha256") != manifest_sha:
            raise ClosureAuditError(f"report manifest file SHA mismatch: {path}")
        if report.get("profile_lock_sha256") != manifest.get("profile_lock_sha256"):
            raise ClosureAuditError(f"report profile-lock identity mismatch: {path}")
        expected = int(manifest.get("expected_units", 0))
        rows = report.get("results", [])
        result_ids = [row.get("result", {}).get("registry_entry_id") for row in rows]
        manifest_ids = [row["identity"]["registry_entry_id"] for row in manifest.get("selection", [])]
        if (
            report.get("settled_units") != expected
            or report.get("remaining_units") != 0
            or len(rows) != expected
            or len(set(result_ids)) != expected
            or set(result_ids) != set(manifest_ids)
        ):
            raise ClosureAuditError(f"report result identities do not exactly close the frozen manifest: {path}")
        cleanup = report.get("cleanup_confirmed", 0)
        unconfirmed = report.get("cleanup_unconfirmed", 0)
        if unconfirmed != 0:
            raise ClosureAuditError(f"report has unconfirmed cleanup: {path}")
        counts = report.get("unit_counts", {})
        if sum(counts.values()) != expected:
            raise ClosureAuditError(f"report status counts do not equal its denominator: {path}")
    else:
        expected = int(manifest.get("expected_units", 0))
        rows = report.get("plugin_reports", [])
        ids = [row.get("registry_entry_id") for row in rows]
        manifest_ids = [row["identity"]["registry_entry_id"] for row in manifest.get("selection", [])]
        if (
            report.get("denominator", {}).get("expected_units") != expected
            or report.get("denominator", {}).get("terminal_units") != expected
            or report.get("counts", {}).get("not_run") != 0
            or len(rows) != expected
            or len(set(ids)) != expected
            or set(ids) != set(manifest_ids)
        ):
            raise ClosureAuditError("Linux report does not close the frozen 2,206-unit manifest")
        cleanup = report.get("counts", {}).get("cleanup_confirmed", 0)
        unconfirmed = 0 if not report.get("counts", {}).get("infrastructure_blocker") else None
        counts = None
    return report, {
        "path": path.relative_to(ROOT).as_posix(),
        "file_sha256": file_sha or sha256_file(path),
        "status": report.get("status"),
        "settled_units": report.get("settled_units", report.get("denominator", {}).get("terminal_units")),
        "remaining_units": report.get("remaining_units", report.get("denominator", {}).get("expected_units", 0) - report.get("denominator", {}).get("terminal_units", 0)),
        "unit_counts": counts,
        "cleanup_confirmed": cleanup,
        "cleanup_unconfirmed": unconfirmed,
    }


def windows_counts(report: dict[str, Any]) -> Counter[str]:
    return Counter({status: int(count) for status, count in report["unit_counts"].items()})


def linux_unit_counts(report: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in report.get("plugin_reports", []):
        stages = row.get("old_to_new", {}).get("new_stage_statuses", {})
        values = list(stages.values())
        if "unknown" in values:
            status = "unknown"
        elif "failed" in values:
            status = "failed"
        elif stages.get("native") == "skipped":
            status = "skipped"
        elif stages and all(value == "passed" for value in values):
            status = "passed"
        else:
            raise ClosureAuditError(f"cannot derive Linux terminal status for {row.get('registry_entry_id')}: {stages}")
        counts[status] += 1
    if counts != Counter({"passed": 1299, "failed": 315, "skipped": 553, "unknown": 39}):
        raise ClosureAuditError(f"Linux derived unit counts differ from the saved queue reconciliation: {counts}")
    return counts


def result_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["result"]["registry_entry_id"]: row["result"] for row in report.get("results", [])}


def main(*, check: bool = False) -> int:
    recovery = load_json(RECOVERY_PATH)
    # The historical P3-01 source ledger predates sidecar hashes. Verify its
    # embedded canonical identity and retain the current file-byte hash here.
    recovery_file_sha = sha256_file(RECOVERY_PATH)
    recovery_canonical = object_sha256({key: value for key, value in recovery.items() if key != "manifest_sha256"})
    if recovery_canonical != recovery.get("manifest_sha256") or recovery_canonical != "f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e":
        raise ClosureAuditError("P3-01 recovery source manifest identity changed")

    linux_manifest, linux_manifest_record = load_manifest(
        LINUX_MANIFEST_PATH,
        "961786350b7c0ebdb990650d051203ad82452fff72ac291402a7f46a517173c8",
        require_sidecar=False,
        require_profile_lock=False,
    )
    linux_report, linux_report_record = load_report(LINUX_REPORT_PATH, linux_manifest, windows_schema=False)

    windows_specs = [
        ("pilot", PILOT_MANIFEST_PATH, PILOT_REPORT_PATH, "0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa"),
        ("nonebot", NONEBOT_MANIFEST_PATH, NONEBOT_REPORT_PATH, "6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914"),
        ("koishi", KOISHI_MANIFEST_PATH, KOISHI_REPORT_PATH, "143fed20ff0018c3e81adb932b4d5de8e2c1b88126ef8c8892cdf1df6aecdb98"),
        ("http_v1", HTTP_V1_MANIFEST_PATH, HTTP_V1_REPORT_PATH, "df01597f7b3710a478e28d2d2ab5d6002ccfb20b45291847c2b879aef1a7d199"),
        ("http_v3", HTTP_V3_MANIFEST_PATH, HTTP_V3_REPORT_PATH, "23ae0370ddc43f64931acb2d9a90a35e8359a67f5013b68521056374690f56fa"),
        ("http_v4", HTTP_V4_MANIFEST_PATH, HTTP_V4_REPORT_PATH, "6420a3e1edbee8d5eb380fb7070cab626a9abce0bca256761641d84a6da3e649"),
    ]
    manifests: dict[str, dict[str, Any]] = {}
    reports: dict[str, dict[str, Any]] = {}
    inventory: dict[str, Any] = {"linux": {"manifest": linux_manifest_record, "report": linux_report_record}}
    for key, manifest_path, report_path, expected_sha in windows_specs:
        manifest, manifest_record = load_manifest(manifest_path, expected_sha)
        report, report_record = load_report(report_path, manifest, manifest_path=manifest_path)
        manifests[key] = manifest
        reports[key] = report
        inventory[key] = {"manifest": manifest_record, "report": report_record}

    # The original Windows denominator is the disjoint pilot (20), NoneBot remainder (786),
    # and Koishi remainder (167). The HTTP follow-ups are additive re-attributions of six
    # Koishi identities and are not counted as new catalog entries.
    recovery_windows = {
        row["identity"]["registry_entry_id"]: row
        for row in recovery.get("selection", [])
        if row.get("identity", {}).get("platform") == "windows"
    }
    pilot_rows = manifests["pilot"].get("selection", [])
    nonebot_rows = manifests["nonebot"].get("selection", [])
    koishi_rows = manifests["koishi"].get("selection", [])
    cohort_rows = {"pilot": pilot_rows, "nonebot": nonebot_rows, "koishi": koishi_rows}
    cohort_ids = {
        name: {row["identity"]["registry_entry_id"] for row in rows}
        for name, rows in cohort_rows.items()
    }
    if (
        len(recovery_windows) != 973
        or len(pilot_rows) != 20
        or len(nonebot_rows) != 786
        or len(koishi_rows) != 167
        or cohort_ids["pilot"] & cohort_ids["nonebot"]
        or cohort_ids["pilot"] & cohort_ids["koishi"]
        or cohort_ids["nonebot"] & cohort_ids["koishi"]
        or set.union(*cohort_ids.values()) != set(recovery_windows)
    ):
        raise ClosureAuditError("Windows 973-unit disjoint source coverage does not reconcile")

    pilot_source_hashes = {
        row["source_identity_sha256"]
        for row in pilot_rows
    }
    pilot_nonebot_source_hashes = {
        row["source_identity_sha256"]
        for row in pilot_rows
        if row["identity"].get("ecosystem") == "nonebot"
    }
    pilot_koishi_source_hashes = {
        row["source_identity_sha256"]
        for row in pilot_rows
        if row["identity"].get("ecosystem") == "koishi"
    }
    nonebot_exclusions = set(manifests["nonebot"].get("exclusions", {}).get("source_identity_sha256", []))
    koishi_exclusions = set(manifests["koishi"].get("exclusions", {}).get("source_identity_sha256", []))
    if (
        len(pilot_source_hashes) != 20
        or len(pilot_nonebot_source_hashes) != 10
        or len(pilot_koishi_source_hashes) != 10
        or nonebot_exclusions != pilot_nonebot_source_hashes
        or koishi_exclusions != pilot_koishi_source_hashes
    ):
        raise ClosureAuditError("Windows pilot exclusions do not exactly bind the selected NoneBot/Koishi rows")

    v4_ids = {row["identity"]["registry_entry_id"] for row in manifests["http_v4"].get("selection", [])}
    koishi_result_map = result_map(reports["koishi"])
    v1_result_map = result_map(reports["http_v1"])
    v3_result_map = result_map(reports["http_v3"])
    v4_result_map = result_map(reports["http_v4"])
    if v4_ids != set(v1_result_map) or v4_ids != set(v3_result_map) or v4_ids != set(v4_result_map):
        raise ClosureAuditError("HTTP v1/v3/v4 result identities do not match the exact six follow-up units")
    for entry_id in v4_ids:
        base_result = koishi_result_map.get(entry_id, {})
        if base_result.get("status") != "skipped" or base_result.get("reason_code") != "required_host_service_unavailable":
            raise ClosureAuditError(f"HTTP follow-up target was not a root missing-service skip: {entry_id}")
        if v1_result_map[entry_id].get("status") != "failed" or v3_result_map[entry_id].get("status") != "failed":
            raise ClosureAuditError(f"superseded HTTP v1/v3 failures changed: {entry_id}")
    v4_follow_up = manifests["http_v4"].get("follow_up", {})
    if (
        v4_follow_up.get("prior_execution_v3", {}).get("manifest_sha256") != manifests["http_v3"].get("manifest_sha256")
        or v4_follow_up.get("prior_execution_v3", {}).get("report_file_sha256") != sha256_file(HTTP_V3_REPORT_PATH)
        or v4_follow_up.get("superseded_freeze_attempt", {}).get("validation_outcome") != "blocked_before_worker"
    ):
        raise ClosureAuditError("HTTP follow-up supersession chain does not bind v3 and the blocked v2 attempt")

    linux_counts = linux_unit_counts(linux_report)
    windows_pilot_counts = windows_counts(reports["pilot"])
    windows_nonebot_counts = windows_counts(reports["nonebot"])
    windows_koishi_base_counts = windows_counts(reports["koishi"])
    windows_http_v4_counts = windows_counts(reports["http_v4"])
    # Six base rows are superseded only in this derived current view; their frozen original
    # results remain unchanged in the original Koishi report.
    effective_koishi = windows_koishi_base_counts.copy()
    for entry_id in v4_ids:
        effective_koishi[koishi_result_map[entry_id]["status"]] -= 1
        effective_koishi[v4_result_map[entry_id]["status"]] += 1
    if any(value < 0 for value in effective_koishi.values()):
        raise ClosureAuditError("effective Koishi status projection produced a negative count")
    effective_koishi = +effective_koishi
    windows_effective = windows_pilot_counts + windows_nonebot_counts + effective_koishi
    expected_windows = Counter({"passed": 0, "failed": 373, "skipped": 586, "unknown": 14})
    if windows_effective != expected_windows or sum(windows_effective.values()) != 973:
        raise ClosureAuditError(f"effective Windows 973-unit outcomes do not reconcile: {windows_effective}")

    overall = linux_counts + windows_effective
    if overall != Counter({"passed": 1299, "failed": 688, "skipped": 1139, "unknown": 53}) or sum(overall.values()) != 3179:
        raise ClosureAuditError(f"cross-platform 3,179-unit ledger does not reconcile: {overall}")

    report = {
        "schema_version": 1,
        "kind": "phase3_p305_overall_closure_audit_v1",
        "task_id": "P3-05",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "completed_with_terminal_unknowns",
        "health": "blocked",
        "plugin_compatibility": "not_evaluated",
        "scope": {
            "linux_units": 2206,
            "windows_unique_units": 973,
            "cross_platform_unique_units": 3179,
            "windows_followup_resamples_not_added_to_denominator": 6,
            "all_required_units_terminal": True,
            "remaining_units": 0,
            "cleanup_unconfirmed": 0,
        },
        "effective_unit_counts": {
            "linux": dict(sorted(linux_counts.items())),
            "windows_pilot_20": dict(sorted(windows_pilot_counts.items())),
            "windows_nonebot_786": dict(sorted(windows_nonebot_counts.items())),
            "windows_koishi_167_original": dict(sorted(windows_koishi_base_counts.items())),
            "windows_koishi_167_after_http_v4_projection": dict(sorted(effective_koishi.items())),
            "windows_unique_973": dict(sorted(windows_effective.items())),
            "cross_platform_unique_3179": dict(sorted(overall.items())),
        },
        "campaigns": inventory,
        "windows_scope_reconciliation": {
            "source_manifest_path": RECOVERY_PATH.relative_to(ROOT).as_posix(),
            "source_manifest_canonical_sha256": recovery_canonical,
            "source_manifest_file_sha256": recovery_file_sha,
            "pilot_registry_entry_count": len(pilot_rows),
            "nonebot_registry_entry_count": len(nonebot_rows),
            "koishi_registry_entry_count": len(koishi_rows),
            "union_registry_entry_count": len(set.union(*cohort_ids.values())),
            "cohorts_disjoint_and_complete": True,
            "http_followup_registry_entry_ids": sorted(v4_ids),
            "http_followup_is_additive_re_attribution_only": True,
            "superseded_attempts_retained": [
                reports["http_v1"]["campaign_id"],
                manifests["http_v4"]["follow_up"]["superseded_freeze_attempt"]["campaign_id"],
                reports["http_v3"]["campaign_id"],
            ],
        },
        "unresolved_outcomes": {
            "linux_unknown": 39,
            "windows_pilot_unknown": 10,
            "windows_nonebot_preparation_unknown": 4,
            "windows_total_unknown": int(windows_effective["unknown"]),
            "remain_unknown": True,
            "behavior_gate": "not_evaluated",
        },
        "interpretation": [
            "P3-05 execution and attribution scope is closed because all 2,206 Linux units and all 973 unique Windows affected units have terminal ledger entries and no units remain open.",
            "The 39 Linux, 10 legacy Windows pilot, and 4 NoneBot preparation outcomes remain unknown; terminal accounting does not convert them to pass.",
            "The six HTTP follow-up results supersede only the current derived Koishi outcome for those same six identities; original Koishi and v1/v3 reports remain immutable.",
            "The Windows Sandbox profile is SAC Evaluation Audit Mode. This closure does not establish enforcement-mode acceptance or plugin behavior compatibility.",
            "Failed and skipped results remain failures/skips in the effective ledger; a completed P3-05 task is not a compatibility pass.",
        ],
    }
    report["report_sha256"] = object_sha256(report)
    raw = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    sidecar = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".sha256")
    if OUTPUT_PATH.exists() or sidecar.exists():
        if not check or not OUTPUT_PATH.is_file() or not sidecar.is_file():
            raise ClosureAuditError(f"refusing to overwrite closure evidence: {OUTPUT_PATH}")
        existing = load_json(OUTPUT_PATH)
        existing_file_sha = verify_sidecar(OUTPUT_PATH)
        existing_report_sha = object_sha256({key: value for key, value in existing.items() if key != "report_sha256"})
        if existing_report_sha != existing.get("report_sha256"):
            raise ClosureAuditError("existing overall closure report self-hash mismatch")
        stable_existing = {key: value for key, value in existing.items() if key not in {"generated_at_utc", "report_sha256"}}
        stable_current = {key: value for key, value in report.items() if key not in {"generated_at_utc", "report_sha256"}}
        if stable_existing != stable_current:
            raise ClosureAuditError("existing overall closure report differs from current frozen-input reconciliation")
        print(json.dumps({"report_path": OUTPUT_PATH.relative_to(ROOT).as_posix(), "report_sha256": existing_report_sha, "file_sha256": existing_file_sha, "status": existing["status"], "verified": True}, ensure_ascii=False, indent=2))
        return 0
    if check:
        raise ClosureAuditError(f"no saved closure evidence to verify: {OUTPUT_PATH}")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(raw)
    file_sha = hashlib.sha256(raw).hexdigest()
    sidecar.write_text(f"{file_sha}  {OUTPUT_PATH.name}\n", encoding="ascii")
    print(json.dumps({"report_path": OUTPUT_PATH.relative_to(ROOT).as_posix(), "report_sha256": report["report_sha256"], "file_sha256": file_sha, "status": report["status"], "counts": report["effective_unit_counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="Reconcile the frozen P3-05 Linux and Windows affected ledgers.")
        parser.add_argument("--check", action="store_true", help="verify saved closure evidence without writing")
        raise SystemExit(main(check=parser.parse_args().check))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, ClosureAuditError) as error:
        print(f"P3-05 closure audit failed: {type(error).__name__}: {error}")
        traceback.print_exc()
        raise SystemExit(2)
