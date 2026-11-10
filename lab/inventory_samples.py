"""Build a deterministic, source-only sample from Linux load-verified entries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from lab.capability_inventory import scan_archive

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "outputs/compat/compatibility-matrix-v1.json"
CATALOGS = {
    "astrbot": ROOT / "catalogs/ab-15937e90d26b7caf9d12",
    "koishi": ROOT / "catalogs/p2-41649744e057d61031d5",
    "nonebot": ROOT / "catalogs/p2-41649744e057d61031d5",
}


def build_report(*, matrix_path: Path, cache_root: Path, per_ecosystem: int = 20) -> dict[str, Any]:
    matrix_bytes = matrix_path.read_bytes()
    matrix = json.loads(matrix_bytes)
    locks = {
        ecosystem: json.loads((snapshot / "artifacts.lock.json").read_text(encoding="utf-8-sig"))
        for ecosystem, snapshot in CATALOGS.items()
    }
    samples = []
    counts = {}
    for ecosystem in ("koishi", "nonebot", "astrbot"):
        lock_entries = {entry["registry_entry_id"]: entry for entry in locks[ecosystem]["entries"]}
        rows = sorted(
            (
                row for row in matrix["entries"]
                if row["ecosystem"] == ecosystem
                and row.get("catalog_status") in {"frozen", "locked"}
                and row.get("capabilities", {}).get("load", {}).get("linux", {}).get("value") == "verified"
            ),
            key=lambda row: row["registry_entry_id"],
        )
        selected = 0
        for row in rows:
            entry = lock_entries.get(row["registry_entry_id"])
            if not entry:
                continue
            artifact = _artifact_for(ecosystem, entry, row)
            if artifact is None:
                continue
            artifact_path = cache_root / artifact["cache_path"]
            if not artifact_path.is_file():
                continue
            digest = _sha256(artifact_path)
            expected = artifact.get("computed_digests", {}).get("sha256")
            if digest != expected:
                raise ValueError(f"artifact cache digest mismatch: {artifact_path}")
            load_evidence = row["capabilities"]["load"]["linux"]["evidence"]
            catalog_package = row["package"]
            version = row.get("version") or entry.get("advertised_version") or "unknown"
            inventory = scan_archive(
                artifact_path,
                ecosystem=ecosystem,
                package=catalog_package,
                version=version,
                registry_entry_id=row["registry_entry_id"],
                artifact_sha256=digest,
            )
            samples.append({
                "ecosystem": ecosystem,
                "sample_index": selected + 1,
                "entry": {
                    "registry_entry_id": row["registry_entry_id"],
                    "package": catalog_package,
                    "version": version,
                    "artifact_sha256": digest,
                    "load_evidence": load_evidence,
                },
                "static_inventory": inventory,
            })
            selected += 1
            if selected == per_ecosystem:
                break
        counts[ecosystem] = selected
    return {
        "schema_version": 1,
        "kind": "p7_p701_static_inventory_sample",
        "selection": {
            "source_matrix": matrix_path.relative_to(ROOT).as_posix(),
            "source_matrix_sha256": hashlib.sha256(matrix_bytes).hexdigest(),
            "rule": "Within each ecosystem, sort Linux load-verified frozen rows by registry_entry_id and select the first cached artifacts until the per-ecosystem target is met.",
            "per_ecosystem_target": per_ecosystem,
            "counts": counts,
        },
        "execution": "static_only_no_plugin_import_or_execution",
        "samples": samples,
    }


def _artifact_for(ecosystem: str, entry: dict, row: dict) -> dict | None:
    if ecosystem == "astrbot":
        return entry.get("artifact")
    desired = set(row.get("artifact_sha256") or [])
    candidates = [
        item.get("artifact")
        for item in entry.get("artifacts", [])
        if item.get("status") == "locked" and isinstance(item.get("artifact"), dict)
    ]
    candidates = [candidate for candidate in candidates if candidate]
    return next(
        (candidate for candidate in candidates if candidate.get("computed_digests", {}).get("sha256") in desired),
        None,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--per-ecosystem", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.per_ecosystem <= 0:
        parser.error("--per-ecosystem must be positive")
    if args.output.exists():
        parser.error(f"refusing to overwrite existing report: {args.output}")
    report = build_report(matrix_path=args.matrix.resolve(), cache_root=args.cache_root.resolve(), per_ecosystem=args.per_ecosystem)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "built", "output": args.output.as_posix(), "samples": report["selection"]["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
