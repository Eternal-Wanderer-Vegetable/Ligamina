"""Command-line entry for generating and validating P3 automation manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .selection import AutomationContractError, ROOT, build_recovery_manifest, validate_manifest


def _write_json(path: Path, value: dict) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="recompute an immutable S2-11 recovery selection")
    plan.add_argument("--snapshot", type=Path, default=ROOT / "catalogs/p2-41649744e057d61031d5")
    plan.add_argument("--audit", type=Path, default=ROOT / "outputs/acceptance/phase2-s212-audit-20261017.json")
    plan.add_argument("--attribution", type=Path, default=ROOT / "outputs/acceptance/phase2-s211-failures-20261014.json")
    plan.add_argument("--reports", type=Path, default=ROOT / "outputs/acceptance")
    plan.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="validate a frozen manifest and its source hashes")
    validate.add_argument("manifest", type=Path)
    options = parser.parse_args(argv)
    try:
        if options.command == "plan":
            reports = sorted(options.reports.glob("phase2-s211-rerun-census-*-shard*of256-20261014.json"))
            manifest = build_recovery_manifest(options.snapshot, options.audit, options.attribution, reports)
            _write_json(options.output, manifest)
            result = validate_manifest(manifest, verify_source_files=True)
            print(json.dumps({"status": "planned", "output": str(options.output.resolve()), **result}, ensure_ascii=False))
            return 0
        manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
        result = validate_manifest(manifest, verify_source_files=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, AutomationContractError) as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
