use std::collections::{BTreeMap, BTreeSet};

use ligamina_contracts::validate_lab_document;
use serde_json::{Map, Value, json};

const STATUSES: [&str; 4] = ["passed", "failed", "skipped", "unknown"];
const GATES: [&str; 3] = ["completion", "automation", "compatibility"];

#[derive(Default)]
struct Counts {
    observed: usize,
    statuses: BTreeMap<String, usize>,
}

pub fn render_report(
    report: &Value,
    output_format: &str,
    gate_override: Option<&str>,
) -> Result<(String, i32), String> {
    if !report.is_object() {
        return Err("report root must be an object".into());
    }
    if !["json", "text", "ndjson"].contains(&output_format) {
        return Err("unsupported report format".into());
    }
    if report.get("schema_version").is_some() {
        if report.get("schema_version").and_then(Value::as_u64) != Some(1) {
            return Err("unsupported report schema version".into());
        }
        let projection = project_v1_report(report, gate_override)?;
        let code = projection["exit_code"].as_i64().unwrap_or(2) as i32;
        let rendered = match output_format {
            "json" => {
                serde_json::to_string_pretty(&projection).map_err(|_| "report rendering failed")?
            }
            "ndjson" => render_ndjson(&projection)?,
            "text" => render_text(&projection),
            _ => unreachable!(),
        };
        return Ok((rendered, code));
    }
    if report.get("manifest").is_some() {
        if gate_override.is_some() {
            return Err("gate selection is not supported for a catalog plan".into());
        }
        return render_catalog_plan(report, output_format);
    }
    if gate_override.is_some() {
        return Err("gate selection requires a versioned lab v1 report".into());
    }
    let summary = legacy_summary(report);
    let mut safe = summary.clone();
    redact_value(&mut safe, None);
    let rendered = match output_format {
        "json" => serde_json::to_string_pretty(&safe).map_err(|_| "report rendering failed")?,
        "text" => render_legacy_text(&safe),
        "ndjson" => {
            let suite = safe.get("suite").cloned().unwrap_or(Value::Null);
            let status = safe.get("status").cloned().unwrap_or(Value::Null);
            let lines = [
                json!({"schema_version": 1, "event": "legacy_summary", "suite": suite, "status": status}),
                json!({"schema_version": 1, "event": "summary", "summary": safe}),
            ];
            lines
                .iter()
                .map(compact_json)
                .collect::<Result<Vec<_>, _>>()?
                .join("\n")
        }
        _ => unreachable!(),
    };
    Ok((rendered, 0))
}

fn render_catalog_plan(report: &Value, output_format: &str) -> Result<(String, i32), String> {
    let stages = report["manifest"]["stages"]
        .as_array()
        .ok_or("catalog plan stages are missing")?;
    let counts = report["job_counts"]
        .as_object()
        .ok_or("catalog job counts are missing")?;
    let selected = report["selected_units"]
        .as_u64()
        .ok_or("selected unit count is missing")?;
    let expected_jobs = report["expected_stage_jobs"]
        .as_u64()
        .ok_or("expected job count is missing")?;
    let total = counts
        .get("total")
        .and_then(Value::as_u64)
        .ok_or("total job count is missing")?;
    let statuses = ["queued", "leased", "passed", "failed", "skipped", "unknown"];
    let computed_jobs = selected
        .checked_mul(stages.len() as u64)
        .ok_or("catalog stage job count overflows")?;
    let accounted: u64 = statuses
        .iter()
        .map(|status| counts.get(*status).and_then(Value::as_u64).unwrap_or(0))
        .sum();
    if stages.is_empty()
        || stages
            .iter()
            .any(|stage| !stage.as_str().is_some_and(|stage| !stage.is_empty()))
        || counts.len() != statuses.len() + 1
        || statuses
            .iter()
            .any(|status| counts.get(*status).and_then(Value::as_u64).is_none())
        || !matches!(report["status"].as_str(), Some("planned" | "resumed"))
        || report["execution_started"] != false
        || expected_jobs != computed_jobs
        || expected_jobs != total
        || accounted != total
        || report["total_catalog_entries"].as_u64().is_none()
        || report["expected_units"]
            .as_u64()
            .is_none_or(|expected| expected < selected)
        || report["manifest"]["snapshot_entry_count"] != report["total_catalog_entries"]
        || report["manifest"]["selected_unit_count"].as_u64() != Some(selected)
        || report["manifest"]["expected_stage_count"].as_u64() != Some(expected_jobs)
        || report["run_id"].as_str().is_none()
        || report["snapshot_id"].as_str().is_none()
    {
        return Err("invalid catalog plan report".into());
    }
    let mut projection = json!({
        "schema_version": 1, "event": "catalog_plan", "suite": "catalog",
        "status": report["status"], "run_id": report["run_id"], "run_state": report["run_state"],
        "snapshot_id": report["snapshot_id"], "total_catalog_entries": report["total_catalog_entries"],
        "expected_units": report["expected_units"], "selected_units": selected,
        "expected_stage_jobs": expected_jobs, "job_counts": report["job_counts"], "execution_started": false
    });
    redact_value(&mut projection, None);
    let rendered = match output_format {
        "json" => {
            serde_json::to_string_pretty(&projection).map_err(|_| "report rendering failed")?
        }
        "ndjson" => {
            let summary = projection
                .as_object()
                .unwrap()
                .iter()
                .filter(|(key, _)| key.as_str() != "event")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<Map<_, _>>();
            compact_json(
                &json!({"schema_version": 1, "event": "catalog_plan", "summary": summary}),
            )?
        }
        "text" => {
            let counts_text = statuses
                .iter()
                .filter_map(|status| counts.get(*status).map(|value| format!("{status}={value}")))
                .collect::<Vec<_>>()
                .join(", ");
            format!(
                "Catalog plan {} ({})\nSnapshot {}: {} entries\nUnits {} selected / {} catalog units; {} stage jobs\nJobs: {}\nPlugin execution started: no",
                projection["run_id"].as_str().unwrap_or(""),
                projection["status"].as_str().unwrap_or(""),
                projection["snapshot_id"].as_str().unwrap_or(""),
                projection["total_catalog_entries"],
                projection["selected_units"],
                projection["expected_units"],
                projection["expected_stage_jobs"],
                counts_text
            )
        }
        _ => return Err("unsupported report format".into()),
    };
    Ok((rendered, 0))
}

fn legacy_summary(report: &Value) -> Value {
    json!({
        "suite": report.get("suite"),
        "status": report.get("status"),
        "gate": report.get("gate"),
        "platform": report.get("platform"),
        "case_summary": report.get("case_summary"),
        "sample_count": report.get("samples").and_then(Value::as_array).map(Vec::len),
        "l4_pass_count": report.get("l4_pass_count"),
        "resource_budget": report.get("resource_budget"),
        "recovery_budget": report.get("recovery_budget"),
        "cleanup": report.get("cleanup"),
        "error": report.get("error"),
    })
}

fn project_v1_report(report: &Value, gate_override: Option<&str>) -> Result<Value, String> {
    validate_lab_document("run", report).map_err(|_| "report failed schema validation")?;
    let calculated = summarize(report)?;
    if report.get("summary") != Some(&calculated) {
        return Err("report summary does not match expected IDs and results".into());
    }
    let gate = report["gate"].as_str().ok_or("report gate is missing")?;
    let recorded_gate_passed = gate_passed(report, gate)?;
    if report["gate_passed"].as_bool() != Some(recorded_gate_passed)
        || report["exit_code"].as_i64() != Some(exit_code(report, gate)? as i64)
    {
        return Err("recorded gate result or exit code is inconsistent".into());
    }
    let selected_gate = gate_override.unwrap_or(gate);
    if !GATES.contains(&selected_gate) {
        return Err("unknown report gate".into());
    }
    let selected_passed = gate_passed(report, selected_gate)?;
    let code = exit_code(report, selected_gate)?;
    let matrix = build_matrix(
        report["results"]
            .as_array()
            .ok_or("results must be an array")?,
    );
    let mut projection = json!({
        "schema_version": 1,
        "suite": report["suite"],
        "run_id": report["run_id"],
        "run_state": report["run_state"],
        "gate": selected_gate,
        "gate_passed": selected_passed,
        "exit_code": code,
        "catalog": {
            "snapshot_id": report["catalog"]["snapshot_id"],
            "entry_count": report["catalog"]["entry_count"],
            "official_maintained_count": report["catalog"]["official_maintained_count"],
            "ecosystems": report["catalog"]["ecosystems"],
        },
        "summary": calculated,
        "matrix": matrix,
    });
    redact_value(&mut projection, None);
    Ok(projection)
}

fn summarize(report: &Value) -> Result<Value, String> {
    let expected_values = report["expected_result_ids"]
        .as_array()
        .ok_or("expected IDs must be an array")?;
    let expected: Vec<&str> = expected_values
        .iter()
        .map(|value| value.as_str().ok_or("expected IDs must be strings"))
        .collect::<Result<_, _>>()?;
    let expected_set: BTreeSet<&str> = expected.iter().copied().collect();
    if expected_set.len() != expected.len() {
        return Err("expected IDs must be unique".into());
    }

    let results = report["results"]
        .as_array()
        .ok_or("results must be an array")?;
    let mut seen = BTreeSet::new();
    let mut duplicates = Vec::new();
    let mut unexpected = BTreeSet::new();
    let mut counts = BTreeMap::from([
        ("passed", 0_usize),
        ("failed", 0),
        ("skipped", 0),
        ("unknown", 0),
    ]);
    for result in results {
        let id = result["result_id"].as_str().ok_or("result ID is missing")?;
        if !seen.insert(id) {
            duplicates.push(id);
            continue;
        }
        if !expected_set.contains(id) {
            unexpected.insert(id);
            continue;
        }
        let status = result["status"]
            .as_str()
            .ok_or("result status is missing")?;
        *counts.get_mut(status).ok_or("unknown result status")? += 1;
    }
    let missing: Vec<&str> = expected_set.difference(&seen).copied().collect();
    let accounted = expected_set.len() - missing.len();
    let executed = counts["passed"] + counts["failed"] + counts["unknown"];
    let expected_count = expected_set.len();
    let gate_passed = expected_count > 0
        && accounted == expected_count
        && unexpected.is_empty()
        && duplicates.is_empty()
        && counts["passed"] == expected_count;
    Ok(json!({
        "schema_version": 1,
        "expected_result_count": expected_count,
        "accounted_result_count": accounted,
        "missing_count": missing.len(),
        "missing_result_ids": missing,
        "unexpected_count": unexpected.len(),
        "unexpected_result_ids": unexpected,
        "duplicate_count": duplicates.len(),
        "duplicate_result_ids": duplicates,
        "status_counts": counts,
        "executed_denominator": executed,
        "entry_coverage": rate(accounted, expected_count),
        "stage_execution_coverage": rate(executed, expected_count),
        "executed_pass_rate": rate(counts["passed"], executed),
        "passed_over_expected_rate": rate(counts["passed"], expected_count),
        "gate_passed": gate_passed,
    }))
}

fn rate(numerator: usize, denominator: usize) -> Option<f64> {
    (denominator > 0).then(|| numerator as f64 / denominator as f64)
}

fn gate_passed(report: &Value, gate: &str) -> Result<bool, String> {
    if !GATES.contains(&gate) {
        return Err("unknown report gate".into());
    }
    let summary = &report["summary"];
    let fully_accounted = report["run_state"].as_str() == Some("completed")
        && summary["expected_result_count"].as_u64().unwrap_or(0) > 0
        && summary["accounted_result_count"] == summary["expected_result_count"]
        && summary["missing_count"].as_u64() == Some(0)
        && summary["unexpected_count"].as_u64() == Some(0)
        && summary["duplicate_count"].as_u64() == Some(0);
    Ok(match gate {
        "completion" => fully_accounted,
        "automation" => {
            fully_accounted
                && summary["status_counts"]["unknown"].as_u64() == Some(0)
                && !report["results"].as_array().is_some_and(|results| {
                    results.iter().any(|result| {
                        result["reason_code"].as_str() == Some("not_scheduled")
                            || (result["status"].as_str() == Some("failed")
                                && matches!(
                                    result["failure_domain"].as_str(),
                                    Some("harness" | "infrastructure")
                                ))
                    })
                })
        }
        "compatibility" => fully_accounted && summary["gate_passed"].as_bool() == Some(true),
        _ => unreachable!(),
    })
}

fn exit_code(report: &Value, gate: &str) -> Result<i32, String> {
    if report["run_state"].as_str() == Some("interrupted") {
        return Ok(3);
    }
    Ok(if gate_passed(report, gate)? { 0 } else { 1 })
}

fn build_matrix(results: &[Value]) -> Value {
    let mut matrix = Map::new();
    for dimension in ["ecosystem", "official_maintained", "platform", "stage"] {
        let mut groups: BTreeMap<String, Counts> = BTreeMap::new();
        for result in results {
            let value = match result.get(dimension) {
                Some(Value::Bool(value)) => value.to_string(),
                Some(Value::String(value)) => value.clone(),
                _ => "unknown".into(),
            };
            let row = groups.entry(value).or_default();
            row.observed += 1;
            let status = result["status"].as_str().unwrap_or("unknown").to_owned();
            *row.statuses.entry(status).or_default() += 1;
        }
        let rows = groups
            .into_iter()
            .map(|(value, counts)| {
                let mut status_counts = Map::new();
                for status in STATUSES {
                    status_counts.insert(status.into(), json!(counts.statuses.get(status).copied().unwrap_or(0)));
                }
                json!({"value": value, "observed_count": counts.observed, "status_counts": status_counts})
            })
            .collect::<Vec<_>>();
        matrix.insert(dimension.into(), json!(rows));
    }
    Value::Object(matrix)
}

fn render_ndjson(projection: &Value) -> Result<String, String> {
    let run_id = &projection["run_id"];
    let events = [
        json!({
            "schema_version": 1, "event": "run", "run_id": run_id,
            "run_state": projection["run_state"], "gate": projection["gate"],
            "exit_code": projection["exit_code"],
        }),
        json!({"schema_version": 1, "event": "matrix", "run_id": run_id, "matrix": projection["matrix"]}),
        json!({"schema_version": 1, "event": "summary", "run_id": run_id, "summary": projection["summary"]}),
    ];
    events
        .iter()
        .map(compact_json)
        .collect::<Result<Vec<_>, _>>()
        .map(|lines| lines.join("\n"))
}

fn compact_json(value: &Value) -> Result<String, String> {
    serde_json::to_string(value).map_err(|_| "report rendering failed".into())
}

fn render_text(projection: &Value) -> String {
    let summary = &projection["summary"];
    let counts = &summary["status_counts"];
    let pass_rate = summary["executed_pass_rate"]
        .as_f64()
        .map_or_else(|| "n/a".into(), |rate| format!("{:.1}%", rate * 100.0));
    let gate_label = if projection["gate_passed"] == true {
        "PASS"
    } else {
        "NOT READY"
    };
    let mut lines = vec![
        format!(
            "Run {} ({}) — {}",
            projection["run_id"].as_str().unwrap_or(""),
            projection["suite"].as_str().unwrap_or(""),
            projection["run_state"].as_str().unwrap_or("")
        ),
        format!(
            "Gate {}: {} (exit {})",
            projection["gate"].as_str().unwrap_or(""),
            gate_label,
            projection["exit_code"]
        ),
        format!(
            "Catalog {}: {} entries; {} officially maintained",
            projection["catalog"]["snapshot_id"].as_str().unwrap_or(""),
            projection["catalog"]["entry_count"],
            projection["catalog"]["official_maintained_count"]
        ),
        format!(
            "Results: {}/{} accounted; {} missing; {} unexpected; {} duplicate",
            summary["accounted_result_count"],
            summary["expected_result_count"],
            summary["missing_count"],
            summary["unexpected_count"],
            summary["duplicate_count"]
        ),
        format!(
            "passed={} failed={} skipped={} unknown={} executed_pass_rate={pass_rate}",
            counts["passed"], counts["failed"], counts["skipped"], counts["unknown"]
        ),
    ];
    if let Some(matrix) = projection["matrix"].as_object() {
        for (dimension, rows) in matrix {
            if let Some(rows) = rows.as_array().filter(|rows| !rows.is_empty()) {
                let cells = rows
                    .iter()
                    .map(|row| format!("{}={}", row["value"], row["observed_count"]))
                    .collect::<Vec<_>>()
                    .join(", ");
                lines.push(format!("{dimension}: {cells}"));
            }
        }
    }
    lines.join("\n")
}

fn render_legacy_text(summary: &Value) -> String {
    summary
        .as_object()
        .map(|object| {
            object
                .iter()
                .map(|(key, value)| format!("{key}: {value}"))
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default()
}

fn redact_value(value: &mut Value, key: Option<&str>) {
    if key.is_some_and(is_secret_key) {
        *value = Value::String("[REDACTED]".into());
        return;
    }
    match value {
        Value::Object(object) => {
            for (key, child) in object {
                redact_value(child, Some(key));
            }
        }
        Value::Array(values) => values
            .iter_mut()
            .for_each(|child| redact_value(child, None)),
        Value::String(text) => {
            *text = redact_inline(text);
            if text.chars().count() > 4096 {
                *text = format!(
                    "{}…[truncated]",
                    text.chars().take(4096).collect::<String>()
                );
            }
        }
        _ => {}
    }
}

fn is_secret_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    [
        "token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "credential",
        "api_key",
        "api-key",
        "private_key",
        "private-key",
    ]
    .iter()
    .any(|needle| key.contains(needle))
}

fn redact_inline(source: &str) -> String {
    let mut text = source.to_owned();
    for marker in [
        "bearer ",
        "password=",
        "password:",
        "token=",
        "token:",
        "secret=",
        "secret:",
        "api_key=",
        "api_key:",
        "api-key=",
        "api-key:",
    ] {
        let mut search_from = 0;
        while let Some(relative) = text[search_from..].to_ascii_lowercase().find(marker) {
            let start = search_from + relative;
            let value_start = start + marker.len();
            let value_end = text[value_start..]
                .find(|character: char| character.is_whitespace() || ",;".contains(character))
                .map_or(text.len(), |relative| value_start + relative);
            if value_end > value_start {
                text.replace_range(value_start..value_end, "[REDACTED]");
                search_from = value_start + "[REDACTED]".len();
            } else {
                search_from = value_start;
            }
            if search_from >= text.len() {
                break;
            }
        }
    }
    if let Some(scheme) = text.find("://") {
        if let Some(at) = text[scheme + 3..].find('@') {
            let at = scheme + 3 + at;
            if text[scheme + 3..at].contains(':') {
                text.replace_range(scheme + 3..at, "[REDACTED]");
            }
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Value {
        json!({
            "schema_version": 1,
            "suite": "catalog",
            "run_id": "report-fixture",
            "run_state": "completed",
            "created_at_utc": "2026-10-12T00:00:00Z",
            "finished_at_utc": "2026-10-12T00:01:00Z",
            "catalog": {
                "schema_version": 1, "snapshot_id": "snapshot", "source_snapshot_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "fetched_at_utc": "2026-10-12T00:00:00Z", "entry_count": 1, "official_maintained_count": 1,
                "ecosystems": ["nonebot"], "filter_policy": "none"
            },
            "provenance": {
                "git_commit": "abcdef0123456789", "git_dirty": false, "framework": "nonebot", "runtime": "python-3.12.14",
                "os": "windows", "architecture": "x86_64", "profile_id": "profile",
                "dependency_lock_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            },
            "expected_result_ids": ["result-1"],
            "results": [{
                "schema_version": 1, "result_id": "result-1", "registry_entry_id": "nb-1", "ecosystem": "nonebot",
                "official_maintained": true, "package": "example", "version": "1.0", "platform": "windows", "profile_id": "profile",
                "stage": "federated", "case_id": "echo", "attempt_id": "attempt-1", "status": "passed", "delivery_scope": "virtual"
            }],
            "summary": {
                "schema_version": 1, "expected_result_count": 1, "accounted_result_count": 1, "missing_count": 0, "missing_result_ids": [],
                "unexpected_count": 0, "unexpected_result_ids": [], "duplicate_count": 0, "duplicate_result_ids": [],
                "status_counts": {"passed": 1, "failed": 0, "skipped": 0, "unknown": 0}, "executed_denominator": 1,
                "entry_coverage": 1.0, "stage_execution_coverage": 1.0, "executed_pass_rate": 1.0,
                "passed_over_expected_rate": 1.0, "gate_passed": true
            },
            "gate": "compatibility", "gate_passed": true, "exit_code": 0
        })
    }

    #[test]
    fn renders_versioned_formats_and_validates_summary() {
        let report = fixture();
        let (json_output, code) = render_report(&report, "json", None).unwrap();
        assert_eq!(code, 0);
        let projection: Value = serde_json::from_str(&json_output).unwrap();
        assert_eq!(projection["summary"]["expected_result_count"], 1);
        let (ndjson, _) = render_report(&report, "ndjson", None).unwrap();
        assert_eq!(ndjson.lines().count(), 3);
        assert!(
            render_report(&report, "text", None)
                .unwrap()
                .0
                .contains("Gate compatibility: PASS")
        );

        let mut invalid = report;
        invalid["summary"]["status_counts"]["passed"] = json!(42);
        assert!(render_report(&invalid, "json", None).is_err());
    }

    #[test]
    fn gates_keep_completion_separate_from_compatibility_and_interruption() {
        let mut report = fixture();
        report["results"][0]["status"] = json!("failed");
        report["results"][0]["reason_code"] = json!("plugin_error");
        report["results"][0]["failure_domain"] = json!("package");
        report["summary"] = summarize(&report).unwrap();
        report["gate"] = json!("completion");
        report["gate_passed"] = json!(true);
        report["exit_code"] = json!(0);
        assert_eq!(render_report(&report, "json", None).unwrap().1, 0);
        assert_eq!(
            render_report(&report, "json", Some("compatibility"))
                .unwrap()
                .1,
            1
        );
        report["run_state"] = json!("interrupted");
        report["gate_passed"] = json!(false);
        report.as_object_mut().unwrap().remove("finished_at_utc");
        report["exit_code"] = json!(3);
        assert_eq!(render_report(&report, "json", None).unwrap().1, 3);
    }

    #[test]
    fn secrets_are_redacted_and_logs_are_bounded() {
        let mut value = json!({"api_token": "x", "message": "Bearer abc password=hunter2", "log": "z".repeat(5000)});
        redact_value(&mut value, None);
        assert_eq!(value["api_token"], "[REDACTED]");
        assert!(!value.to_string().contains("abc"));
        assert!(!value.to_string().contains("hunter2"));
        assert!(value["log"].as_str().unwrap().ends_with("[truncated]"));
    }

    #[test]
    fn catalog_plan_output_is_explicitly_non_executing() {
        let plan = json!({
            "status": "planned", "run_id": "catalog-run", "run_state": "queued", "snapshot_id": "snapshot",
            "total_catalog_entries": 5690, "expected_units": 5690, "selected_units": 3,
            "expected_stage_jobs": 18,
            "job_counts": {"queued": 18, "leased": 0, "passed": 0, "failed": 0, "skipped": 0, "unknown": 0, "total": 18},
                "execution_started": false, "manifest": {
                    "stages": ["a", "b", "c", "d", "e", "f"],
                    "snapshot_entry_count": 5690, "selected_unit_count": 3, "expected_stage_count": 18
                }
        });
        let (json_output, code) = render_report(&plan, "json", None).unwrap();
        assert_eq!(code, 0);
        let output: Value = serde_json::from_str(&json_output).unwrap();
        assert_eq!(output["execution_started"], false);
        assert_eq!(output["job_counts"]["queued"], 18);
        assert!(
            render_report(&plan, "text", None)
                .unwrap()
                .0
                .contains("Plugin execution started: no")
        );
        assert!(render_report(&plan, "json", Some("compatibility")).is_err());
    }
}
