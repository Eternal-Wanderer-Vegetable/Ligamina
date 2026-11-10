"""Measure plugin-level static discovery against source-review annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_SCHEMA_PATH = ROOT / "contracts/lab/inventory/v1/capability-inventory.schema.json"


def build_report(sample_path: Path, annotation_path: Path) -> dict[str, Any]:
    sample_bytes = sample_path.read_bytes()
    sample_report = json.loads(sample_bytes)
    annotation_bytes = annotation_path.read_bytes()
    annotation_report = json.loads(annotation_bytes)
    sample_digest = hashlib.sha256(sample_bytes).hexdigest()
    if annotation_report.get("source_static_sample_sha256") != sample_digest:
        raise ValueError("manual annotations are not bound to this static sample report")
    samples = {
        item["entry"]["registry_entry_id"]: item
        for item in sample_report.get("samples", [])
    }
    annotations = {
        item["registry_entry_id"]: item
        for item in annotation_report.get("annotations", [])
    }
    inventory_validator = Draft202012Validator(
        json.loads(INVENTORY_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    if len(samples) != len(sample_report.get("samples", [])):
        raise ValueError("sample report contains duplicate registry_entry_id values")
    if set(samples) != set(annotations):
        raise ValueError("annotations must cover exactly the selected sample IDs")

    rows = []
    for registry_entry_id, sample in sorted(samples.items()):
        annotation = annotations[registry_entry_id]
        entry = sample["entry"]
        schema_errors = list(inventory_validator.iter_errors(sample["static_inventory"]))
        if schema_errors:
            raise ValueError(
                f"static inventory schema invalid: {registry_entry_id}: {schema_errors[0].message}"
            )
        if (
            entry["artifact_sha256"] != annotation.get("artifact_sha256")
            or sample["ecosystem"] != annotation.get("ecosystem")
        ):
            raise ValueError(f"annotation artifact hash mismatch: {registry_entry_id}")
        if annotation.get("review", {}).get("review_status") != "reviewed":
            raise ValueError(f"sample is not marked reviewed: {registry_entry_id}")
        static_capabilities = sample["static_inventory"]["capabilities"]
        row: dict[str, Any] = {
            "ecosystem": sample["ecosystem"],
            "registry_entry_id": registry_entry_id,
            "package": entry["package"],
            "artifact_sha256": entry["artifact_sha256"],
            "review": annotation["review"],
        }
        for category in ("commands", "handlers"):
            truth = annotation["ground_truth"][category]
            if not isinstance(truth, bool):
                raise ValueError(f"annotation ground truth must be boolean: {registry_entry_id}/{category}")
            static_record = static_capabilities[category]
            detected = static_record["status"] == "observed" and bool(static_record["items"])
            row[category] = {
                "manual_presence": truth,
                "static_presence": detected,
                "static_item_count": len(static_record["items"]),
                "result": _outcome(truth, detected),
                "miss_reason": annotation.get("miss_reasons", {}).get(category),
            }
            if truth and not detected and not row[category]["miss_reason"]:
                raise ValueError(f"missed {category} requires a documented reason: {registry_entry_id}")
        rows.append(row)

    groups = {}
    for ecosystem in ("koishi", "nonebot", "astrbot"):
        groups[ecosystem] = {
            category: _metrics(
                (row[category]["manual_presence"], row[category]["static_presence"])
                for row in rows if row["ecosystem"] == ecosystem
            )
            for category in ("commands", "handlers")
        }
    groups["overall"] = {
        category: _metrics(
            (row[category]["manual_presence"], row[category]["static_presence"])
            for row in rows
        )
        for category in ("commands", "handlers")
    }
    misses = [
        {
            "ecosystem": row["ecosystem"],
            "registry_entry_id": row["registry_entry_id"],
            "package": row["package"],
            "category": category,
            "reason": row[category]["miss_reason"],
        }
        for row in rows
        for category in ("commands", "handlers")
        if row[category]["result"] == "false_negative"
    ]
    false_positives = [
        {
            "ecosystem": row["ecosystem"],
            "registry_entry_id": row["registry_entry_id"],
            "package": row["package"],
            "category": category,
            "static_item_count": row[category]["static_item_count"],
        }
        for row in rows
        for category in ("commands", "handlers")
        if row[category]["result"] == "false_positive"
    ]
    return {
        "schema_version": 1,
        "kind": "p7_p701_inventory_quality",
        "method": {
            "sampling": sample_report["selection"],
            "review_scope": annotation_report["review_scope"],
            "reviewer": annotation_report["reviewer"],
            "measurement": "Per-plugin presence of at least one declared command or non-command handler; this does not measure exact registration counts or name accuracy.",
            "runtime_inventories_measured": False,
            "independent_double_review": False,
        },
        "sources": {
            "static_sample_sha256": sample_digest,
            "manual_annotations_sha256": hashlib.sha256(annotation_bytes).hexdigest(),
        },
        "counts": {"samples": len(rows), "per_ecosystem": {eco: 20 for eco in ("koishi", "nonebot", "astrbot")}},
        "metrics": groups,
        "decision": {
            "command_presence_recall_target": 0.9,
            "command_presence_recall_met": groups["overall"]["commands"]["recall"]["value"] is not None
            and groups["overall"]["commands"]["recall"]["value"] >= 0.9,
            "continue_to_p7_04": not misses,
            "basis": "The command-presence gate is applied only to this deterministic 60-entry source sample. Item-name quality, runtime-versus-static agreement, and catalog-wide discovery remain unmeasured.",
        },
        "false_positives": false_positives,
        "misses": misses,
        "limitations": [
            "The sample is deterministic by registry ID and cache availability, not randomized; rates are not an ecosystem-wide estimate.",
            "Labels come from one source-review pass, not independent human double annotation.",
            "Static item counts and names may be duplicated across source and built files or left unknown when registration arguments are dynamic.",
            "Runtime inventories are tested with self-authored Host fixtures; these third-party plugins were not loaded or executed.",
        ],
        "samples": rows,
    }


def _outcome(truth: bool, detected: bool) -> str:
    if truth and detected:
        return "true_positive"
    if truth:
        return "false_negative"
    if detected:
        return "false_positive"
    return "true_negative"


def _metrics(pairs) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for truth, detected in pairs:
        result = _outcome(truth, detected)
        tp += result == "true_positive"
        fp += result == "false_positive"
        fn += result == "false_negative"
        tn += result == "true_negative"
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "recall": _rate(tp, tp + fn),
        "precision": _rate(tp, tp + fp),
        "false_positive_rate": _rate(fp, fp + tn),
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    report = build_report(args.samples, args.annotations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "samples": report["counts"], "metrics": report["metrics"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
