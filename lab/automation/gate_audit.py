"""Build P3-10 denominators, evidence manifest, gate rulings, and matrix.

This audit re-derives the P3-05 selected-unit sets from frozen manifests and
reconciles their terminal outcomes. It records engineering delivery separately
from health, behavior compatibility, and field acceptance.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .behavior_campaign import ROOT, behavior_accounting_matches_frozen, build_behavior_accounting_report
from .evidence_hash import matches_text_sha256


CATALOG = "catalogs/p2-41649744e057d61031d5/catalog.json"
RECOVERY = "outputs/acceptance/phase3-p301-recovery-manifest-20261020.json"
LINUX_MANIFEST = "outputs/acceptance/phase3-p305-linux-affected-v3-manifest-20261025.json"
LINUX_REPORT = "outputs/acceptance/phase3-p305-linux-affected-v3-execution-resume6-20261025.json"
PILOT_MANIFEST = "outputs/acceptance/phase3-p305-windows-pilot-manifest-v2-20261025.json"
PILOT_REPORT = "outputs/acceptance/phase3-p305-windows-pilot-v2-execution-20261025.json"
NONEBOT_MANIFEST = "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-manifest-20261025.json"
NONEBOT_REPORT = "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-execution-20261025.json"
KOISHI_MANIFEST = "outputs/acceptance/phase3-p305-windows-koishi-evaluation-manifest-v1-20261026.json"
KOISHI_REPORT = "outputs/acceptance/phase3-p305-windows-koishi-evaluation-execution-v1-20261026.json"
HTTP_V1_MANIFEST = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-manifest-v1-20261026.json"
HTTP_V1_REPORT = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-execution-v1-20261026.json"
HTTP_V3_MANIFEST = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-manifest-v3-20261026.json"
HTTP_V3_REPORT = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-execution-v3-20261026.json"
HTTP_V4_MANIFEST = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-manifest-v4-20261026.json"
HTTP_V4_REPORT = "outputs/acceptance/phase3-p305-windows-koishi-http-followup-execution-v4-20261026.json"
P305_CLOSURE = "outputs/acceptance/phase3-p305-overall-closure-20261026.json"
P304_WINDOWS = "outputs/acceptance/phase3-p304-windows-admission-python31210-20261025.json"
WINDOWS_NONEBOT_PROFILE_LOCK = "outputs/acceptance/phase3-p305-windows-nonebot-evaluation-profile-lock-v2-20261025.json"
WINDOWS_KOISHI_PROFILE_LOCK = "outputs/acceptance/phase3-p305-windows-koishi-evaluation-profile-lock-v1-20261026.json"
P308_LINUX_ADMISSION = "outputs/acceptance/phase3-p308-linux-admission-20261026.json"
P308_LINUX_PROBES = "outputs/acceptance/phase3-p308-linux-worker-probes-20261026.json"
P308_BEHAVIOR = "outputs/acceptance/phase3-p308-behavior-accounting-20261026.json"
P306_SUITE = "contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json"
P307_LEGACY = "outputs/acceptance/phase3-p307-linux-legacy-20261020.json"
P309_CLEAN = "outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.json"
P309_CLEAN_LOG = "outputs/acceptance/phase3-p309-linux-self-test-clean-checkout-20261020.log"
P309_TRAP = "outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.json"
P309_TRAP_LOG = "outputs/acceptance/phase3-p309-linux-controlled-trap-check-20261020.log"
P309_INTERRUPT = "outputs/acceptance/phase3-p309-linux-controlled-interrupt-check-20261020.json"
P309_INTERRUPT_LOG = "outputs/acceptance/phase3-p309-linux-controlled-interrupt-check-20261020.log"
P310_MATRIX = "outputs/acceptance/phase3-p310-compatibility-matrix-20261026.md"
MANIFEST_IDENTITIES = {
    RECOVERY: "f084778b6cfee271876ba6493e039fc2a288e0295a7a0fe9353f0d6f2074416e",
    LINUX_MANIFEST: "961786350b7c0ebdb990650d051203ad82452fff72ac291402a7f46a517173c8",
    PILOT_MANIFEST: "0796fad72d14f0e84cfd91b77e63177873a26510dfa3adee55c9a3298feed4fa",
    NONEBOT_MANIFEST: "6c5ffb197dddc1c135117a610560a74cb2bfe669d720dc30bad7e537216dc914",
    KOISHI_MANIFEST: "143fed20ff0018c3e81adb932b4d5de8e2c1b88126ef8c8892cdf1df6aecdb98",
    HTTP_V1_MANIFEST: "df01597f7b3710a478e28d2d2ab5d6002ccfb20b45291847c2b879aef1a7d199",
    HTTP_V3_MANIFEST: "23ae0370ddc43f64931acb2d9a90a35e8359a67f5013b68521056374690f56fa",
    HTTP_V4_MANIFEST: "6420a3e1edbee8d5eb380fb7070cab626a9abce0bca256761641d84a6da3e649",
}

SOURCE_PATHS = [
    CATALOG, "catalogs/p2-41649744e057d61031d5/profiles.lock.json",
    RECOVERY, LINUX_MANIFEST, LINUX_REPORT,
    PILOT_MANIFEST, PILOT_REPORT, NONEBOT_MANIFEST, NONEBOT_REPORT,
    KOISHI_MANIFEST, KOISHI_REPORT,
    HTTP_V1_MANIFEST, HTTP_V1_REPORT, HTTP_V3_MANIFEST, HTTP_V3_REPORT,
    HTTP_V4_MANIFEST, HTTP_V4_REPORT, P305_CLOSURE, P304_WINDOWS,
    WINDOWS_NONEBOT_PROFILE_LOCK, WINDOWS_KOISHI_PROFILE_LOCK,
    P308_LINUX_ADMISSION, P308_LINUX_PROBES, P308_BEHAVIOR, P306_SUITE,
    P307_LEGACY, P309_CLEAN, P309_CLEAN_LOG, P309_TRAP, P309_TRAP_LOG,
    P309_INTERRUPT, P309_INTERRUPT_LOG,
]


class GateAuditError(ValueError):
    """A frozen source no longer satisfies the P3-10 audit contract."""


def _read(relative: str) -> dict[str, Any]:
    try:
        value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateAuditError(f"cannot read {relative}: {error}") from error
    if not isinstance(value, dict):
        raise GateAuditError(f"expected JSON object: {relative}")
    return value


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record(relative: str) -> dict[str, Any]:
    path = ROOT / relative
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise GateAuditError(f"missing evidence source {relative}: {error}") from error
    row: dict[str, Any] = {"path": relative, "file_sha256": _sha(raw), "size_bytes": len(raw)}
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="ascii").strip().split()[0]
        if not matches_text_sha256(raw, expected):
            raise GateAuditError(f"sidecar mismatch for {relative}")
        row["sidecar_path"] = sidecar.relative_to(ROOT).as_posix()
        row["sidecar_sha256"] = _sha(sidecar.read_bytes())
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("manifest_sha256", "report_sha256", "canonical_sha256"):
            value = parsed.get(key)
            if isinstance(value, str):
                row[key] = value
                break
    return row


def _identity(row: dict[str, Any]) -> tuple[str, str, str]:
    identity = row.get("identity", row)
    platform = str(identity.get("platform", "")).lower()
    platform = {"windows-x86_64": "windows", "linux-x86_64": "linux"}.get(platform, platform)
    fields = (identity.get("snapshot_id"), identity.get("registry_entry_id"), platform)
    if not all(isinstance(value, str) and value for value in fields):
        raise GateAuditError(f"invalid unit identity: {identity}")
    return fields  # type: ignore[return-value]


def _selection(path: str) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[str, Any]]:
    doc = _read(path)
    rows = doc.get("selection")
    if not isinstance(rows, list):
        raise GateAuditError(f"manifest selection missing: {path}")
    mapped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        identity = _identity(row)
        if identity in mapped:
            raise GateAuditError(f"duplicate selected unit in {path}: {identity}")
        mapped[identity] = row
    return mapped, doc


def _verify_manifest_identity(path: str, expected: str) -> dict[str, Any]:
    doc = _read(path)
    actual = _sha(_canonical({key: value for key, value in doc.items() if key != "manifest_sha256"}))
    if doc.get("manifest_sha256") != expected or actual != expected:
        raise GateAuditError(f"frozen manifest identity mismatch: {path}")
    return doc


def _linux_status(row: dict[str, Any]) -> tuple[str, str]:
    statuses = row.get("old_to_new", {}).get("new_stage_statuses", {})
    values = list(statuses.values())
    if "unknown" in values:
        status = "unknown"
    elif "failed" in values:
        status = "failed"
    elif statuses.get("native") == "skipped":
        status = "skipped"
    elif values and all(value == "passed" for value in values):
        status = "passed"
    else:
        raise GateAuditError(f"cannot derive Linux terminal status for {row.get('registry_entry_id')}")
    stage_summary = ";".join(f"{stage}={value}" for stage, value in sorted(statuses.items()))
    return status, stage_summary


def _windows_results(path: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    report = _read(path)
    mapped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in report.get("results", []):
        result = item.get("result", {})
        identity = (str(result.get("snapshot_id") or item.get("selection_identity", {}).get("snapshot_id") or "p2-41649744e057d61031d5"), str(result.get("registry_entry_id", "")), "windows")
        if not identity[1] or identity in mapped:
            raise GateAuditError(f"invalid or duplicate Windows result in {path}: {identity}")
        mapped[identity] = result
    return mapped


def _count_status(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["status"] for row in rows)
    return {status: int(counts[status]) for status in ("passed", "failed", "skipped", "unknown")}


def _windows_cleanup_status(result: dict[str, Any]) -> str:
    worker = result.get("worker")
    if isinstance(worker, dict) and isinstance(worker.get("cleanup_confirmed"), bool):
        return "confirmed" if worker["cleanup_confirmed"] else "unconfirmed"
    cleanup = result.get("cleanup")
    if isinstance(cleanup, dict):
        return "confirmed" if cleanup.get("status") == "passed" else "unconfirmed"
    if result.get("attempted") is False:
        return "not_applicable_before_worker_start"
    return "unconfirmed"


def build_gate_audit() -> tuple[dict[str, Any], str]:
    """Recompute the P3 denominators and return audit JSON plus matrix Markdown."""
    catalog = _read(CATALOG)
    recovery = _read(RECOVERY)
    linux_manifest_rows, linux_manifest = _selection(LINUX_MANIFEST)
    pilot_rows, _ = _selection(PILOT_MANIFEST)
    nonebot_rows, _ = _selection(NONEBOT_MANIFEST)
    koishi_rows, _ = _selection(KOISHI_MANIFEST)
    http_v1_rows, _ = _selection(HTTP_V1_MANIFEST)
    http_v3_rows, _ = _selection(HTTP_V3_MANIFEST)
    http_v4_rows, _ = _selection(HTTP_V4_MANIFEST)
    recovery_rows = recovery.get("selection", [])
    recovery_units = {_identity(row) for row in recovery_rows}
    linux_ids = set(linux_manifest_rows)
    for manifest_path, expected_sha in MANIFEST_IDENTITIES.items():
        _verify_manifest_identity(manifest_path, expected_sha)
    windows_cohorts = [set(pilot_rows), set(nonebot_rows), set(koishi_rows)]
    windows_union = set.union(*windows_cohorts)
    if any(windows_cohorts[left] & windows_cohorts[right] for left in range(3) for right in range(left + 1, 3)):
        raise GateAuditError("Windows pilot/NoneBot/Koishi cohorts overlap")
    if len(linux_ids) != 2206 or len(windows_union) != 973 or linux_ids & windows_union:
        raise GateAuditError("P3-05 source cohort denominators or disjointness changed")
    if recovery_units != linux_ids | windows_union or len(recovery_units) != 3179:
        raise GateAuditError("P3-01 recovery selection does not equal the P3-05 cohort union")
    koishi_ids = set(koishi_rows)
    if not (http_v1_rows.keys() <= koishi_ids and http_v3_rows.keys() <= koishi_ids and http_v4_rows.keys() <= koishi_ids):
        raise GateAuditError("Koishi HTTP follow-up selection escaped its frozen 167-unit base cohort")
    if not (set(http_v1_rows) == set(http_v3_rows) == set(http_v4_rows)) or len(http_v4_rows) != 6:
        raise GateAuditError("Koishi HTTP follow-up identities do not match")

    p305 = _read(P305_CLOSURE)
    p305_body = {key: value for key, value in p305.items() if key != "report_sha256"}
    if _sha(_canonical(p305_body)) != p305.get("report_sha256"):
        raise GateAuditError("P3-05 closure report canonical digest is invalid")
    if p305.get("scope", {}).get("cross_platform_unique_units") != len(recovery_units):
        raise GateAuditError("P3-05 closure denominator does not match independent manifest union")

    linux_report = _read(LINUX_REPORT)
    linux_results = {("p2-41649744e057d61031d5", row["registry_entry_id"], "linux"): row for row in linux_report.get("plugin_reports", [])}
    if set(linux_results) != linux_ids:
        raise GateAuditError("Linux execution rows do not exactly cover the frozen Linux manifest")
    matrix_rows: list[dict[str, Any]] = []
    for key, selected in sorted(linux_manifest_rows.items()):
        result = linux_results[key]
        status, reason = _linux_status(result)
        identity = selected["identity"]
        matrix_rows.append({
            "snapshot_id": key[0], "registry_entry_id": key[1], "platform": key[2],
            "ecosystem": identity.get("ecosystem", result.get("ecosystem", "")),
            "package": identity.get("package", result.get("package", "")),
            "version": identity.get("version", result.get("version", "")),
            "status": status, "reason": reason or "", "cleanup_status": "confirmed" if result.get("cleanup_confirmed") is True else "unconfirmed",
            "source_campaign": "p3-recovery-linux-842f71712e2c", "source_report": LINUX_REPORT,
        })

    pilot_results = _windows_results(PILOT_REPORT)
    nonebot_results = _windows_results(NONEBOT_REPORT)
    koishi_results = _windows_results(KOISHI_REPORT)
    http_v4_results = _windows_results(HTTP_V4_REPORT)
    if set(pilot_results) != set(pilot_rows) or set(nonebot_results) != set(nonebot_rows) or set(koishi_results) != set(koishi_rows):
        raise GateAuditError("Windows result identities do not exactly match the three frozen source cohorts")
    if set(http_v4_results) != set(http_v4_rows):
        raise GateAuditError("HTTP v4 follow-up results do not exactly match their frozen selection")
    source_by_id: dict[tuple[str, str, str], tuple[dict[str, Any], str, dict[str, Any]]] = {}
    for mapping, result_map, report_path, campaign in (
        (pilot_rows, pilot_results, PILOT_REPORT, "p3-recovery-windows-pilot-v2-9666ebb4609f"),
        (nonebot_rows, nonebot_results, NONEBOT_REPORT, "p3-recovery-windows-nonebot-evaluation-20261025"),
        (koishi_rows, koishi_results, KOISHI_REPORT, "p3-recovery-windows-koishi-evaluation-v1-20261026"),
    ):
        for key, selected in mapping.items():
            source_by_id[key] = (selected, report_path, result_map[key])
    for key, result in http_v4_results.items():
        prior = source_by_id[key]
        if prior[2].get("status") != "skipped" or prior[2].get("reason_code") != "required_host_service_unavailable":
            raise GateAuditError(f"HTTP v4 re-attribution source is not the expected missing-service skip: {key}")
        source_by_id[key] = (prior[0], HTTP_V4_REPORT, result)
    for key, (selected, report_path, result) in sorted(source_by_id.items()):
        identity = selected.get("identity", selected)
        matrix_rows.append({
            "snapshot_id": key[0], "registry_entry_id": key[1], "platform": key[2],
            "ecosystem": identity.get("ecosystem", result.get("ecosystem", "")),
            "package": identity.get("package", result.get("package", "")),
            "version": identity.get("version", result.get("version", "")),
            "status": result["status"], "reason": result.get("reason_code") or "",
            "cleanup_status": _windows_cleanup_status(result),
            "source_campaign": result.get("worker", {}).get("campaign_id") if isinstance(result.get("worker"), dict) else report_path,
            "source_report": report_path,
        })
    matrix_rows.sort(key=lambda row: (row["platform"], row["ecosystem"], row["registry_entry_id"]))
    matrix_ids = {(row["snapshot_id"], row["registry_entry_id"], row["platform"]) for row in matrix_rows}
    if len(matrix_rows) != 3179 or len(matrix_ids) != 3179 or matrix_ids != recovery_units:
        raise GateAuditError("materialized P3-05 compatibility matrix does not contain 3,179 unique units")

    catalog_entries = catalog.get("entries", [])
    catalog_count = len(catalog_entries)
    if catalog.get("snapshot_id") != "p2-41649744e057d61031d5" or catalog_count != 5690:
        raise GateAuditError("frozen catalog denominator changed")
    full_entry_os_count = catalog_count * 2
    behavior = _read(P308_BEHAVIOR)
    rebuilt_behavior = build_behavior_accounting_report()
    if not behavior_accounting_matches_frozen(rebuilt_behavior, behavior):
        raise GateAuditError("P3-08 accounting report is stale against its frozen sources")
    behavior_positions = behavior.get("positions", [])
    if len(behavior_positions) != 120 or behavior.get("scope", {}).get("planned_attempt_slots") != 360:
        raise GateAuditError("P3-08 denominator differs from 120 positions / 360 attempts")
    if any(attempt.get("status") != "not_dispatched" for position in behavior_positions for attempt in position.get("attempts", [])):
        raise GateAuditError("P3-08 report contains an unaccounted or dispatched behavior attempt")

    matrix_counts = _count_status(matrix_rows)
    closure_counts = p305.get("effective_unit_counts", {}).get("cross_platform_unique_3179", {})
    if matrix_counts != {key: int(value) for key, value in closure_counts.items()}:
        raise GateAuditError(f"matrix unit outcomes do not match P3-05 closure report: {matrix_counts}")
    linux_counts = _count_status([row for row in matrix_rows if row["platform"] == "linux"])
    windows_counts = _count_status([row for row in matrix_rows if row["platform"] == "windows"])
    if linux_counts != {"passed": 1299, "failed": 315, "skipped": 553, "unknown": 39}:
        raise GateAuditError(f"Linux P3-05 outcomes changed: {linux_counts}")
    if windows_counts != {"passed": 0, "failed": 373, "skipped": 586, "unknown": 14}:
        raise GateAuditError(f"Windows P3-05 outcomes changed: {windows_counts}")
    cleanup_counts = Counter(row["cleanup_status"] for row in matrix_rows)
    if cleanup_counts["unconfirmed"] != 0 or cleanup_counts["confirmed"] != 3169 or cleanup_counts["not_applicable_before_worker_start"] != 10:
        raise GateAuditError(f"P3-05 cleanup projection changed: {dict(cleanup_counts)}")

    linux_admission = _read(P308_LINUX_ADMISSION)
    linux_probes = _read(P308_LINUX_PROBES)
    windows_admission = _read(P304_WINDOWS)
    p307 = _read(P307_LEGACY)
    p309_clean = _read(P309_CLEAN)
    p309_trap = _read(P309_TRAP)
    p309_interrupt = _read(P309_INTERRUPT)
    if linux_admission.get("status") != "admitted" or linux_admission.get("capabilities", {}).get("claim_allowed") is not True:
        raise GateAuditError("Linux Worker admission changed")
    if len(linux_probes.get("results", [])) != 10 or any(row.get("passed") is not True or row.get("cleanup_confirmed") is not True for row in linux_probes["results"]):
        raise GateAuditError("Linux benign Worker probes are not all expected and cleaned")
    if windows_admission.get("status") != "admitted" or windows_admission.get("capabilities", {}).get("catalog_plugin_claims_allowed") is not False:
        raise GateAuditError("Windows P3-04 runtime-only admission scope changed")
    if p307.get("status") != "completed":
        raise GateAuditError("P3-07 historical regressions are not complete")
    if p309_clean.get("status") != "passed" or p309_clean.get("windows_tests_run") is not False or p309_clean.get("plugin_execution") is not False:
        raise GateAuditError("P3-09 clean-checkout self-test evidence changed")
    for report, expected_status in ((p309_trap, "failed_or_interrupted"), (p309_interrupt, "failed_or_interrupted")):
        if report.get("status") != expected_status or report.get("plugin_execution") not in (False, "not_started"):
            raise GateAuditError("P3-09 failure/interruption evidence changed")
    for report_path, report in ((P309_CLEAN, p309_clean), (P309_TRAP, p309_trap), (P309_INTERRUPT, p309_interrupt)):
        log_path = ROOT / report_path.replace(".json", ".log")
        if _sha(log_path.read_bytes()) != report.get("log_sha256"):
            raise GateAuditError(f"P3-09 report log digest mismatch: {report_path}")

    gates = [
        {
            "gate": "P3-ENGINE", "status": "passed_scoped",
            "scope": "Linux Docker Worker benign isolation probes and Windows Sandbox CPython 3.12.10 runtime admission only",
            "evidence": [P308_LINUX_ADMISSION, P308_LINUX_PROBES, P304_WINDOWS],
            "limitations": ["Windows catalog_plugin_claims_allowed=false", "This is backend admission evidence, not catalog behavior acceptance."],
        },
        {
            "gate": "P3-RECOVERY", "status": "blocked", "scope": "3,179 selected affected units",
            "evidence": [P305_CLOSURE],
            "limitations": ["53 terminal unknown outcomes remain: Linux 39, Windows 14.", "All units are terminal and cleanup_unconfirmed=0; unknown is not converted to pass."],
        },
        {
            "gate": "P3-AUTOMATION", "status": "blocked", "scope": "P3-05 recovery ledger and P3-09 CI/controlled-runner delivery",
            "evidence": [P305_CLOSURE, P309_CLEAN, P309_TRAP, P309_INTERRUPT],
            "limitations": ["The same 53 P3-05 unknowns block automation health.", "GitHub hosted and protected self-hosted runner deployment state is not verified by local reports."],
        },
        {
            "gate": "P3-BEHAVIOR", "status": "not_evaluated", "scope": "20 frozen entries × 3 cases × 2 platforms = 120 positions / 360 attempt slots",
            "evidence": [P306_SUITE, P308_BEHAVIOR],
            "limitations": ["0 attempts dispatched; all 120 positions lack executable Host event/setup bindings.", "Windows admission is runtime-only and does not permit catalog behavior claims."],
        },
        {
            "gate": "P3-FIELD", "status": "not_run", "scope": "real platform ingress/delivery",
            "evidence": [P305_CLOSURE],
            "limitations": ["No real-platform acceptance was included in this plan."],
        },
    ]

    matrix_lines = [
        "# P3-10 Compatibility and Execution Matrix",
        "",
        "Generated from frozen reports and manifests by `scripts/phase3-p310-gate-audit.py`; statuses are terminal accounting, not a support declaration.",
        "",
        f"- Frozen catalog: {catalog_count} entries / {full_entry_os_count} entry×OS positions.",
        f"- P3-05 affected units: Linux {len(linux_ids)} + Windows {len(windows_union)} = {len(matrix_rows)} unique units.",
        f"- P3-05 effective unit outcomes (passed / failed / skipped / unknown): {matrix_counts['passed']} / {matrix_counts['failed']} / {matrix_counts['skipped']} / {matrix_counts['unknown']}.",
        f"- P3-08 behavior ledger: {len(behavior_positions)} positions / {behavior['scope']['planned_attempt_slots']} attempt slots; dispatched 0; behavior gate `not_evaluated`.",
        "- No catalog-wide compatibility or support claim is made. Windows Sandbox evidence is Evaluation/Audit Mode.",
        "",
        "## P3-10 Gate Rulings",
        "",
        "| Gate | Ruling | Scope and unresolved boundary |",
        "| --- | --- | --- |",
    ]
    for gate in gates:
        matrix_lines.append(f"| {gate['gate']} | `{gate['status']}` | {gate['limitations'][0]} |")
    matrix_lines.extend([
        "",
        "## P3-05 Terminal Unit Matrix",
        "",
        "| Platform | Ecosystem | Registry ID | Package | Version | Status | Reason | Cleanup | Source report |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for row in matrix_rows:
        values = [row["platform"], row["ecosystem"], row["registry_entry_id"], row["package"], row["version"], row["status"], row["reason"], row["cleanup_status"], row["source_report"]]
        matrix_lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
    matrix_lines.append("")
    matrix = "\n".join(matrix_lines)
    matrix_bytes = matrix.encode("utf-8")
    source_records = [_record(path) for path in SOURCE_PATHS]
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "phase3_p310_gate_audit_v1",
        "task_id": "P3-10",
        "status": "completed_with_blocked_gates",
        "plugin_compatibility": "not_evaluated",
        "support_expansion": False,
        "denominators": {
            "frozen_catalog_entries": catalog_count,
            "frozen_catalog_entry_os_positions": full_entry_os_count,
            "p305_linux_unique_units": len(linux_ids),
            "p305_windows_unique_units": len(windows_union),
            "p305_cross_platform_unique_units": len(matrix_rows),
            "p305_additive_http_reattributions_excluded": len(http_v4_rows),
            "p308_behavior_positions": len(behavior_positions),
            "p308_planned_attempt_slots": behavior["scope"]["planned_attempt_slots"],
            "p308_dispatched_attempts": behavior["accounting"]["dispatched_attempts"],
        },
        "p305_effective_unit_counts": {
            "linux_2206": linux_counts,
            "windows_973": windows_counts,
            "cross_platform_3179": matrix_counts,
        },
        "p305_cleanup_unit_counts": {key: int(cleanup_counts[key]) for key in ("confirmed", "not_applicable_before_worker_start", "unconfirmed")},
        "p305_set_reconciliation": {
            "recovery_selection_units": len(recovery_units),
            "linux_windows_disjoint": True,
            "windows_cohorts_disjoint": True,
            "windows_cohort_counts": {"pilot": len(pilot_rows), "nonebot": len(nonebot_rows), "koishi": len(koishi_rows)},
            "http_v1_v3_v4_refer_to_same_six_koishi_identities": True,
        },
        "gates": gates,
        "evidence_manifest": source_records,
        "matrix": {"path": P310_MATRIX, "file_sha256": _sha(matrix_bytes), "size_bytes": len(matrix_bytes), "rows": len(matrix_rows)},
        "interpretation": [
            "P3-05 execution/attribution and P3-10 reporting deliverables are complete; P3-05 health remains blocked by 53 terminal unknowns.",
            "P3-08 execution is fully accounted at 120 positions and 360 planned attempts, but 0 attempts were dispatched because executable Host fixtures are absent; P3-BEHAVIOR remains not_evaluated.",
            "P3-ENGINE is scoped to admitted Worker runtimes and benign isolation probes. Windows admission does not authorize catalog plugin execution or behavior claims.",
            "P3-AUTOMATION remains blocked until P3-05 unknowns are resolved and deployment-side runner controls are verified.",
            "No compatibility support entries were added and no real-platform field acceptance was run.",
        ],
    }
    report["report_sha256"] = _sha(_canonical(report))
    return report, matrix
