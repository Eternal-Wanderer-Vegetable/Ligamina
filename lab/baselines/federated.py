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


def _segments(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, str):
        return [{"type": "text", "text": message}]
    if not isinstance(message, list):
        return []
    result = []
    for segment in message:
        if not isinstance(segment, dict):
            raise RuntimeError("federated Gateway captured a malformed message segment")
        kind = segment.get("type", "text")
        if kind == "text":
            result.append({"type": "text", "text": segment.get("text", segment.get("data", {}).get("text", ""))})
        elif kind in {"mention", "at"}:
            result.append({"type": "mention", "user_id": str(segment.get("user_id", segment.get("data", {}).get("qq", "")))})
        elif kind == "reply":
            result.append({"type": "reply", "message_id": str(segment.get("message_id", segment.get("data", {}).get("id", "")))})
        elif kind == "image_ref":
            result.append({"type": "image_ref", "resource_ref": str(segment.get("resource_ref", segment.get("data", {}).get("file", "")))})
        else:
            raise RuntimeError(f"federated Gateway captured unsupported message segment {kind!r}")
    return result


def _phase4_probe_observation(ecosystem: str, work: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    if ecosystem == "nonebot":
        data_root = (work / "nonebot-data").resolve()
        path = (data_root / "phase4-probes" / "observations.ndjson").resolve()
        path.relative_to(data_root)
        raw = path.read_bytes()
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
        event_by_message_id = {
            str(event.get("extensions", {}).get("onebot.v11", {}).get("message_id", "")): event
            for event in events
        }
        observations = []
        for row in rows:
            event = event_by_message_id.get(str(row.get("message_id", "")))
            if event is None:
                raise RuntimeError("phase4 observer wrote an unbound OneBot message id")
            observations.append({
                "event_id": str(event["event_id"]),
                "account_id": row["account_id"],
                "actor_id": row["actor_id"],
                "conversation": row["conversation"],
                "timestamp": row["timestamp"],
            })
        source_path = Path(__file__).resolve().parent / "nonebot" / "fixtures" / "phase4_probe_observer.py"
    else:
        data_root = (work / "koishi-data").resolve()
        path = (data_root / "phase4-probes" / "observations.ndjson").resolve()
        path.relative_to(data_root)
        raw = path.read_bytes()
        observations = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
        source_path = Path(__file__).resolve().parent / "koishi" / "phase4_probe_plugin.mjs"
    if len(observations) != len(events):
        raise RuntimeError("phase4 observer did not persist exactly one record per dispatched framework event")
    if [row.get("event_id") for row in observations] != [str(event["event_id"]) for event in events]:
        raise RuntimeError("phase4 observer event ids differ from the ordered stimulus")
    return {
        "status": "recorded",
        "storage": {
            "relative_path": str(path.relative_to(work.resolve())).replace("\\", "/"),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "record_count": len(observations),
            "probe_source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        },
        "events": observations,
    }


def _seed_koishi_users(node: str, profile_work: Path, data_work: Path, users: list[dict[str, Any]]) -> None:
    if not users:
        return
    directory = data_work / "koishi-data"
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "koishi.sqlite"
    script = r'''const { createRequire } = require('node:module')
const requireFromProfile = createRequire(process.cwd() + '/package.json')
const { Context } = requireFromProfile('@koishijs/core')
const driverModule = requireFromProfile('@minatojs/driver-sqlite')
const SQLiteDriver = driverModule.default ?? driverModule
const users = JSON.parse(process.argv[2])
;(async () => {
  const app = new Context()
  app.plugin(SQLiteDriver, { path: process.argv[1] })
  await app.start()
  try {
    for (const user of users) await app.database.createUser(user.platform, user.id, { authority: user.authority })
  } finally {
    await app.stop()
  }
})().catch((error) => { console.error(error); process.exitCode = 1 })
'''
    result = subprocess.run(
        [node, "-e", script, str(database), json.dumps(users, ensure_ascii=False)],
        cwd=profile_work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"federated Koishi setup could not seed reviewed authority rows: {result.stderr[-1500:]}")


def run(request: dict[str, Any]) -> dict[str, Any]:
    context = request["context"]
    root = Path(context["repo_root"]).resolve()
    site.addsitedir(context["site_packages"])
    sys.path.insert(0, str(root))
    os.environ.update(context.get("sample_environment", {}))
    from lab.representative import run_federated

    sample = dict(context["sample"])
    work = Path(context.get("execution", {}).get("workspace", context["work_dir"])).resolve()
    profile_work = Path(context["work_dir"]).resolve()
    environment = dict(context["host_environment"])
    behavior_fixture = context.get("behavior_fixture")
    fixture_events = request["stimulus"]["events"]
    if behavior_fixture:
        if behavior_fixture.get("adapter", {}).get("id") != f"{sample['ecosystem']}.onebot.v11":
            raise ValueError("v2 fixture adapter does not match the selected Federated Host")
        sample["sample_id"] = f"phase4-{context['binding']['registry_entry_id']}-{request['case_id'].rsplit('-', 1)[-1]}"
        if sample["ecosystem"] == "nonebot":
            sample["preload_plugins"] = [*sample.get("preload_plugins", []), "phase4_random_seed", "phase4_probe_observer"]
            environment["LIGAMINA_BEHAVIOR_RANDOM_SEED"] = str(behavior_fixture["determinism"]["random_seed"])
            data_directory = work / "nonebot-data"
            environment["LIGAMINA_NONEBOT_DATA_DIR"] = str(data_directory)
            environment["LIGAMINA_P4_OBSERVATION_FILE"] = str(data_directory / "phase4-probes" / "observations.ndjson")
            config = behavior_fixture.get("setup", {}).get("plugin_config", {})
            if sample.get("package") == "nonebot_plugin_help":
                if (
                    set(config) != {"help_at_sender", "help_ignore_plugins"}
                    or not isinstance(config["help_at_sender"], bool)
                    or config["help_ignore_plugins"] != ["phase4_random_seed", "phase4_probe_observer"]
                ):
                    raise ValueError("v2 NoneBot Help config is outside the reviewed allowlist")
                (profile_work / ".env.prod").write_text(
                    "\n".join(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}" for key, value in sorted(config.items())) + "\n",
                    encoding="utf-8",
                )
                sample["fixture"] = {"nonebot_config": config}
            elif config:
                if (
                    set(config) != {"name", "limit", "name_list"}
                    or not isinstance(config["name"], str)
                    or not isinstance(config["limit"], int)
                    or isinstance(config["limit"], bool)
                    or not isinstance(config["name_list"], list)
                    or any(not isinstance(item, str) for item in config["name_list"])
                ):
                    raise ValueError("v2 NoneBot config is outside the reviewed plugin-config allowlist")
                environment.update(
                    {
                        "GUESS_NAME": config["name"],
                        "GUESS_LIMIT": str(config["limit"]),
                        "GUESS_NAME_LIST": json.dumps(config["name_list"], ensure_ascii=False),
                    }
                )
                sample["fixture"] = {"nonebot_config": {}}
            fixture_directory = Path(__file__).resolve().parent / "nonebot" / "fixtures"
            python_path = environment.get("PYTHONPATH", str(root / "hosts/nonebot"))
            environment["PYTHONPATH"] = os.pathsep.join((python_path, str(fixture_directory)))
        else:
            config = behavior_fixture["setup"].get("plugin_config", {})
            sample["fixture"] = {"plugin_config": config}
            module_url = context["koishi_entry_url"]
            probe_url = (root / "lab/baselines/koishi/phase4_probe_plugin.mjs").resolve().as_uri()
            environment["LIGAMINA_KOISHI_PLUGINS"] = ",".join((module_url, probe_url))
            environment["LIGAMINA_KOISHI_PLUGIN_CONFIG"] = json.dumps({module_url: config, probe_url: {}}, ensure_ascii=False)
            users = []
            for key, authority in behavior_fixture["setup"].get("user_authority", {}).items():
                platform, separator, user_id = key.partition(":")
                if not separator or platform != "onebot.v11" or not user_id or not isinstance(authority, int) or isinstance(authority, bool) or not 0 <= authority <= 5:
                    raise ValueError("v2 Koishi authority setup is outside the reviewed allowlist")
                users.append({"platform": platform, "id": user_id, "authority": authority})
            _seed_koishi_users(context["node_executable"], profile_work, work, users)
    events = []
    for event in fixture_events:
        platform = "onebot.v11" if sample["ecosystem"] == "nonebot" else "koishi"
        extensions = dict(event.get("extensions", {}))
        if "onebot.v11" not in extensions and event.get("extensions", {}).get("onebot.v11"):
            extensions["onebot.v11"] = event["extensions"]["onebot.v11"]
        events.append({
            "schema_version": "ligamina/0.1",
            "event_id": str(event["event_id"]),
            "trace_id": f"trace-{event['event_id']}",
            "platform": platform,
            "account_id": str(event["account_id"]),
            "conversation": {"kind": event["conversation"]["kind"], "id": str(event["conversation"]["id"])},
            "actor_id": str(event["actor_id"]),
            "timestamp": event["timestamp"],
            "target_host": f"sample-{sample.get('sample_id', sample['package'])}",
            "message": event["message"],
            "extensions": extensions,
        })
    if sample["ecosystem"] == "koishi" and not behavior_fixture:
        module_url = context["koishi_entry_url"]
        environment["LIGAMINA_KOISHI_PLUGINS"] = module_url
        config = sample.get("fixture", {}).get("plugin_config")
        if config:
            environment["LIGAMINA_KOISHI_PLUGIN_CONFIG"] = json.dumps({module_url: config}, ensure_ascii=False)
    trace = run_federated(sample, work, environment, events, profile_work=profile_work)
    if trace.get("cleanup", {}).get("host_alive_after_shutdown") is not False:
        raise RuntimeError("federated Host process cleanup was not confirmed")

    messages = []
    receipts = []
    records = trace.get("gateway_operations", [])
    event_by_trace = {f"trace-{event['event_id']}": str(event["event_id"]) for event in events}
    for index, record in enumerate(records, start=1):
        operation = record.get("operation", {})
        payload = operation.get("operation_payload", {})
        target = payload.get("target")
        raw_message = payload.get("params", {}).get("message")
        message = _segments(raw_message) if behavior_fixture else [{"type": "text", "text": _text(raw_message)}]
        if not isinstance(target, dict) or target.get("kind") not in {"group", "private"}:
            raise RuntimeError("federated Gateway captured an invalid message target")
        operation_id = f"operation-{index}"
        messages.append({
            "operation_id": operation_id,
            "account_id": payload.get("account_id"),
            "operation": payload.get("operation"),
            "target": target,
            "params": {"message": message},
        })
        if behavior_fixture:
            trace_id = operation.get("trace_id")
            messages[-1]["source_event_id"] = event_by_trace.get(trace_id)
        receipt_doc = record.get("receipt", {})
        receipt = receipt_doc.get("result", receipt_doc)
        if receipt.get("status") != "succeeded":
            raise RuntimeError(f"federated Gateway receipt was not successful: {receipt_doc}")
        receipt_record = {
            "operation_id": operation_id,
            "status": receipt["status"],
            "target": target,
        }
        if behavior_fixture:
            receipt_record["account_id"] = str(payload.get("account_id", ""))
            receipt_record["source_event_id"] = messages[-1].get("source_event_id")
        receipts.append(receipt_record)

    result = {
        "status": "completed",
        "environment": _environment(context, request),
        "events": events,
        "operations": messages,
        "operation_receipts": receipts,
        "cleanup": {"active_capability_count": 0, "live_processes": []},
        "federated_trace": {
            "core_host_route": "passed",
            "host_pid_reaped": trace.get("cleanup", {}).get("host_alive_after_shutdown") is False,
            "gateway_operation_count": len(messages),
        },
    }
    if behavior_fixture:
        result["binding"] = context.get("binding")
        result["capability_probe_observation"] = _phase4_probe_observation(sample["ecosystem"], work, fixture_events)
        result["plugin_observation"] = {
            "events": [
                {"event_id": event["event_id"], "account_id": event["account_id"], "actor_id": event["actor_id"], "conversation": event["conversation"]}
                for event in fixture_events
            ],
            "operations": messages,
            "receipts": receipts,
            "response_or_operation_count": len(messages),
        }
    return result


def main() -> int:
    request = json.load(sys.stdin)
    print(json.dumps(run(request), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
