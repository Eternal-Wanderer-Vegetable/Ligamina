"""Run the P7-04 stock-Native x2 versus Federated pilot in admitted Linux Workers.

The orchestrator selects exact load-verified catalog identities, prepares locked
offline inputs, generates command scenarios, and compares observations. Workers
receive plugin/profile/runtime inputs and scenarios only; comparison and reports
stay outside the Worker boundary.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator

from lab.automation import phase4_campaign
from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT, _canonical
from lab.automation.phase4_docker_side import _docker_side
from lab.automation.p7_contracts import CONTRACT_IDS, _pilot_entry_contracts
from lab.capability_inventory import SCANNER_VERSION
from lab.pilot_nonebot import _prepare_wheelhouse
from lab.pilot_workers import _load_npm_lock, _prepare_npm_project
from lab.scenario_generator import (
    GENERATOR_VERSION,
    compare_repeated_observations,
    generate_scenarios,
    normalize_observation,
    object_sha256,
)

MATRIX_PATH = ROOT / "outputs/compat/compatibility-matrix-v1.json"
DESCRIPTOR_SCHEMA_PATH = ROOT / "contracts/v0.2/command-descriptor.schema.json"
ADMISSION_PATH = ROOT / "outputs/acceptance/phase7-linux-admission-20261110.json"
DEFAULT_SELECTION = ROOT / "outputs/acceptance/p7-p704-pilot-selection-20261110.json"
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/p7-p704-differential-pilot-20261110.json"
DEFAULT_BUNDLE_ROOT = ARTIFACT_CACHE / "p7-p704-differential-bundles-20261110"
ECOSYSTEMS = ("koishi", "nonebot")
FIXED_SEED = 20261110
ROUNDS = 3


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_sha256(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"runtime artifact tree contains a symbolic link: {path}")
        if path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)})
    return _sha256_bytes(_canonical(rows))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _frozen_records() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog = _read(CATALOG_ROOT / "catalog.json")
    artifacts = _read(CATALOG_ROOT / "artifacts.lock.json")
    profiles = _read(CATALOG_ROOT / "profiles.lock.json")
    return (
        {row["registry_entry_id"]: row for row in catalog["entries"]},
        {row["registry_entry_id"]: row for row in artifacts["entries"]},
        {row["registry_entry_id"]: row for row in profiles["entries"]},
    )


def select_candidates(selection: dict[str, Any], *, limit_per_ecosystem: int | None = None) -> list[dict[str, Any]]:
    """Validate the 30-per-ecosystem static selection against load and lock evidence."""
    if selection.get("kind") != "p7_p701_static_inventory_sample":
        raise ValueError("selection is not a source-bound P7-01 inventory sample")
    matrix_bytes = MATRIX_PATH.read_bytes()
    if selection.get("selection", {}).get("source_matrix_sha256") != _sha256_bytes(matrix_bytes):
        raise ValueError("P7 pilot selection was built from a different compatibility matrix")
    matrix = json.loads(matrix_bytes)
    by_id = {row["registry_entry_id"]: row for row in matrix["entries"]}
    catalog_by_id, artifact_by_id, profile_by_id = _frozen_records()
    candidates = []
    for sample in selection.get("samples", []):
        ecosystem = sample.get("ecosystem")
        if ecosystem not in ECOSYSTEMS:
            continue
        identity = sample.get("entry", {})
        entry_id = identity.get("registry_entry_id")
        row = by_id.get(entry_id)
        catalog_entry = catalog_by_id.get(entry_id)
        artifact_record = artifact_by_id.get(entry_id)
        profile_record = profile_by_id.get(entry_id)
        if not all(isinstance(value, dict) for value in (row, catalog_entry, artifact_record, profile_record)):
            raise ValueError(f"frozen source is incomplete for P7 pilot entry {entry_id}")
        load = row.get("capabilities", {}).get("load", {}).get("linux", {})
        if (
            row.get("ecosystem") != ecosystem
            or row.get("package") != identity.get("package")
            or row.get("version") != identity.get("version")
            or load.get("value") != "verified"
            or not load.get("evidence")
        ):
            raise ValueError(f"selection is not bound to an exact Linux load-verified identity: {entry_id}")
        artifact_platform = "all" if ecosystem == "koishi" else "linux-x86_64"
        artifact_value = next(
            (item.get("artifact") for item in artifact_record.get("artifacts", [])
             if item.get("platform") == artifact_platform and isinstance(item.get("artifact"), dict)),
            None,
        )
        profile_value = profile_record.get("profiles", {}).get("linux-x86_64", {}).get("dependency_lock")
        if not isinstance(artifact_value, dict) or not isinstance(profile_value, dict):
            raise ValueError(f"selection has no exact Linux artifact/profile lock: {entry_id}")
        artifact_path = (ARTIFACT_CACHE / artifact_value["cache_path"]).resolve(strict=True)
        lock_path = (ARTIFACT_CACHE / profile_value["cache_path"]).resolve(strict=True)
        artifact_hash = _sha256_file(artifact_path)
        if artifact_hash != identity.get("artifact_sha256"):
            raise ValueError(f"pilot artifact differs from the selected SHA-256: {entry_id}")
        if _sha256_file(lock_path) != profile_value.get("sha256"):
            raise ValueError(f"pilot profile lock differs from its frozen SHA-256: {entry_id}")
        candidates.append({
            "ecosystem": ecosystem,
            "registry_entry_id": entry_id,
            "package": identity["package"],
            "version": identity["version"],
            "artifact_sha256": artifact_hash,
            "artifact_path": artifact_path,
            "artifact_filename": artifact_value.get("filename"),
            "artifact_record": artifact_record,
            "distribution_name": artifact_record.get("distribution_name") or identity["package"],
            "profile_lock_path": lock_path,
            "profile_lock_sha256": profile_value["sha256"],
            "profile_id": profile_value.get("profile_id"),
            "catalog_entry": catalog_entry,
            "load_evidence": load["evidence"],
            "static_inventory": sample["static_inventory"],
            "static_inventory_sha256": object_sha256(sample["static_inventory"]),
            "static_command_count": len(sample["static_inventory"]["capabilities"]["commands"]["items"]),
        })
    candidates.sort(key=lambda item: (item["ecosystem"], item["registry_entry_id"]))
    counts = {eco: sum(item["ecosystem"] == eco for item in candidates) for eco in ECOSYSTEMS}
    requested = limit_per_ecosystem or 30
    if any(count < requested for count in counts.values()):
        raise ValueError(f"P7 pilot selection is below the requested per-ecosystem count: {counts}")
    if limit_per_ecosystem is not None:
        candidates = [
            item for eco in ECOSYSTEMS
            for item in [row for row in candidates if row["ecosystem"] == eco][:limit_per_ecosystem]
        ]
    if limit_per_ecosystem is None and counts != {"koishi": 30, "nonebot": 30}:
        raise ValueError(f"full P7-04 pilot requires exactly 30 load-verified entries per ecosystem: {counts}")
    return candidates


def prepare_bundle(candidate: dict[str, Any], *, bundle_root: Path, core_binary: Path) -> dict[str, Any]:
    """Prepare one exact plugin/profile bundle; no plugin code is imported or run here."""
    entry_id = candidate["registry_entry_id"]
    bundle = bundle_root / entry_id
    if bundle.exists():
        raise FileExistsError(f"refusing to overwrite an existing P7 Worker bundle: {bundle}")
    project = bundle / "project"
    sample = project / "sample"
    phase4_campaign._copy_python_sources(project)
    sample.mkdir(parents=True)
    descriptor = candidate.get("profile_document_path") or (ROOT / "samples/profiles" / (
        "nonebot-group-v0.1.json" if candidate["ecosystem"] == "nonebot" else "koishi-group-v0.1.json"
    ))
    descriptor = Path(descriptor).resolve(strict=True)
    shutil.copy2(descriptor, project / "samples/profiles" / descriptor.name)
    shutil.copy2(candidate["artifact_path"], sample / "artifact.bin")
    _link_or_copy(core_binary, sample / "ligamina-cli")
    sample.joinpath("ligamina-cli").chmod(0o755)
    runtime_artifacts: dict[str, Any] = {"core_cli_sha256": _sha256_file(sample / "ligamina-cli")}
    asset_summary: dict[str, Any]

    if candidate["ecosystem"] == "nonebot":
        lock_raw = candidate["profile_lock_path"].read_bytes()
        direct = {
            "distribution_name": candidate["distribution_name"],
            "version": candidate["version"],
            "filename": candidate["artifact_filename"],
            "sha256": candidate["artifact_sha256"],
        }
        stats = _prepare_wheelhouse(
            lock_raw.decode("utf-8"),
            cache_root=ARTIFACT_CACHE,
            wheelhouse=sample / "wheelhouse",
            direct_artifact=direct,
            direct_artifact_path=candidate["artifact_path"],
        )
        (sample / "requirements.txt").write_bytes(lock_raw)
        asset_summary = {
            "kind": "hash_locked_python_wheelhouse",
            "profile_lock_sha256": candidate["profile_lock_sha256"],
            "wheel_count": sum(1 for path in (sample / "wheelhouse").iterdir() if path.is_file()),
            "dependency_stats": stats,
        }
    else:
        lock, lock_raw = _load_npm_lock(candidate["profile_lock_path"], {
            "sha256": candidate["profile_lock_sha256"],
            "size_bytes": candidate["profile_lock_path"].stat().st_size,
        })
        project_summary = _prepare_npm_project(
            lock,
            package_name=candidate["package"],
            cache_root=ARTIFACT_CACHE,
            project_dir=sample,
        )
        (sample / "original-package-lock.json").write_bytes(lock_raw)
        runtime_artifacts.update({
            "koishi_install": "localized_tarballs",
            "localized_package_lock_sha256": _sha256_file(sample / "package-lock.json"),
        })
        host_dist = ROOT / "hosts/koishi/dist"
        if not host_dist.is_dir():
            raise FileNotFoundError("current Koishi Host dist is missing; build it with npm run build")
        shutil.copytree(host_dist, sample / "koishi-host-dist")
        runtime_artifacts["koishi_host_dist_sha256"] = _tree_sha256(sample / "koishi-host-dist")
        asset_summary = {
            "kind": "hash_locked_local_npm_tarballs",
            "profile_lock_sha256": candidate["profile_lock_sha256"],
            "raw_package_lock_sha256": _sha256_bytes(lock_raw),
            "localized_package_lock_sha256": runtime_artifacts["localized_package_lock_sha256"],
            "dependency_stats": project_summary,
        }

    context_files = {
        "artifact_sha256": _sha256_file(sample / "artifact.bin"),
        "core_cli_sha256": runtime_artifacts["core_cli_sha256"],
        "profile_sha256": _sha256_file(descriptor),
        "host_lock_sha256": _sha256_file(ROOT / "hosts/koishi/package-lock.json") if candidate["ecosystem"] == "koishi" else None,
        "koishi_host_dist_sha256": runtime_artifacts.get("koishi_host_dist_sha256"),
    }
    manifest = {
        "schema_version": 1,
        "registry_entry_id": entry_id,
        "ecosystem": candidate["ecosystem"],
        "artifact_sha256": candidate["artifact_sha256"],
        "profile_lock_sha256": candidate["profile_lock_sha256"],
        "runtime_artifacts": runtime_artifacts,
        "profile_document": f"project/samples/profiles/{descriptor.name}",
        "service_profile_sha256": candidate.get("service_profile_sha256"),
        "files": [
            {"path": path.relative_to(bundle).as_posix(), "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            for path in sorted(bundle.rglob("*")) if path.is_file()
        ],
    }
    manifest_sha = object_sha256(manifest)
    manifest["manifest_sha256"] = manifest_sha
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "path": str(bundle),
        "manifest_sha256": manifest_sha,
        "runtime_artifacts": runtime_artifacts,
        "context_files": context_files,
        "asset_summary": asset_summary,
        "bundle_bytes": sum(path.stat().st_size for path in bundle.rglob("*") if path.is_file()),
    }


def validate_bundle(bundle: Path, candidate: dict[str, Any], expected_sha256: str) -> None:
    manifest = _read(bundle / "bundle-manifest.json")
    claimed = manifest.pop("manifest_sha256", None)
    if claimed != expected_sha256 or object_sha256(manifest) != expected_sha256:
        raise ValueError(f"P7 bundle manifest hash mismatch: {candidate['registry_entry_id']}")
    if manifest.get("registry_entry_id") != candidate["registry_entry_id"]:
        raise ValueError("P7 bundle manifest entry identity mismatch")
    for record in manifest.get("files", []):
        path = bundle / record["path"]
        if not path.is_file() or path.stat().st_size != record["size_bytes"] or _sha256_file(path) != record["sha256"]:
            raise ValueError(f"P7 bundle input changed after preparation: {record['path']}")


def _tool_name_part(value: str) -> str:
    safe = "".join(char if char.isascii() and (char.isalnum() or char in "_-.") else "_" for char in value)
    if safe == value:
        return safe
    return f"{safe}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def _native_host_id(candidate: dict[str, Any]) -> str:
    return f"sample-p7-{candidate['registry_entry_id']}"


def _native_sample(candidate: dict[str, Any]) -> dict[str, Any]:
    catalog = candidate["catalog_entry"]
    entry = candidate["package"]
    if candidate["ecosystem"] == "nonebot":
        entry = catalog.get("entry_point") or catalog.get("registry_identity", {}).get("module_name") or entry
    return {
        "sample_id": f"p7-{candidate['registry_entry_id']}",
        "ecosystem": candidate["ecosystem"],
        "package": candidate["package"],
        "version": candidate["version"],
        "_entry": entry,
        "_artifact_hash": candidate["artifact_sha256"],
        "_artifact_hash_algorithm": "sha256",
        "preload_plugins": list(candidate.get("preload_plugins", [])),
        "fixture": {"plugin_config": {}},
    }


def _build_context(
    candidate: dict[str, Any],
    bundle: Path,
    runtime_artifacts: dict[str, Any],
    stimulus: dict[str, Any],
    *,
    mode: str,
    seed: int,
    discover_commands: bool,
) -> dict[str, Any]:
    ecosystem = candidate["ecosystem"]
    sample = _native_sample(candidate)
    config = {
        "ecosystem": ecosystem,
        "package": candidate["package"],
        "version": candidate["version"],
        "entry": sample["_entry"],
        "preload_plugins": [],
        "host_profile": phase4_campaign.HOST_VERSIONS[ecosystem],
        "nonebot_config": {},
        "plugin_config": {},
        "environment": {},
    }
    profile = "nonebot-group-v0.1.json" if ecosystem == "nonebot" else "koishi-group-v0.1.json"
    profile_document = candidate.get("profile_document_path")
    profile_document_name = Path(profile_document).name if profile_document else profile
    lock_path = "requirements.txt" if ecosystem == "nonebot" else "original-package-lock.json"
    profile_path = Path(profile_document) if profile_document else ROOT / "samples/profiles" / profile
    config["preload_plugins"] = list(candidate.get("preload_plugins", []))
    expected = {
        "ecosystem": ecosystem,
        "framework_version": "2.5.0" if ecosystem == "nonebot" else "4.18.11",
        "runtime_version": "3.12.14" if ecosystem == "nonebot" else "24.20.0",
        "lockfile_sha256": candidate["profile_lock_sha256"],
        "profile_sha256": _sha256_file(profile_path),
        "artifact_hash": candidate["artifact_sha256"],
        "config_sha256": _sha256_bytes(_canonical(config)),
        "stimulus_sha256": _sha256_bytes(_canonical(stimulus)),
    }
    context: dict[str, Any] = {
        "repo_root": "/input/project",
        "work_dir": "/work/profile",
        "site_packages": "/work/profile/site-packages",
        "node_executable": "/usr/local/bin/node",
        "sample": sample,
        "fixture": {"plugin_config": {}},
        "config": config,
        "sample_environment": {},
        "host_environment": {},
        "koishi_entry_url": "",
        "expected_environment": expected,
        "fingerprint_paths": {
            "artifact": "/input/project/sample/artifact.bin",
            "lockfile": f"/input/project/sample/{lock_path}",
            "profile": f"/input/project/samples/profiles/{profile_document_name}",
        },
        "profile_document": f"samples/profiles/{profile_document_name}",
        "service_profile": copy.deepcopy(candidate.get("service_profile", {})),
        "execution": {
            "mode": mode,
            "random_seed": seed,
            "capture_source_event_id": True,
            "discover_commands": discover_commands,
            "required_services": list(candidate.get("required_services", [])),
            "workspace": "/work/side",
        },
        "runtime_artifacts": copy.deepcopy(runtime_artifacts),
    }
    if ecosystem == "koishi":
        context["host_lock_sha256"] = _sha256_file(ROOT / "hosts/koishi/package-lock.json")
    return context


def _event_from_scenario(scenario: dict[str, Any], *, ecosystem: str) -> dict[str, Any]:
    step = scenario["steps"][0]
    if step["kind"] == "event":
        event = copy.deepcopy(step["event"])
    else:
        invocation = str(step["invocation"])
        arguments = str(step.get("arguments", ""))
        message = " ".join(part for part in (invocation, arguments) if part)
        scenario_id = scenario["scenario_id"]
        event_id = f"event-{scenario_id[4:]}"
        event = {
            "schema_version": "ligamina/0.1",
            "event_id": event_id,
            "trace_id": f"trace-{event_id}",
            "platform": "onebot.v11" if ecosystem == "nonebot" else "koishi",
            "account_id": scenario["actors"][0]["account_id"],
            "conversation": {"kind": "private", "id": scenario["conversations"][0]["conversation_id"]},
            "actor_id": scenario["actors"][0]["actor_id"],
            "timestamp": scenario["controls"]["fixed_time_utc"],
            "target_host": step["host_id"],
            "message": [{"type": "text", "text": message}],
            "extensions": {},
        }
    if ecosystem == "nonebot":
        actor_seed = hashlib.sha256(str(event["actor_id"]).encode("utf-8")).hexdigest()[:8]
        actor_id = str(int(actor_seed, 16) % 1_000_000_000 + 1)
        account_id = "100000"
        message_id = str(int(hashlib.sha256(str(event["event_id"]).encode("utf-8")).hexdigest()[:8], 16) + 1)
        event["actor_id"] = actor_id
        event["account_id"] = account_id
        extensions = event.get("extensions") if isinstance(event.get("extensions"), dict) else {}
        onebot = extensions.get("onebot.v11") if isinstance(extensions.get("onebot.v11"), dict) else {}
        onebot["message_id"] = message_id
        extensions["onebot.v11"] = onebot
        event["extensions"] = extensions
        event["platform"] = "onebot.v11"
        event["conversation"] = {"kind": "private", "id": actor_id}
    else:
        event["platform"] = "koishi"
    return event


def build_scenario_batch(candidate: dict[str, Any], runtime_commands: list[dict[str, Any]], *, seed: int = FIXED_SEED) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    schema = _read(DESCRIPTOR_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    events: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    command_descriptors = []
    invalid_descriptors = 0
    for descriptor in runtime_commands:
        if list(validator.iter_errors(descriptor)):
            invalid_descriptors += 1
            continue
        command_descriptors.append(descriptor)
    inventory_sha = object_sha256({
        "ecosystem": candidate["ecosystem"],
        "registry_entry_id": candidate["registry_entry_id"],
        "artifact_sha256": candidate["artifact_sha256"],
        "collector": "stock-native-runtime-command-reader/1",
        "commands": command_descriptors,
    })
    for descriptor in command_descriptors:
        batch = generate_scenarios(
            inventory_sha256=inventory_sha,
            descriptor=descriptor,
            host_id=candidate["ecosystem"],
            seed=seed,
            max_scenarios=16,
        )
        for scenario in batch.scenarios:
            event = _event_from_scenario(scenario, ecosystem=candidate["ecosystem"])
            events.append(event)
            scenario_step = scenario["steps"][0]
            raw = scenario_step.get("arguments", "")
            scenarios.append({
                "scenario_id": scenario["scenario_id"],
                "event_id": event["event_id"],
                "command_id": descriptor["command_id"],
                "descriptor_sha256": object_sha256(descriptor),
                "kind": scenario["case"]["kind"],
                "arguments_bytes": len(str(raw).encode("utf-8")),
                "arguments_nonempty": bool(str(raw).strip()),
                "input_sha256": object_sha256(scenario_step),
                "batch_sha256": object_sha256(batch.to_dict()),
                "pairwise": batch.pairwise,
            })
    if len({event["event_id"] for event in events}) != len(events):
        raise ValueError("generated P7 pilot scenarios contain duplicate event IDs")
    return events, {
        "inventory_sha256": inventory_sha,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "commands_valid": len(command_descriptors),
        "commands_invalid": invalid_descriptors,
        "scenarios": scenarios,
        "scenario_count": len(scenarios),
    }


def _build_request(
    candidate: dict[str, Any],
    stimulus: dict[str, Any],
    runtime_artifacts: dict[str, Any],
    *,
    mode: str,
    seed: int,
    discover_commands: bool,
    case_id: str,
) -> dict[str, Any]:
    context = _build_context(
        candidate,
        Path("."),
        runtime_artifacts,
        stimulus,
        mode=mode,
        seed=seed,
        discover_commands=discover_commands,
    )
    return {"case_id": case_id, "stimulus": stimulus, "context": context}


def _run_side(
    request: dict[str, Any], candidate: dict[str, Any], bundle: Path, runtime_image: str,
    *, campaign_id: str, round_no: int, side: str,
) -> dict[str, Any]:
    options = argparse.Namespace(
        image=runtime_image,
        bundle=bundle,
        run_id=campaign_id,
        entry_id=candidate["registry_entry_id"],
        attempt_id=f"p7-{candidate['registry_entry_id']}-r{round_no}-{side}",
        generation=round_no,
        side=side,
    )
    started = time.monotonic()
    try:
        result = _docker_side(copy.deepcopy(request), options)
    except Exception as error:
        result = {"status": "failed", "diagnostic": f"{type(error).__name__}: {error}", "cleanup": {"worker_cleanup_confirmed": False}}
    result["orchestrator_elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def _worker_summary(result: dict[str, Any]) -> dict[str, Any]:
    outer = result.get("outer_worker", {}) if isinstance(result.get("outer_worker"), dict) else {}
    setup = result.get("profile_setup", {}) if isinstance(result.get("profile_setup"), dict) else {}
    cleanup = result.get("cleanup", {}) if isinstance(result.get("cleanup"), dict) else {}
    inventory = result.get("stock_native", {}).get("runtime_commands")
    runtime_inventory = result.get("stock_native", {}).get("runtime_inventory", {})
    service_items = runtime_inventory.get("capabilities", {}).get("services", {}).get("items", [])
    runtime_services = sorted({
        item["name"] for item in service_items
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    })
    runtime_plugins = result.get("stock_native", {}).get("runtime_plugins", [])
    command_inventory = result.get("command_inventory", {})
    return {
        "status": result.get("status"),
        "reason_code": result.get("reason_code") or result.get("worker_failure", {}).get("reason_code"),
        "profile_setup_status": setup.get("status"),
        "profile_setup_kind": setup.get("kind"),
        "outer_worker": {
            "status": outer.get("status"),
            "reason_code": outer.get("reason_code"),
            "elapsed_seconds": outer.get("elapsed_seconds"),
            "orchestrator_elapsed_seconds": result.get("orchestrator_elapsed_seconds"),
            "peak_memory_bytes": outer.get("peak_memory_bytes"),
            "cleanup_confirmed": outer.get("cleanup_confirmed"),
            "output_truncated": outer.get("output_truncated"),
            "stdout_sha256": outer.get("stdout_sha256"),
            "stderr_sha256": outer.get("stderr_sha256"),
        },
        "inner_cleanup": {
            "host_pid_reaped": result.get("federated_trace", {}).get("host_pid_reaped"),
            "worker_cleanup_confirmed": cleanup.get("worker_cleanup_confirmed"),
        },
        "environment_fingerprint": result.get("environment"),
        "runtime_command_ids": sorted(
            descriptor.get("command_id") for descriptor in inventory or []
            if isinstance(descriptor, dict) and isinstance(descriptor.get("command_id"), str)
        ),
        "runtime_services": runtime_services,
        "runtime_plugins": sorted(item for item in runtime_plugins if isinstance(item, str)) if isinstance(runtime_plugins, list) else [],
        "federated_command_tool_count": len(command_inventory.get("listed_command_tools", [])) if isinstance(command_inventory, dict) else None,
        "federated_command_tools_sha256": object_sha256(command_inventory.get("listed_command_tools", [])) if isinstance(command_inventory, dict) else None,
        "gateway_operation_count": result.get("federated_trace", {}).get("gateway_operation_count"),
        "diagnostic_summary": result.get("stderr_summary") or result.get("diagnostics_summary") or result.get("diagnostic_summary"),
    }


def _discovery_terminal_state(discovery: dict[str, Any]) -> tuple[str, str] | None:
    """Return an entry state for a failed discovery, stopping only on lost cleanup."""
    cleanup_confirmed = discovery.get("outer_worker", {}).get("cleanup_confirmed") is True
    if not cleanup_confirmed:
        return "incomplete", "worker_cleanup_unconfirmed"
    if (
        discovery.get("status") == "completed"
        and discovery.get("profile_setup", {}).get("status") == "passed"
    ):
        return None
    return "skipped", "native_discovery_failed"


def _discovery_skip_domain(discovery: dict[str, Any]) -> str:
    """Classify known service/database setup prerequisites separately from Host failures."""
    profile_status = discovery.get("profile_setup", {}).get("status") or discovery.get("profile_setup_status")
    if profile_status != "passed":
        return "profile"
    diagnostic = str(discovery.get("diagnostic_summary") or "").casefold()
    profile_markers = ("migration", "orm upgrade", "database schema", "required host service")
    return "profile" if any(marker in diagnostic for marker in profile_markers) else "native_host"


def _project_observation(result: dict[str, Any], event_ids: list[str]) -> dict[str, Any] | None:
    if result.get("status") != "completed":
        return None
    grouped: dict[str, list[dict[str, Any]]] = {event_id: [] for event_id in event_ids}
    unbound = []
    for operation in result.get("operations", []):
        event_id = operation.get("source_event_id")
        projected = {
            "account_id": operation.get("account_id"),
            "operation": operation.get("operation"),
            "target": operation.get("target"),
            "message": operation.get("params", {}).get("message"),
        }
        if event_id in grouped:
            grouped[event_id].append(projected)
        else:
            unbound.append(projected)
    return normalize_observation({"events": grouped, "unbound_operations": unbound})


def _attribution(native: dict[str, Any], federated: dict[str, Any]) -> dict[str, Any]:
    if native.get("profile_setup", {}).get("status") != "passed" or federated.get("profile_setup", {}).get("status") != "passed":
        return {"category": "profile", "confidence": "evidence-bound", "reason": "one or both exact offline profiles did not install"}
    if federated.get("status") != "completed":
        trace = federated.get("federated_trace", {})
        if trace.get("core_host_route") != "passed":
            return {"category": "Core", "confidence": "candidate", "reason": "Federated route did not complete"}
        return {"category": "Host", "confidence": "candidate", "reason": "Federated Host did not complete the scenario batch"}
    return {
        "category": "unknown",
        "confidence": "unresolved",
        "reason": "both sides completed under matching locked identities; available evidence does not distinguish plugin behavior from Host/Core integration",
    }


def _tool_exposure(candidate: dict[str, Any], descriptors: list[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
    names = run.get("command_inventory", {}).get("listed_command_tools", [])
    host_id = _native_host_id(candidate)
    expected = {f"cmd.{_tool_name_part(host_id)}.{_tool_name_part(item['command_id'])}" for item in descriptors}
    listed = set(names) if isinstance(names, list) else set()
    return {
        "native_descriptor_count": len(descriptors),
        "federated_tool_count": len(listed),
        "matched_count": len(expected & listed),
        "missing_count": len(expected - listed),
        "unmatched_federated_count": len(listed - expected),
        "listed_tools_sha256": object_sha256(sorted(listed)),
    }


def _round_scenario_results(
    scenarios: list[dict[str, Any]],
    native_one: dict[str, Any],
    native_two: dict[str, Any],
    federated: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    event_ids = [item["event_id"] for item in scenarios]
    first = _project_observation(native_one, event_ids)
    second = _project_observation(native_two, event_ids)
    fed = _project_observation(federated, event_ids)
    if not scenarios:
        return [], first, second, fed
    case_results = []
    for scenario in scenarios:
        event_id = scenario["event_id"]
        left = first.get("events", {}).get(event_id) if first else None
        repeated = second.get("events", {}).get(event_id) if second else None
        actual = fed.get("events", {}).get(event_id) if fed else None
        if left is None or repeated is None or actual is None:
            status = "unknown"
            self_status = "unknown"
            reason = "side_failed_or_operation_could_not_be_bound_to_source_event"
        elif not left and not repeated:
            status = "unknown"
            self_status = "unknown"
            reason = "no_native_gateway_operation_observed"
        else:
            self_diff = compare_repeated_observations(left, repeated)
            self_status = self_diff["status"]
            if self_status == "nondeterministic":
                status, reason = "nondeterministic", self_diff.get("reason") or "native_replay_changed"
            elif self_status != "stable":
                status, reason = "unknown", self_diff.get("reason") or "native_replay_not_comparable"
            elif _canonical(left) == _canonical(actual):
                status, reason = "equivalent", None
            else:
                status, reason = "non_equivalent", "normalized_native_and_federated_observations_differ"
        case_results.append({
            "scenario_id": scenario["scenario_id"],
            "command_id": scenario["command_id"],
            "case_kind": scenario["kind"],
            "status": status,
            "native_self_status": self_status,
            "reason_code": reason,
            "native_one_observation_sha256": object_sha256(left) if left is not None else None,
            "native_two_observation_sha256": object_sha256(repeated) if repeated is not None else None,
            "federated_observation_sha256": object_sha256(actual) if actual is not None else None,
        })
    return case_results, first, second, fed


def _nontrivial_commands(
    scenario_meta: list[dict[str, Any]],
    round_observations: list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
) -> dict[str, str]:
    by_command: dict[str, list[dict[str, Any]]] = {}
    for scenario in scenario_meta:
        by_command.setdefault(scenario["command_id"], []).append(scenario)
    result = {}
    for command_id, cases in by_command.items():
        normal = [item for item in cases if item["kind"] in {"pairwise", "raw_text"} and item["arguments_nonempty"]]
        edge = [
            item for item in cases
            if item["kind"] in {"missing_required", "extra_argument"}
            or (item["kind"] == "raw_text" and not item["arguments_nonempty"])
        ]
        if not normal or not edge:
            result[command_id] = "not_assessed"
            continue
        differs = False
        comparable = False
        for native_one, native_two in round_observations:
            if not native_one or not native_two:
                continue
            stable = {
                scenario["event_id"]
                for scenario in cases
                if scenario["event_id"] in native_one.get("events", {})
                and scenario["event_id"] in native_two.get("events", {})
                and _canonical(native_one["events"][scenario["event_id"]])
                == _canonical(native_two["events"][scenario["event_id"]])
            }
            for observation in (native_one, native_two):
                if not observation:
                    continue
                for typical in normal:
                    if typical["event_id"] not in stable:
                        continue
                    for boundary in edge:
                        if boundary["event_id"] not in stable:
                            continue
                        left = observation.get("events", {}).get(typical["event_id"])
                        right = observation.get("events", {}).get(boundary["event_id"])
                        if isinstance(left, list) and left and isinstance(right, list) and right:
                            comparable = True
                        if left is not None and right is not None and _canonical(left) != _canonical(right):
                            differs = True
                            break
                    if differs:
                        break
                if differs:
                    break
            if differs:
                break
        if differs:
            result[command_id] = "nontrivial"
        elif comparable:
            result[command_id] = "trivial_for_generated_normal_vs_edge_inputs"
        else:
            result[command_id] = "not_assessed"
    return result


def _command_summary(
    descriptors: list[dict[str, Any]],
    scenario_meta: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
    round_scenarios: list[list[dict[str, Any]]],
    round_observations: list[tuple[dict[str, Any] | None, dict[str, Any] | None]],
) -> dict[str, Any]:
    outcomes: dict[str, list[str]] = {item["command_id"]: [] for item in descriptors}
    for batch in round_scenarios:
        for row in batch:
            outcomes.setdefault(row["command_id"], []).append(row["status"])
    per_command = []
    counts = {"equivalent": 0, "non_equivalent": 0, "nondeterministic": 0, "unknown": 0}
    for descriptor in descriptors:
        command_id = descriptor["command_id"]
        values = outcomes.get(command_id, [])
        if not values:
            status = "unknown"
        elif "nondeterministic" in values:
            status = "nondeterministic"
        elif "non_equivalent" in values:
            status = "non_equivalent"
        elif all(value == "equivalent" for value in values):
            status = "equivalent"
        else:
            status = "unknown"
        counts[status] += 1
        case_rounds: dict[str, list[str]] = {}
        for batch in round_scenarios:
            for row in batch:
                if row["command_id"] == command_id:
                    case_rounds.setdefault(row["scenario_id"], []).append(row["status"])
        consistency = (
            all(len(rows) == ROUNDS and len(set(rows)) == 1 for rows in case_rounds.values())
            and len(case_rounds) == len({item["scenario_id"] for item in scenario_meta if item["command_id"] == command_id})
        )
        per_command.append({
            "command_id": command_id,
            "descriptor_sha256": object_sha256(descriptor),
            "scenarios": len({item["scenario_id"] for item in scenario_meta if item["command_id"] == command_id}),
            "status": status,
            "consistent_across_three_fresh_reruns": consistency,
        })
    nontrivial = _nontrivial_commands(scenario_meta, round_observations)
    counts["nontrivial"] = sum(value == "nontrivial" for value in nontrivial.values())
    counts["nontrivial_unknown"] = sum(value == "not_assessed" for value in nontrivial.values())
    exercised_ids = {item["command_id"] for item in scenario_meta}
    counts["exercised"] = sum(item["command_id"] in exercised_ids for item in descriptors)
    for row in per_command:
        row["nontrivial"] = nontrivial.get(row["command_id"], "not_assessed")
    return {"counts": counts, "items": per_command}


def _write_progress(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_final(path: Path, report: dict[str, Any]) -> str:
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    digest = _sha256_bytes(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def run_campaign(
    *,
    selection_path: Path,
    admission_path: Path,
    runtime_image: str,
    core_binary: Path,
    bundle_root: Path,
    output_path: Path,
    limit_per_ecosystem: int | None = None,
    rounds: int = ROUNDS,
    workers: int = 3,
) -> dict[str, Any]:
    if rounds != ROUNDS:
        raise ValueError("P7-04 acceptance requires exactly three fresh reruns")
    if workers not in {1, 2, 3}:
        raise ValueError("P7-04 permits one to three concurrent isolated Workers")
    output_path = output_path.expanduser().absolute()
    progress_path = output_path.with_suffix(output_path.suffix + ".progress.json")
    bundle_root = bundle_root.expanduser().absolute()
    if output_path.exists() or progress_path.exists():
        raise FileExistsError(f"refusing to overwrite P7-04 report/progress: {output_path}")
    try:
        bundle_root.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("P7 Worker bundles must be outside the repository")
    if bundle_root.exists():
        raise FileExistsError(f"refusing to overwrite a P7-04 bundle root: {bundle_root}")
    if "@sha256:" not in runtime_image:
        raise ValueError("runtime image must be digest-pinned")
    core_binary = core_binary.expanduser().resolve(strict=True)
    if not core_binary.is_file():
        raise ValueError("Core binary hash could not be computed")

    admission = _read(admission_path)
    capabilities = admission.get("capabilities", {})
    if not (
        admission.get("status") == "admitted"
        and admission.get("platform") == "linux"
        and capabilities.get("claim_allowed") is True
        and capabilities.get("network_disabled") is True
        and capabilities.get("cleanup_evidence_required") is True
    ):
        raise RuntimeError("Linux Worker backend does not have a current admitted report")
    selection = _read(selection_path)
    candidates = select_candidates(selection, limit_per_ecosystem=limit_per_ecosystem)
    bundle_root.mkdir(parents=True)
    campaign_id = f"p7-p704-{int(time.time())}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "p7_p704_generated_differential_pilot",
        "campaign_id": campaign_id,
        "started_at_utc": _now(),
        "status": "running",
        "scope": {
            "ecosystems": {eco: sum(item["ecosystem"] == eco for item in candidates) for eco in ECOSYSTEMS},
            "selection_path": selection_path.relative_to(ROOT).as_posix() if selection_path.is_relative_to(ROOT) else str(selection_path),
            "selection_sha256": _sha256_file(selection_path),
            "source_matrix_path": "outputs/compat/compatibility-matrix-v1.json",
            "source_matrix_sha256": _sha256_file(MATRIX_PATH),
            "load_evidence_required": "linux:verified",
            "rounds": ROUNDS,
            "per_scenario_workers": ["native-1", "native-2", "federated"],
            "generator_version": GENERATOR_VERSION,
            "scenario_limit_per_command": 16,
            "random_seed": FIXED_SEED,
            "timezone": "UTC",
            "network": "disabled",
            "limit_per_ecosystem": limit_per_ecosystem,
        },
        "admission_path": str(admission_path),
        "admission_sha256": _sha256_file(admission_path),
        "runtime_image": runtime_image,
        "runtime_image_reference_sha256": _sha256_bytes(runtime_image.encode("ascii")),
        "core_cli_sha256": _sha256_file(core_binary),
        "koishi_host_lock_sha256": _sha256_file(ROOT / "hosts/koishi/package-lock.json"),
        "koishi_host_dist_sha256": _tree_sha256(ROOT / "hosts/koishi/dist"),
        "worker_limits": {"timeout_seconds": 240, "memory_mb": 4096, "cpu_cores": 2, "process_limit": 256, "max_concurrent_workers": workers},
        "bundle_root": str(bundle_root),
        "bundle_preparation": [],
        "entries": [],
        "counts": {"entries": 0, "worker_runs": 0, "cleanup_unconfirmed": 0, "scenarios": 0},
    }
    _write_progress(progress_path, report)

    # The P4 runtime image is re-probed through an admitted Docker Worker before any plugin bundle runs.
    runtime_probe_root = bundle_root / "_runtime_probe"
    runtime_probe_root.mkdir()
    runtime_probe = phase4_campaign._runtime_probe(
        phase4_campaign.DockerWorker(image_digest=runtime_image, repository_root=ROOT),
        runtime_probe_root,
        run_id=f"{campaign_id}-runtime-probe",
    )
    report["runtime_probe"] = runtime_probe
    if runtime_probe.get("status") != "passed":
        report["status"] = "blocked"
        report["stop_reason"] = "runtime_image_probe_failed"
        report["completed_at_utc"] = _now()
        _write_progress(progress_path, report)
        return report

    for candidate in candidates:
        bundle_info = prepare_bundle(candidate, bundle_root=bundle_root, core_binary=core_binary)
        bundle_info["registry_entry_id"] = candidate["registry_entry_id"]
        bundle_info["ecosystem"] = candidate["ecosystem"]
        report["bundle_preparation"].append(bundle_info)
        _write_progress(progress_path, report)

    bundle_info_by_id = {row["registry_entry_id"]: row for row in report["bundle_preparation"]}
    schema_validator = Draft202012Validator(_read(DESCRIPTOR_SCHEMA_PATH))
    stop_reason = None
    for index, candidate in enumerate(candidates, start=1):
        entry_id = candidate["registry_entry_id"]
        bundle_info = bundle_info_by_id[entry_id]
        bundle = Path(bundle_info["path"])
        validate_bundle(bundle, candidate, bundle_info["manifest_sha256"])
        runtime_artifacts = bundle_info["runtime_artifacts"]
        inventory_request = _build_request(
            candidate, {"events": []}, runtime_artifacts, mode="native", seed=FIXED_SEED,
            discover_commands=False, case_id=f"{entry_id}-inventory",
        )
        discovery = _run_side(
            inventory_request, candidate, bundle, runtime_image,
            campaign_id=campaign_id, round_no=1, side="native-discovery",
        )
        discovery_summary = _worker_summary(discovery)
        if discovery.get("outer_worker", {}).get("cleanup_confirmed") is not True:
            report["counts"]["cleanup_unconfirmed"] += 1
            stop_reason = f"worker_cleanup_unconfirmed:{entry_id}:native_discovery"
        discovery_ok = (
            discovery.get("status") == "completed"
            and discovery.get("profile_setup", {}).get("status") == "passed"
            and discovery.get("outer_worker", {}).get("cleanup_confirmed") is True
        )
        raw_descriptors = discovery.get("stock_native", {}).get("runtime_commands", [])
        descriptors = [item for item in raw_descriptors if isinstance(item, dict) and not list(schema_validator.iter_errors(item))]
        events, generation = build_scenario_batch(candidate, descriptors, seed=FIXED_SEED)
        stimulus = {"events": events}
        scenario_meta = generation["scenarios"]
        entry_result: dict[str, Any] = {
            "registry_entry_id": entry_id,
            "ecosystem": candidate["ecosystem"],
            "package": candidate["package"],
            "version": candidate["version"],
            "artifact_sha256": candidate["artifact_sha256"],
            "profile_lock_sha256": candidate["profile_lock_sha256"],
            "load_evidence": candidate["load_evidence"],
            "static_inventory_sha256": candidate["static_inventory_sha256"],
            "static_commands_detected": candidate["static_command_count"],
            "native_discovery": discovery_summary,
            "scenario_generation": {
                key: value for key, value in generation.items() if key != "scenarios"
            },
            "scenarios": [
                {key: item[key] for key in ("scenario_id", "command_id", "kind", "descriptor_sha256", "input_sha256", "arguments_bytes", "arguments_nonempty", "pairwise")}
                for item in scenario_meta
            ],
            "rounds": [],
        }
        report["counts"]["worker_runs"] += 1
        report["counts"]["scenarios"] += len(scenario_meta)

        if not discovery_ok:
            entry_result["commands"] = {
                "discovered": len(raw_descriptors) if isinstance(raw_descriptors, list) else 0,
                "valid_descriptors": len(descriptors),
                "invalid_descriptors": generation["commands_invalid"],
                "exercised": 0,
                "nontrivial": 0,
                "nontrivial_unknown": len(descriptors),
                "equivalent": 0,
                "non_equivalent": 0,
                "nondeterministic": 0,
                "unknown": 0,
                "items": [],
            }
            terminal_state = _discovery_terminal_state(discovery)
            if terminal_state is None:
                raise AssertionError("discovery failure has no terminal state")
            entry_result["status"], entry_result["conclusion_state"] = terminal_state
            entry_result["conclusions_consistent_across_three_fresh_reruns"] = None
            entry_result["skip_domain"] = _discovery_skip_domain(discovery_summary)
            entry_result["skip_reason"] = discovery_summary.get("diagnostic_summary") or discovery_summary.get("reason_code") or "native_discovery_failed"
            entry_result["contracts"] = _pilot_entry_contracts(entry_result)
            report["entries"].append(entry_result)
            report["counts"]["entries"] += 1
            if entry_result["status"] == "incomplete" and stop_reason is None:
                stop_reason = f"worker_cleanup_unconfirmed:{entry_id}:native_discovery"
            action = "skipped; continuing" if entry_result["status"] == "skipped" else "incomplete; stopping"
            print(f"[{index}/{len(candidates)}] {candidate['ecosystem']} {entry_id}: discovery={entry_result['status']} ({action})", flush=True)
            _write_progress(progress_path, report)
            if entry_result["status"] == "incomplete":
                break
            continue

        if not descriptors:
            entry_result["commands"] = {
                "discovered": len(raw_descriptors) if isinstance(raw_descriptors, list) else 0,
                "valid_descriptors": 0,
                "invalid_descriptors": generation["commands_invalid"],
                "exercised": 0,
                "nontrivial": 0,
                "nontrivial_unknown": 0,
                "equivalent": 0,
                "non_equivalent": 0,
                "nondeterministic": 0,
                "unknown": 0,
                "items": [],
            }
            entry_result["conclusion_state"] = (
                "no_runtime_commands_discovered"
                if not raw_descriptors
                else "no_valid_runtime_command_descriptors"
            )
            entry_result["conclusions_consistent_across_three_fresh_reruns"] = None
            entry_result["status"] = "completed"
            entry_result["contracts"] = _pilot_entry_contracts(entry_result)
            report["entries"].append(entry_result)
            report["counts"]["entries"] += 1
            print(f"[{index}/{len(candidates)}] {candidate['ecosystem']} {entry_id}: no runtime commands; behavior comparison not applicable", flush=True)
            _write_progress(progress_path, report)
            continue

        raw_observations: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
        round_case_rows: list[list[dict[str, Any]]] = []
        all_clean = True
        for round_no in range(1, ROUNDS + 1):
            native_one_request = _build_request(candidate, stimulus, runtime_artifacts, mode="native", seed=FIXED_SEED, discover_commands=False, case_id=f"{entry_id}-r{round_no}-native-1")
            native_two_request = _build_request(candidate, stimulus, runtime_artifacts, mode="native", seed=FIXED_SEED, discover_commands=False, case_id=f"{entry_id}-r{round_no}-native-2")
            federated_request = _build_request(candidate, stimulus, runtime_artifacts, mode="federated", seed=FIXED_SEED, discover_commands=True, case_id=f"{entry_id}-r{round_no}-federated")
            jobs = [
                ("native-1", native_one_request),
                ("native-2", native_two_request),
                ("federated", federated_request),
            ]
            with ThreadPoolExecutor(max_workers=min(workers, 3)) as pool:
                futures = {
                    name: pool.submit(
                        _run_side, request, candidate, bundle, runtime_image,
                        campaign_id=campaign_id, round_no=round_no, side=name,
                    )
                    for name, request in jobs
                }
                run_results = {name: future.result() for name, future in futures.items()}
            report["counts"]["worker_runs"] += 3
            summaries = {name: _worker_summary(result) for name, result in run_results.items()}
            clean = all(result.get("outer_worker", {}).get("cleanup_confirmed") is True for result in run_results.values())
            federated_result = run_results["federated"]
            host_reaped = federated_result.get("federated_trace", {}).get("host_pid_reaped") is True
            host_cleanup_unconfirmed = federated_result.get("status") == "completed" and not host_reaped
            if host_cleanup_unconfirmed:
                clean = False
                stop_reason = f"federated_host_cleanup_unconfirmed:{entry_id}:round-{round_no}"
            elif federated_result.get("status") != "completed" and federated_result.get("outer_worker", {}).get("cleanup_confirmed") is not True:
                clean = False
            if not clean:
                all_clean = False
                report["counts"]["cleanup_unconfirmed"] += sum(
                    result.get("outer_worker", {}).get("cleanup_confirmed") is not True
                    for result in run_results.values()
                ) + int(host_cleanup_unconfirmed)
                if stop_reason is None:
                    stop_reason = f"worker_cleanup_unconfirmed:{entry_id}:round-{round_no}"
            case_rows, first_obs, second_obs, fed_obs = _round_scenario_results(
                scenario_meta,
                run_results["native-1"],
                run_results["native-2"],
                run_results["federated"],
            )
            raw_observations.append((first_obs, second_obs))
            round_case_rows.append(case_rows)
            exposure = _tool_exposure(candidate, descriptors, run_results["federated"])
            mismatches = []
            if any(row["status"] in {"non_equivalent", "unknown", "nondeterministic"} for row in case_rows):
                for row in case_rows:
                    if row["status"] in {"non_equivalent", "unknown", "nondeterministic"}:
                        mismatches.append({
                            "scenario_id": row["scenario_id"],
                            "command_id": row["command_id"],
                            "status": row["status"],
                            "attribution": {
                                **_attribution(run_results["native-1"], run_results["federated"]),
                                "scenario_reason": row.get("reason_code"),
                            },
                        })
            entry_result["rounds"].append({
                "round": round_no,
                "fresh_workers": summaries,
                "command_tool_exposure": exposure,
                "scenario_results": case_rows,
                "mismatches": mismatches,
                "cleanup_confirmed": clean,
                "federated_host_reaped": host_reaped,
            })
            if not clean:
                break

        command_summary = _command_summary(descriptors, scenario_meta, entry_result["rounds"], round_case_rows, raw_observations)
        entry_result["commands"] = {
            "discovered": len(raw_descriptors) if isinstance(raw_descriptors, list) else 0,
            "valid_descriptors": len(descriptors),
            "invalid_descriptors": generation["commands_invalid"],
            "exercised": command_summary["counts"]["exercised"],
            "nontrivial": command_summary["counts"]["nontrivial"],
            "nontrivial_unknown": command_summary["counts"]["nontrivial_unknown"],
            "equivalent": command_summary["counts"]["equivalent"],
            "non_equivalent": command_summary["counts"]["non_equivalent"],
            "nondeterministic": command_summary["counts"]["nondeterministic"],
            "unknown": command_summary["counts"]["unknown"],
            "items": command_summary["items"],
        }
        entry_result["conclusions_consistent_across_three_fresh_reruns"] = (
            all(round_case_rows[0][i]["status"] == round_case_rows[1][i]["status"] == round_case_rows[2][i]["status"]
                for i in range(len(round_case_rows[0])))
            if len(round_case_rows) == ROUNDS and all(len(rows) == len(round_case_rows[0]) for rows in round_case_rows)
            else False
        )
        entry_result["status"] = "completed" if len(entry_result["rounds"]) == ROUNDS and all_clean else "incomplete"
        entry_result["contracts"] = _pilot_entry_contracts(entry_result)
        report["entries"].append(entry_result)
        report["counts"]["entries"] += 1
        print(f"[{index}/{len(candidates)}] {candidate['ecosystem']} {entry_id}: commands={entry_result['commands']['discovered']} scenarios={len(scenario_meta)} rounds={len(entry_result['rounds'])} status={entry_result['status']}", flush=True)
        _write_progress(progress_path, report)
        if stop_reason:
            break

    complete_entries = len(report["entries"]) == len(candidates) and all(
        entry.get("status") in {"completed", "skipped"} for entry in report["entries"]
    )
    assessed_entries = [entry for entry in report["entries"] if entry.get("scenario_generation", {}).get("scenario_count", 0) > 0]
    all_consistent = complete_entries and all(entry.get("conclusions_consistent_across_three_fresh_reruns") is True for entry in assessed_entries)
    contract_totals = {
        contract_id: {status: 0 for status in ("passed", "failed", "unknown")}
        for contract_id in CONTRACT_IDS
    }
    for entry in report["entries"]:
        for contract_id, check in (entry.get("contracts") or {}).items():
            contract_totals[contract_id][check["status"]] += 1
    report["summary"] = {
        "selected_entries": len(candidates),
        "completed_entries": sum(entry.get("status") == "completed" for entry in report["entries"]),
        "skipped_discovery_entries": sum(entry.get("conclusion_state") == "native_discovery_failed" for entry in report["entries"]),
        "entries_without_runtime_commands": sum(entry.get("conclusion_state") == "no_runtime_commands_discovered" for entry in report["entries"]),
        "entries_without_valid_runtime_descriptors": sum(entry.get("conclusion_state") == "no_valid_runtime_command_descriptors" for entry in report["entries"]),
        "entries_with_behavior_scenarios": len(assessed_entries),
        "commands_discovered": sum(entry.get("commands", {}).get("discovered", 0) for entry in report["entries"]),
        "commands_exercised": sum(entry.get("commands", {}).get("exercised", 0) for entry in report["entries"]),
        "commands_nontrivial": sum(entry.get("commands", {}).get("nontrivial", 0) for entry in report["entries"]),
        "commands_equivalent": sum(entry.get("commands", {}).get("equivalent", 0) for entry in report["entries"]),
        "commands_non_equivalent": sum(entry.get("commands", {}).get("non_equivalent", 0) for entry in report["entries"]),
        "commands_nondeterministic": sum(entry.get("commands", {}).get("nondeterministic", 0) for entry in report["entries"]),
        "commands_unknown": sum(entry.get("commands", {}).get("unknown", 0) for entry in report["entries"]),
        "conclusions_consistent_across_three_fresh_reruns": all_consistent,
        "mismatch_count": sum(len(round_row.get("mismatches", [])) for entry in report["entries"] for round_row in entry["rounds"]),
        "contract_checks": {
            "entries_with_all_c1_c8_conclusions": sum(
                set((entry.get("contracts") or {}).keys()) == set(CONTRACT_IDS)
                for entry in report["entries"]
            ),
            "by_contract": contract_totals,
        },
    }
    report["stop_reason"] = stop_reason
    report["status"] = "completed" if complete_entries and report["counts"]["cleanup_unconfirmed"] == 0 else "incomplete"
    report["completed_at_utc"] = _now()
    _write_final(output_path, report)
    progress_path.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--admission", type=Path, default=ADMISSION_PATH)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--core-binary", required=True, type=Path)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit-per-ecosystem", type=int)
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    parser.add_argument("--workers", type=int, default=3)
    options = parser.parse_args(argv)
    try:
        report = run_campaign(
            selection_path=options.selection.resolve(strict=True),
            admission_path=options.admission.resolve(strict=True),
            runtime_image=options.runtime_image,
            core_binary=options.core_binary,
            bundle_root=options.bundle_root,
            output_path=options.output,
            limit_per_ecosystem=options.limit_per_ecosystem,
            rounds=options.rounds,
            workers=options.workers,
        )
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 2
    digest_path = options.output.with_suffix(options.output.suffix + ".sha256")
    digest = digest_path.read_text(encoding="ascii").split()[0] if digest_path.is_file() else None
    print(json.dumps({"status": report.get("status"), "campaign_id": report.get("campaign_id"), "output": options.output.as_posix(), "report_sha256": digest, "counts": report.get("summary")}, ensure_ascii=False))
    return 0 if report.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
