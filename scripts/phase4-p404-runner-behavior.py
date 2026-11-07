#!/usr/bin/env python3
"""Run the existing Windows Sandbox Worker contract probes without overwriting history."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SCRIPT = ROOT / "deploy" / "lab" / "windows" / "probes" / "acceptance.py"
SOURCE_PATHS = (
    "deploy/lab/windows/runner.ps1",
    "deploy/lab/windows/collect-output.ps1",
    "lab/workers/windows.py",
    "deploy/lab/windows/probes/acceptance.py",
    "scripts/phase4-p404-runner-behavior.py",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_output_path(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve()
    acceptance_root = (ROOT / "outputs" / "acceptance").resolve()
    try:
        candidate.relative_to(acceptance_root)
    except ValueError as error:
        raise SystemExit("--output must be inside outputs/acceptance") from error
    if candidate.suffix.lower() != ".json":
        raise SystemExit("--output must end in .json")
    if candidate.exists() or Path(str(candidate) + ".sha256").exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {candidate}")
    return candidate


def load_acceptance_module() -> Any:
    spec = importlib.util.spec_from_file_location("phase4_p404_windows_acceptance", ACCEPTANCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load Windows acceptance probes: {ACCEPTANCE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_name = f"phase4-p404-runner-behavior-{datetime.now().astimezone():%Y%m%dT%H%M%S}.json"
    parser.add_argument(
        "--output",
        default=f"outputs/acceptance/{default_name}",
        help="new JSON report path under outputs/acceptance (existing paths are rejected)",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("these probes require the Windows host and Windows Sandbox CLI")

    output_path = resolve_output_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    module = load_acceptance_module()
    module.REPORT_PATH = output_path
    exit_code = int(module.main())

    if not output_path.is_file():
        raise SystemExit(f"probe entrypoint returned without writing its report: {output_path}")
    report = json.loads(output_path.read_text(encoding="utf-8"))
    results = report.get("results", [])
    all_passed = bool(results) and all(item.get("passed") is True for item in results)
    all_cleanup_confirmed = bool(results) and all(item.get("cleanup_confirmed") is True for item in results)
    report["date"] = datetime.now().astimezone().date().isoformat()
    report["scope"] = "phase4_p404_runner_behavior_only"
    report["runner_behavior_status"] = "passed_scoped" if all_passed and all_cleanup_confirmed else "not_passed"
    report["all_cleanup_confirmed"] = all_cleanup_confirmed
    report["claims"] = {
        "runner_behavior": report["runner_behavior_status"],
        "catalog_admission": "not_evaluated",
        "catalog_plugin_compatibility": "not_evaluated",
        "catalog_plugin_claims_allowed": False,
    }
    report["scope_controls"] = {
        "catalog_plugins_executed": False,
        "app_control_policy_changed": False,
        "host_policy_changed": False,
        "third_party_signing_certificate_required": False,
    }
    report["run_metadata"] = {
        "started_at": started_at,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_head": git_head(),
        "host_platform": platform.platform(),
        "source_sha256": {
            relative: sha256_file(ROOT / relative) for relative in SOURCE_PATHS
        },
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output_path.write_text(serialized, encoding="utf-8", newline="\n")
    sidecar = Path(str(output_path) + ".sha256")
    report_hash = sha256_file(output_path)
    sidecar.write_text(f"{report_hash}  {output_path.name}\n", encoding="ascii", newline="\n")
    print(
        json.dumps(
            {
                "report": str(output_path),
                "report_sha256": report_hash,
                "runner_behavior_status": report["runner_behavior_status"],
                "case_count": len(results),
                "all_cleanup_confirmed": all_cleanup_confirmed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["runner_behavior_status"] == "passed_scoped" and exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
