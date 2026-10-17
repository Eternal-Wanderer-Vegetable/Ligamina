"""Run the installed sample through the production Core/Host/Gateway route."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import site
import subprocess
import sys
from pathlib import Path
from typing import Any


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hash(path: Path, algorithm: str) -> str:
    if algorithm == "sha256":
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if algorithm == "sri-sha512":
        import base64

        return "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode("ascii")
    raise ValueError(f"unsupported artifact digest algorithm: {algorithm}")


def _environment(context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    paths = context["fingerprint_paths"]
    sample = context["sample"]
    artifact = Path(paths["artifact"])
    artifact_hash = _artifact_hash(artifact, sample["_artifact_hash_algorithm"])
    if artifact_hash != sample["_artifact_hash"]:
        raise RuntimeError("federated artifact bytes no longer match the frozen registry digest")
    if sample["ecosystem"] == "nonebot":
        site.addsitedir(context["site_packages"])
        framework = importlib.metadata.version("nonebot2")
        runtime = ".".join(map(str, sys.version_info[:3]))
    else:
        lock = json.loads(Path(paths["lockfile"]).read_text(encoding="utf-8"))
        framework = lock.get("packages", {}).get("node_modules/@koishijs/core", {}).get("version")
        node = context["node_executable"]
        completed = subprocess.run(
            [node, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False
        )
        if completed.returncode:
            raise RuntimeError(f"could not read Node runtime version: {completed.stderr[-1000:]}")
        runtime = completed.stdout.strip().removeprefix("v")
    fingerprints = {
        "ecosystem": sample["ecosystem"],
        "framework_version": framework,
        "runtime_version": runtime,
        "lockfile_sha256": _sha256(Path(paths["lockfile"])),
        "profile_sha256": _sha256(Path(paths["profile"])),
        "artifact_hash": artifact_hash,
        "config_sha256": hashlib.sha256(_canonical(context["config"])).hexdigest(),
        "stimulus_sha256": hashlib.sha256(_canonical(request["stimulus"])).hexdigest(),
    }
    if fingerprints != context["expected_environment"]:
        raise RuntimeError(f"federated fingerprint mismatch: expected {context['expected_environment']}, observed {fingerprints}")
    return fingerprints


def _text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(_text(segment) for segment in message)
    if isinstance(message, dict):
        if message.get("type") not in (None, "text"):
            return ""
        for key in ("text", "data", "content", "message", "elements"):
            if key in message:
                return _text(message[key])
    return ""


def run(request: dict[str, Any]) -> dict[str, Any]:
    context = request["context"]
    root = Path(context["repo_root"]).resolve()
    site.addsitedir(context["site_packages"])
    sys.path.insert(0, str(root))
    os.environ.update(context.get("sample_environment", {}))
    from lab.representative import run_federated

    sample = context["sample"]
    work = Path(context["work_dir"]).resolve()
    environment = context["host_environment"]
    if sample["ecosystem"] == "koishi":
        environment = dict(environment)
        module_url = context["koishi_entry_url"]
        environment["LIGAMINA_KOISHI_PLUGINS"] = module_url
        config = sample.get("fixture", {}).get("plugin_config")
        if config:
            environment["LIGAMINA_KOISHI_PLUGIN_CONFIG"] = json.dumps({module_url: config}, ensure_ascii=False)
    trace = run_federated(sample, work, environment, request["stimulus"]["events"])
    if trace.get("cleanup", {}).get("host_alive_after_shutdown") is not False:
        raise RuntimeError("federated Host process cleanup was not confirmed")

    messages = []
    receipts = []
    records = trace.get("gateway_operations", [])
    for index, record in enumerate(records, start=1):
        operation = record.get("operation", {})
        payload = operation.get("operation_payload", {})
        target = payload.get("target")
        text = _text(payload.get("params", {}).get("message"))
        if not isinstance(target, dict) or target.get("kind") not in {"group", "private"}:
            raise RuntimeError("federated Gateway captured an invalid message target")
        operation_id = f"operation-{index}"
        messages.append({
            "operation_id": operation_id,
            "account_id": payload.get("account_id"),
            "operation": payload.get("operation"),
            "target": target,
            "params": {"message": [{"type": "text", "text": text}]},
        })
        receipt_doc = record.get("receipt", {})
        receipt = receipt_doc.get("result", receipt_doc)
        if receipt.get("status") != "succeeded":
            raise RuntimeError(f"federated Gateway receipt was not successful: {receipt_doc}")
        receipts.append({"operation_id": operation_id, "status": receipt["status"], "target": target})

    return {
        "status": "completed",
        "environment": _environment(context, request),
        "events": request["stimulus"]["events"],
        "operations": messages,
        "operation_receipts": receipts,
        "cleanup": {"active_capability_count": 0, "live_processes": []},
        "federated_trace": {
            "core_host_route": "passed",
            "host_pid_reaped": trace.get("cleanup", {}).get("host_alive_after_shutdown") is False,
            "gateway_operation_count": len(messages),
        },
    }


def main() -> int:
    request = json.load(sys.stdin)
    print(json.dumps(run(request), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
