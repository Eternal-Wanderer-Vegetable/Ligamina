"""Build the exact bounded selection for source-bound P7 synthetic contexts."""

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

from lab.automation.p7_context_profiles import (
    PROFILE_DOCUMENT_PATH as NONEBOT_PROFILE_DOCUMENT_PATH,
    context_profile_entry_ids,
    context_profile_manifest_hashes,
    context_profile_manifest_sha256,
    validate_profile_document as validate_nonebot_profiles,
)
from lab.automation.p7_koishi_context_profiles import (
    PROFILE_DOCUMENT_PATH as KOISHI_PROFILE_DOCUMENT_PATH,
    validate_profile_document as validate_koishi_profiles,
)


SOURCE_SELECTION_PATH = ROOT / "outputs/acceptance/p7-p704-independent-pilot-selection-20261111.json"
DEFAULT_OUTPUT_PATH = ROOT / "outputs/acceptance/p7-p704-context-profile-diagnostic-selection-feat061-retry3-20261111.json"


def _pretty(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_selection_document(source_selection_path: Path = SOURCE_SELECTION_PATH) -> dict[str, Any]:
    source_selection_path = source_selection_path.resolve(strict=True)
    source_raw = source_selection_path.read_bytes()
    source = json.loads(source_raw)
    if not isinstance(source, dict) or source.get("kind") != "p7_p701_static_inventory_sample":
        raise ValueError("source-context diagnostic input must be a P7-01 static inventory selection")
    nonebot = validate_nonebot_profiles()
    koishi = validate_koishi_profiles()
    profile_by_id = {
        row["registry_entry_id"]: {"ecosystem": "nonebot", **row}
        for row in nonebot["profiles"]
    }
    profile_by_id.update({
        row["registry_entry_id"]: {"ecosystem": "koishi", **row}
        for row in koishi["profiles"]
    })
    expected_ids = context_profile_entry_ids()
    source_samples = source.get("samples")
    if not isinstance(source_samples, list):
        raise ValueError("source selection has no sample list")
    selected: dict[str, dict[str, Any]] = {}
    for sample in source_samples:
        entry = sample.get("entry") if isinstance(sample, dict) else None
        entry_id = entry.get("registry_entry_id") if isinstance(entry, dict) else None
        if entry_id not in profile_by_id:
            continue
        profile = profile_by_id[entry_id]
        if (
            sample.get("ecosystem") != profile["ecosystem"]
            or entry.get("package") != profile["package"]
            or entry.get("version") != profile["version"]
            or entry.get("artifact_sha256") != profile["artifact_sha256"]
        ):
            raise ValueError(f"source selection does not contain the exact context-profile artifact: {entry_id}")
        if entry_id in selected:
            raise ValueError(f"source selection repeats a context-profile artifact: {entry_id}")
        selected[entry_id] = sample
    if set(selected) != expected_ids:
        raise ValueError(f"source selection does not contain all exact context-profile artifacts: {sorted(expected_ids - set(selected))}")

    source_selection = source.get("selection", {})
    matrix = source_selection.get("source_matrix")
    matrix_sha = source_selection.get("source_matrix_sha256")
    if not isinstance(matrix, str) or not isinstance(matrix_sha, str):
        raise ValueError("source selection is missing its frozen compatibility matrix binding")
    counts = {
        ecosystem: sum(sample["ecosystem"] == ecosystem for sample in selected.values())
        for ecosystem in ("koishi", "nonebot")
    }
    return {
        "schema_version": 1,
        "kind": "p7_p701_static_inventory_sample",
        "execution": "exact_source_context_subset_only_no_plugins_or_workers_ran",
        "source_selection": {
            "path": source_selection_path.relative_to(ROOT).as_posix()
            if source_selection_path.is_relative_to(ROOT)
            else str(source_selection_path),
            "sha256": _sha256(source_raw),
        },
        "source_context_profiles": {
            "nonebot_path": NONEBOT_PROFILE_DOCUMENT_PATH.relative_to(ROOT).as_posix(),
            "koishi_path": KOISHI_PROFILE_DOCUMENT_PATH.relative_to(ROOT).as_posix(),
            "manifest_hashes": context_profile_manifest_hashes(),
            "manifest_set_sha256": context_profile_manifest_sha256(),
        },
        "diagnostic_scope": {
            "kind": "p7_source_bound_context_profiles",
            "profile_manifest_sha256": context_profile_manifest_sha256(),
        },
        "selection": {
            "source_matrix": matrix,
            "source_matrix_sha256": matrix_sha,
            "rule": "Exact subset of the locked P7-04 hash-ranked selection matching all source-bound NoneBot and Koishi synthetic context profile artifacts; this diagnostic is not representative.",
            "selection_seed": "p7-p704-source-context-profile-diagnostic-20261111-feat061-v1",
            "target_count": len(selected),
            "counts": counts,
        },
        "samples": [selected[entry_id] for entry_id in sorted(selected)],
    }


def write_selection(output_path: Path, source_selection_path: Path = SOURCE_SELECTION_PATH) -> str:
    output_path = output_path.expanduser().absolute()
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists():
        raise FileExistsError(f"refusing to overwrite source-context selection: {output_path}")
    raw = _pretty(build_selection_document(source_selection_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    digest = _sha256(raw)
    output_path.with_suffix(output_path.suffix + ".sha256").write_text(f"{digest}  {output_path.name}\n", encoding="ascii")
    return digest


def check_selection(output_path: Path, source_selection_path: Path = SOURCE_SELECTION_PATH) -> str:
    output_path = output_path.resolve(strict=True)
    actual = output_path.read_bytes()
    digest = _sha256(actual)
    sidecar = output_path.with_suffix(output_path.suffix + ".sha256")
    if actual != _pretty(build_selection_document(source_selection_path)) or not sidecar.is_file():
        raise ValueError("source-context selection differs from its derived locked subset")
    if sidecar.read_text(encoding="ascii").split()[0] != digest:
        raise ValueError("source-context selection SHA-256 sidecar changed")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-selection", type=Path, default=SOURCE_SELECTION_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args(argv)
    try:
        digest = check_selection(options.output, options.source_selection) if options.check else write_selection(options.output, options.source_selection)
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "passed", "output": options.output.as_posix(), "sha256": digest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
