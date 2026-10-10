"""Run one deterministic case against isolated native and federated harness commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from lab.compare import compare_pair
except ModuleNotFoundError:  # supports `python lab/native_runner.py` as well as module mode
    from compare import compare_pair


def load_case(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("expected"), dict):
        raise ValueError(f"invalid case fixture: {path}")
    return value


def execute_harness(
    command: list[str], case: dict[str, Any], mode: str, timeout_seconds: float
) -> dict[str, Any]:
    if not command:
        return {"status": "environment_missing", "diagnostic": "harness command is empty"}
    environment = os.environ.copy()
    environment["LIGAMINA_RUN_MODE"] = mode
    harness_input = {
        "case_id": case.get("case_id"),
        "stimulus": case["stimulus"],
    }
    try:
        process = subprocess.run(
            command,
            input=json.dumps(harness_input, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        return {"status": "environment_missing", "diagnostic": str(error)}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "diagnostic": "harness timed out"}
    if process.returncode != 0:
        return {
            "status": "failed",
            "diagnostic": f"harness exited with {process.returncode}",
            "stderr": process.stderr[-4000:],
        }
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            "status": "failed",
            "diagnostic": f"harness stdout was not one JSON document: {error}",
            "stderr": process.stderr[-4000:],
        }
    if not isinstance(result, dict):
        return {"status": "failed", "diagnostic": "harness result must be a JSON object"}
    result.setdefault("status", "completed")
    if process.stderr:
        result["diagnostics"] = process.stderr[-4000:]
    return result


def run_pair(
    case: dict[str, Any],
    native_command: list[str],
    federated_command: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be greater than zero")
    native = execute_harness(native_command, case, "native", timeout_seconds)
    federated = execute_harness(federated_command, case, "federated", timeout_seconds)
    comparison = compare_pair(case, native, federated).as_json()
    return {"case_id": case.get("case_id"), "native": native, "federated": federated, "comparison": comparison}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--native-command", required=True, nargs="+")
    parser.add_argument("--federated-command", required=True, nargs="+")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(argv)
    report = run_pair(
        load_case(options.case),
        options.native_command,
        options.federated_command,
        options.timeout,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if options.report:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    statuses = (report["native"].get("status"), report["federated"].get("status"))
    if "environment_missing" in statuses:
        return 2
    return 0 if report["comparison"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
