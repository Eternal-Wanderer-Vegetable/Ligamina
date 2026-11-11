"""Derive an exact three-artifact selection for the P7 context-profile diagnostic."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation.p7_context_profiles import PROFILE_DOCUMENT_PATH, profile_manifest_sha256, validate_profile_document


SOURCE_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json"
DEFAULT_OUTPUT_PATH = ROOT / "outputs/acceptance/p7-p704-context-profile-diagnostic-selection-20261111.json"


def _pretty(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_selection_document(source_selection_path: Path = SOURCE_SELECTION_PATH) -> dict[str, Any]:
    source_selection_path = source_selection_path.resolve(strict=True)
    source_raw = source_selection_path.read_bytes()
    source = json.loads(source_raw)
    if not isinstance(source, dict) or source.get("kind") != "p7_p701_static_inventory_sample":
        raise ValueError("context diagnostic source must be a P7-01 static inventory selection")
    profile_document = validate_profile_document()
    profile_by_id = {row["registry_entry_id"]: row for row in profile_document["profiles"]}
    source_samples = source.get("samples")
    if not isinstance(source_samples, list):
        raise ValueError("context diagnostic source selection has no sample list")
    selected: dict[str, dict[str, Any]] = {}
    for sample in source_samples:
        entry = sample.get("entry") if isinstance(sample, dict) else None
        entry_id = entry.get("registry_entry_id") if isinstance(entry, dict) else None
        if entry_id not in profile_by_id:
            continue
        profile = profile_by_id[entry_id]
        if (
            sample.get("ecosystem") != "nonebot"
            or entry.get("package") != profile["package"]
            or entry.get("version") != profile["version"]
            or entry.get("artifact_sha256") != profile["artifact_sha256"]
        ):
            raise ValueError(f"source selection does not contain the exact profile artifact: {entry_id}")
        if entry_id in selected:
            raise ValueError(f"source selection repeats a profile artifact: {entry_id}")
        selected[entry_id] = sample
    if set(selected) != set(profile_by_id):
        missing = sorted(set(profile_by_id) - set(selected))
        raise ValueError(f"source selection is missing P7 context profile artifacts: {missing}")

    source_selection = source.get("selection", {})
    matrix = source_selection.get("source_matrix")
    matrix_sha = source_selection.get("source_matrix_sha256")
    if not isinstance(matrix, str) or not isinstance(matrix_sha, str):
        raise ValueError("source selection is missing its frozen compatibility matrix binding")
    return {
        "schema_version": 1,
        "kind": "p7_p701_static_inventory_sample",
        "execution": "source_subset_only_no_plugins_or_workers_ran",
        "source_selection": {
            "path": source_selection_path.relative_to(ROOT).as_posix()
            if source_selection_path.is_relative_to(ROOT)
            else str(source_selection_path),
            "sha256": _sha256(source_raw),
        },
        "test_context_profile": {
            "path": PROFILE_DOCUMENT_PATH.relative_to(ROOT).as_posix(),
            "sha256": profile_manifest_sha256(),
        },
        "diagnostic_scope": {
            "kind": "p7_nonebot_context_profile",
            "profile_manifest_sha256": profile_manifest_sha256(),
        },
        "selection": {
            "source_matrix": matrix,
            "source_matrix_sha256": matrix_sha,
            "rule": "Exact three-entry subset of the locked P7-04 hash-ranked selection matching the source-bound P7 NoneBot context profile identities; this diagnostic is not a representative sample.",
            "selection_seed": "p7-p704-context-profile-diagnostic-20261111-v1",
            "target_count": len(selected),
            "counts": {"koishi": 0, "nonebot": len(selected)},
        },
        "samples": [selected[entry_id] for entry_id in sorted(selected)],
    }


def check_selection(output_path: Path, source_selection_path: Path = SOURCE_SELECTION_PATH) -> str:
    output_path = output_path.resolve(strict=True)
    expected = _pretty(build_selection_document(source_selection_path))
    actual = output_path.read_bytes()
    digest = _sha256(actual)
    sidecar = output_path.with_suffix(output_path.suffix + ".sha256")
    if actual != expected or not sidecar.is_file() or sidecar.read_text(encoding="ascii").split()[0] != digest:
        raise ValueError("context-profile diagnostic selection or SHA-256 sidecar changed")
    return digest


def write_selection(output_path: Path, source_selection_path: Path = SOURCE_SELECTION_PATH) -> str:
    output_path = output_path.expanduser().absolute()
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite context-profile selection: {output_path}")
    raw = _pretty(build_selection_document(source_selection_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    digest = _sha256(raw)
    output_path.with_suffix(output_path.suffix + ".sha256").write_text(f"{digest}  {output_path.name}\n", encoding="ascii")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-selection", type=Path, default=SOURCE_SELECTION_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args(argv)
    try:
        digest = (
            check_selection(options.output, options.source_selection)
            if options.check
            else write_selection(options.output, options.source_selection)
        )
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "passed", "output": options.output.as_posix(), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

