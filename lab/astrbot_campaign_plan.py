"""Build a frozen, incremental AstrBot command campaign plan from a closed census.

Planning is read-only. It never starts a Worker, and it refuses an in-progress or
non-conserving census so load-census checkpoint rows cannot leak into behavior work.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import os
import re
import time
from pathlib import Path
from typing import Any

from lab.astrbot_census import LOCK_FILE, _load_full_artifact_lock, select_full_census
from lab.queue import StageQueue
from lab.scenario_generator import GENERATOR_NAME, GENERATOR_VERSION, generate_scenarios


CAMPAIGN_SCHEMA_VERSION = 1
DEFAULT_HOST_ID = "astrbot-p709"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
ARGUMENT_KEYS = {"name", "required", "type"}
CAMPAIGN_SOURCE_PATHS = (
    "lab/astrbot_campaign_plan.py",
    "lab/astrbot_campaign_execute.py",
    "lab/astrbot_census.py",
    "lab/astrbot_census_side.py",
    "lab/astrbot_native_tool_campaign.py",
    "lab/astrbot_native_tool_side.py",
    "lab/astrbot_tool_campaign.py",
    "lab/astrbot_tool_side.py",
    "lab/automation/p7_contracts.py",
    "lab/automation/reconcile.py",
    "lab/mcp_http.py",
    "lab/queue.py",
    "lab/scenario_generator.py",
    "lab/workers/base.py",
    "lab/workers/linux.py",
    "hosts/astrbot/ligamina_astrbot/__init__.py",
    "hosts/astrbot/ligamina_astrbot/bridge.py",
    "hosts/astrbot/ligamina_astrbot/commands.py",
    "hosts/astrbot/ligamina_astrbot/contracts.py",
    "hosts/astrbot/ligamina_astrbot/lifecycle.py",
    "hosts/astrbot/ligamina_astrbot/runtime.py",
    "hosts/astrbot/ligamina_astrbot/worker.py",
    "contracts/v0.1/event-envelope.schema.json",
    "contracts/v0.2/command-descriptor.schema.json",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _campaign_source_identity() -> dict[str, Any]:
    """Bind the exact controller, Worker, Host, generator, and contract sources."""
    root = Path(__file__).resolve().parents[1]
    sources = {}
    for relative in CAMPAIGN_SOURCE_PATHS:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"campaign source required for run identity is missing: {relative}")
        sources[relative] = _sha256(path.read_bytes())
    return {"sha256": _sha256(_canonical(sources)), "files": sources}


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _admission_environment_identity(admission: dict[str, Any], worker_image: str) -> dict[str, Any]:
    """Keep stable execution facts in the run key, excluding report timestamps and free resources."""
    facts = admission.get("facts", {}) if isinstance(admission.get("facts"), dict) else {}
    capabilities = admission.get("capabilities", {}) if isinstance(admission.get("capabilities"), dict) else {}
    host = facts.get("host", {}) if isinstance(facts.get("host"), dict) else {}
    docker = facts.get("docker", {}) if isinstance(facts.get("docker"), dict) else {}
    image = facts.get("image", {}) if isinstance(facts.get("image"), dict) else {}
    resources = facts.get("resources", {}) if isinstance(facts.get("resources"), dict) else {}
    return {
        "status": admission.get("status"),
        "platform": admission.get("platform"),
        "worker_image": worker_image,
        "capabilities": {key: capabilities.get(key) for key in (
            "backend", "claim_allowed", "network_disabled", "cleanup_evidence_required",
        )},
        "facts": {
            "backend": facts.get("backend"),
            "host": {key: host.get(key) for key in ("system", "release", "machine", "python")},
            "docker": {key: docker.get(key) for key in (
                "server_version", "operating_system", "architecture", "cgroup_version", "cgroup_driver",
                "security_options", "mem_total_bytes", "n_cpu",
            )},
            "image": {key: image.get(key) for key in ("locked_reference", "lock_sha256", "image_id")},
            # Bind the admission thresholds, not transient free-memory and free-disk readings.
            "resource_requirements": {key: resources.get(key) for key in (
                "memory_required_bytes", "scratch_minimum_bytes",
            )},
        },
    }


def _normalized_descriptor(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    command_id = value.get("command_id")
    if not isinstance(command_id, str) or not command_id or len(command_id) > 96 or any(char.isspace() for char in command_id):
        raise ValueError(f"{label} has an invalid AstrBot command_id")
    raw_arguments = value.get("arguments", [])
    if not isinstance(raw_arguments, list) or len(raw_arguments) > 64:
        raise ValueError(f"{label} arguments must be a bounded array")
    arguments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, argument in enumerate(raw_arguments):
        if not isinstance(argument, dict):
            raise ValueError(f"{label} argument {index} must be an object")
        name, required, kind = argument.get("name"), argument.get("required"), argument.get("type")
        if (not isinstance(name, str) or not name or len(name) > 128 or name in seen
                or not isinstance(required, bool) or not isinstance(kind, str) or len(kind) > 64):
            raise ValueError(f"{label} argument {index} has an invalid name, required flag, or type")
        seen.add(name)
        # Reports may contain legacy or unexpected keys. Keep only the census contract;
        # command descriptions and values must never enter generated plans or Workers.
        arguments.append({"name": name, "required": required, "type": kind})
    return {
        "framework": "astrbot",
        "command_id": command_id,
        "invocation": command_id,
        "arguments": arguments,
    }


def differential_jobs_for_inventory(
    *,
    snapshot_id: str,
    artifacts_lock_sha256: str,
    selection_sha256: str,
    worker_image: str,
    native_runtime: dict[str, str],
    admission_environment_sha256: str,
    registry_entry_id: str,
    artifact_sha256: str,
    descriptors: list[dict[str, Any]],
    max_scenarios: int = 16,
) -> tuple[str, list[dict[str, Any]]]:
    """Derive the same command jobs for observed inventory and later backfills."""
    normalized = [_normalized_descriptor(item, f"{registry_entry_id} descriptor {index}")
                  for index, item in enumerate(descriptors)]
    command_ids = [item["command_id"] for item in normalized]
    if len(command_ids) != len(set(command_ids)):
        raise ValueError(f"{registry_entry_id} inventory contains duplicate command descriptors")
    if not isinstance(max_scenarios, int) or isinstance(max_scenarios, bool) or not 1 <= max_scenarios <= 16:
        raise ValueError("max_scenarios must be between 1 and 16")
    inventory_sha = _sha256(_canonical({
        "registry_entry_id": registry_entry_id,
        "artifact_sha256": artifact_sha256,
        "command_descriptors": normalized,
    }))
    jobs = []
    for descriptor in normalized:
        descriptor_sha = _sha256(_canonical(descriptor))
        seed = int(_sha256(f"{inventory_sha}:{descriptor_sha}".encode("utf-8"))[:13], 16)
        batch = generate_scenarios(
            inventory_sha256=inventory_sha,
            descriptor=descriptor,
            host_id=DEFAULT_HOST_ID,
            seed=seed,
            max_scenarios=max_scenarios,
        )
        batch_sha = _sha256(_canonical(batch.to_dict()))
        key = _sha256(_canonical({
            "campaign_schema_version": CAMPAIGN_SCHEMA_VERSION,
            "stage": "differential",
            "snapshot_id": snapshot_id,
            "artifacts_lock_sha256": artifacts_lock_sha256,
            "selection_sha256": selection_sha256,
            "worker_image": worker_image,
            "native_runtime": native_runtime,
            "admission_environment_sha256": admission_environment_sha256,
            "registry_entry_id": registry_entry_id,
            "artifact_sha256": artifact_sha256,
            "command_id": descriptor["command_id"],
            "descriptor_sha256": descriptor_sha,
            "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION, "seed": seed},
            "scenario_batch_sha256": batch_sha,
        }))
        jobs.append({
            "unit_id": f"astrbot-diff-{key}",
            "registry_entry_id": registry_entry_id,
            "ecosystem": "astrbot",
            "platform": "linux",
            "profile_id": "astrbot-4.29.1-native-federated-tool",
            "stage": "differential",
            "case_id": descriptor["command_id"],
            "max_attempts": 2,
            "payload": {
                "artifact_sha256": artifact_sha256,
                "inventory_sha256": inventory_sha,
                "descriptor_sha256": descriptor_sha,
                "scenario_batch_sha256": batch_sha,
                "scenario_count": len(batch.scenarios),
                "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION, "seed": seed},
            },
        })
    return inventory_sha, jobs


def plan_astrbot_command_campaign(
    lock: dict[str, Any],
    census: dict[str, Any],
    *,
    artifact_lock_sha256: str,
    census_report_sha256: str,
    worker_image: str,
    admission: dict[str, Any],
    admission_sha256: str,
    native_runtime: dict[str, str],
    max_scenarios: int = 16,
) -> dict[str, Any]:
    """Validate a completed full census and derive deterministic inventory/diff jobs.

    The report hash is provenance only. The incremental run key is derived from the
    frozen lock, closed census dispositions and generated command inputs, so identical
    frozen work can be resumed even if an outer JSON checkpoint is resealed.
    """
    lock_sha = _digest(artifact_lock_sha256, "artifact_lock_sha256")
    report_sha = _digest(census_report_sha256, "census_report_sha256")
    if not isinstance(census, dict) or census.get("kind") != "astrbot_linux_install_load_census" or census.get("status") != "completed":
        raise ValueError("AstrBot command campaign requires a completed full census")
    admission_sha = _digest(admission_sha256, "admission_sha256")
    if not isinstance(worker_image, str) or not IMAGE_RE.fullmatch(worker_image):
        raise ValueError("worker_image must be pinned by a lowercase sha256 digest")
    admission_report = admission if isinstance(admission, dict) else {}
    capabilities = admission_report.get("capabilities", {})
    admitted_image = admission_report.get("facts", {}).get("image", {}).get("locked_reference")
    collected_at = admission_report.get("collected_at_epoch")
    fresh = isinstance(collected_at, int) and not isinstance(collected_at, bool) and -60 <= time.time() - collected_at <= 1800
    if not (admission_report.get("status") == "admitted" and admission_report.get("platform") == "linux"
            and capabilities.get("network_disabled") is True
            and capabilities.get("cleanup_evidence_required") is True
            and admitted_image == worker_image and fresh):
        raise ValueError("campaign requires a fresh admitted Linux report for the exact Worker image")
    admission_environment_sha = _sha256(_canonical(_admission_environment_identity(admission_report, worker_image)))
    runtime_fields = ("archive_sha256", "manifest_sha256", "site_packages_sha256", "requirements_lock_sha256")
    if not isinstance(native_runtime, dict):
        raise ValueError("native_runtime must be a hash-locked runtime bundle")
    runtime_identity = {field: _digest(native_runtime.get(field), f"native_runtime.{field}") for field in runtime_fields}
    if runtime_identity["requirements_lock_sha256"] != _sha256(LOCK_FILE.read_bytes()):
        raise ValueError("native AstrBot runtime lock hash differs from the current requirements lock")
    if not isinstance(max_scenarios, int) or isinstance(max_scenarios, bool) or not 1 <= max_scenarios <= 16:
        raise ValueError("max_scenarios must be between 1 and 16")
    selected = select_full_census(lock)
    frozen = {entry["registry_entry_id"]: entry for _, entry in selected}
    snapshot_id = lock.get("snapshot_id")
    manifest = census.get("manifest")
    denominator = census.get("denominator")
    if not isinstance(snapshot_id, str) or census.get("snapshot_id") != snapshot_id:
        raise ValueError("census snapshot identity differs from the artifact lock")
    if not isinstance(manifest, dict) or manifest.get("snapshot_id") != snapshot_id:
        raise ValueError("census manifest snapshot identity is invalid")
    if manifest.get("artifacts_lock_sha256") != lock_sha:
        raise ValueError("census artifact lock hash differs from the supplied frozen lock")
    selected_ids = [entry["registry_entry_id"] for _, entry in selected]
    selection_sha = _sha256(_canonical(selected_ids))
    if manifest.get("selection_sha256") != selection_sha or manifest.get("selected_entries") != len(selected):
        raise ValueError("census selection does not match every frozen artifact identity")
    if not isinstance(denominator, dict) or (
        denominator.get("snapshot_entries") != len(lock.get("entries", []))
        or denominator.get("frozen_entries") != len(selected)
        or denominator.get("selected_entries") != len(selected)
        or denominator.get("terminal_entries") != len(selected)
        or denominator.get("closed_exactly_once") is not True
    ):
        raise ValueError("census denominator is not closed exactly once over the frozen selection")

    rows = census.get("results")
    if not isinstance(rows, list) or len(rows) != len(selected):
        raise ValueError("census results must contain each frozen identity exactly once")
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry_id = row.get("registry_entry_id") if isinstance(row, dict) else None
        if not isinstance(entry_id, str) or entry_id not in frozen or entry_id in rows_by_id:
            raise ValueError("census results must contain each frozen identity exactly once")
        lock_entry = frozen[entry_id]
        expected_artifact_sha = _digest(lock_entry.get("artifact", {}).get("computed_digests", {}).get("sha256"),
                                        f"{entry_id} artifact hash in frozen lock")
        if row.get("artifact_sha256") != expected_artifact_sha:
            raise ValueError(f"{entry_id} census artifact hash differs from the frozen artifact hash")
        if row.get("status") not in {"passed", "failed", "skipped", "unknown"}:
            raise ValueError(f"{entry_id} census disposition is not terminal")
        rows_by_id[entry_id] = row
    if set(rows_by_id) != set(frozen):
        raise ValueError("census results must contain each frozen identity exactly once")

    dispositions: dict[str, dict[str, Any]] = {}
    unresolved_inventory: list[dict[str, str]] = []
    jobs: list[dict[str, Any]] = []
    totals = {"load_passed": 0, "load_failed": 0, "load_skipped": 0, "load_unknown": 0,
              "inventory_backfill_required": 0, "inventory_backfill_jobs": 0,
              "inventory_truncated": 0, "observed_commands": 0,
              "differential_jobs": 0, "contract_status": "not_evaluated"}

    for _, lock_entry in selected:
        entry_id = lock_entry["registry_entry_id"]
        row = rows_by_id[entry_id]
        status = row["status"]
        status_key = {"passed": "load_passed", "failed": "load_failed", "skipped": "load_skipped", "unknown": "load_unknown"}[status]
        totals[status_key] += 1
        reason_code = row.get("reason_code") if isinstance(row.get("reason_code"), str) else None
        disposition: dict[str, Any] = {"status": status, "reason_code": reason_code}
        dispositions[entry_id] = disposition
        if status != "passed":
            continue

        result = row.get("result")
        side = result.get("side") if isinstance(result, dict) else None
        if not isinstance(side, dict) or side.get("status") != "passed":
            raise ValueError(f"{entry_id} passed census row lacks a passed Worker-side result")
        load = side.get("load")
        if not isinstance(load, dict) or "command_descriptors" not in load:
            totals["inventory_backfill_required"] += 1
            unresolved_inventory.append({"registry_entry_id": entry_id, "artifact_sha256": row["artifact_sha256"],
                                         "reason_code": "runtime_descriptor_field_absent"})
            disposition["inventory_status"] = "backfill_required"
            backfill_key = _sha256(_canonical({
                "campaign_schema_version": CAMPAIGN_SCHEMA_VERSION,
                "stage": "inventory_backfill",
                "snapshot_id": snapshot_id,
                "artifacts_lock_sha256": lock_sha,
                "selection_sha256": selection_sha,
                "worker_image": worker_image,
                "native_runtime": runtime_identity,
                "admission_environment_sha256": admission_environment_sha,
                "registry_entry_id": entry_id,
                "artifact_sha256": row["artifact_sha256"],
            }))
            jobs.append({
                "unit_id": f"astrbot-inventory-{backfill_key}",
                "registry_entry_id": entry_id,
                "ecosystem": "astrbot",
                "platform": "linux",
                "profile_id": "astrbot-4.29.1-runtime-inventory",
                "stage": "inventory",
                "case_id": "runtime-command-inventory-backfill",
                "max_attempts": 2,
                "payload": {"artifact_sha256": row["artifact_sha256"], "inventory_backfill": True},
            })
            totals["inventory_backfill_jobs"] += 1
            continue
        raw_descriptors = load.get("command_descriptors")
        if not isinstance(raw_descriptors, list):
            raise ValueError(f"{entry_id} command_descriptors must be an array")
        descriptors = [_normalized_descriptor(item, f"{entry_id} command descriptor {index}")
                       for index, item in enumerate(raw_descriptors)]
        command_ids = [item["command_id"] for item in descriptors]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError(f"{entry_id} census contains duplicate command descriptors")
        command_count = load.get("commands")
        truncated = load.get("commands_truncated") is True
        if not isinstance(command_count, int) or isinstance(command_count, bool) or command_count < len(descriptors):
            raise ValueError(f"{entry_id} census command count is inconsistent with its descriptors")
        if command_count > len(descriptors) and not truncated:
            raise ValueError(f"{entry_id} census omitted command descriptors without a truncation marker")
        if truncated:
            totals["inventory_truncated"] += 1
            unresolved_inventory.append({"registry_entry_id": entry_id, "artifact_sha256": row["artifact_sha256"],
                                         "reason_code": "runtime_inventory_truncated"})
            disposition["inventory_status"] = "truncated"
        else:
            disposition["inventory_status"] = "observed"
        disposition["observed_command_count"] = len(descriptors)
        totals["observed_commands"] += len(descriptors)
        inventory_sha, differential_jobs = differential_jobs_for_inventory(
            snapshot_id=snapshot_id,
            artifacts_lock_sha256=lock_sha,
            selection_sha256=selection_sha,
            worker_image=worker_image,
            native_runtime=runtime_identity,
            admission_environment_sha256=admission_environment_sha,
            registry_entry_id=entry_id,
            artifact_sha256=row["artifact_sha256"],
            descriptors=descriptors,
            max_scenarios=max_scenarios,
        )
        jobs.extend(differential_jobs)
    jobs.sort(key=lambda item: (item["stage"], item["registry_entry_id"], item["unit_id"]))
    totals["differential_jobs"] = sum(job["stage"] == "differential" for job in jobs)
    campaign_sources = _campaign_source_identity()

    key_material = {
        "campaign_schema_version": CAMPAIGN_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "artifacts_lock_sha256": lock_sha,
        "selection_sha256": selection_sha,
        "worker_image": worker_image,
        "native_runtime": runtime_identity,
        "admission_environment_sha256": admission_environment_sha,
        "max_scenarios": max_scenarios,
        "campaign_sources_sha256": campaign_sources["sha256"],
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "dispositions": dispositions,
        "job_keys": [job["unit_id"] for job in jobs],
        "inventory_backfill": unresolved_inventory,
    }
    manifest_sha = _sha256(_canonical(key_material))
    return {
        "schema_version": 1,
        "kind": "astrbot_incremental_command_campaign_plan",
        "status": "planned",
        "run_id": f"p7-09-astra-{manifest_sha[:20]}",
        "manifest_sha256": manifest_sha,
        "inputs": {
            "snapshot_id": snapshot_id,
            "artifacts_lock_sha256": lock_sha,
            "selection_sha256": selection_sha,
            "census_run_id": census.get("run_id"),
            "census_report_sha256": report_sha,
            "worker_image": worker_image,
            "native_runtime": runtime_identity,
            "admission_sha256": admission_sha,
            "admission_environment_sha256": admission_environment_sha,
            "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
            "max_scenarios": max_scenarios,
            "campaign_sources_sha256": campaign_sources["sha256"],
            "campaign_sources": campaign_sources["files"],
        },
        "denominator": {
            "snapshot_entries": len(lock.get("entries", [])),
            "frozen_entries": len(selected),
            "selected_entries": len(selected),
        },
        "counts": totals,
        "dispositions": dispositions,
        "unresolved_inventory": unresolved_inventory,
        "jobs": jobs,
        "scope_note": "Plan only: no Worker is dispatched. Load failures, skips, unknowns, and missing or truncated inventories remain separate; differential observations do not establish plugin correctness or support.",
    }


def register_campaign_plan(queue_path: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Create or idempotently enqueue a fenced campaign plan without dispatching it."""
    if plan.get("kind") != "astrbot_incremental_command_campaign_plan" or plan.get("status") != "planned":
        raise ValueError("only a validated AstrBot command campaign plan can be registered")
    inputs = plan.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("snapshot_id"), str):
        raise ValueError("campaign plan inputs are incomplete")
    manifest_sha = _digest(plan.get("manifest_sha256"), "campaign manifest_sha256")
    if not isinstance(plan.get("jobs"), list):
        raise ValueError("campaign plan jobs must be an array")
    manifest = {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "kind": plan["kind"],
        "snapshot_id": inputs["snapshot_id"],
        "artifacts_lock_sha256": inputs.get("artifacts_lock_sha256"),
        "selection_sha256": inputs.get("selection_sha256"),
        "worker_image": inputs.get("worker_image"),
        "native_runtime": inputs.get("native_runtime"),
        "admission_environment_sha256": inputs.get("admission_environment_sha256"),
        "max_scenarios": inputs.get("max_scenarios"),
        "campaign_sources_sha256": inputs.get("campaign_sources_sha256"),
        "campaign_sources": inputs.get("campaign_sources"),
        "denominator": plan.get("denominator"),
        "plan_counts": plan.get("counts"),
        "dispositions": plan.get("dispositions"),
        "job_keys": [job.get("unit_id") for job in plan["jobs"]],
        "unresolved_inventory": plan.get("unresolved_inventory"),
    }
    run_id = plan.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("campaign plan run_id is missing")
    queue_target = Path(queue_path).expanduser().absolute()
    repository_root = Path(__file__).resolve().parents[1]
    resolved_queue = queue_target.resolve()
    if resolved_queue == repository_root or repository_root in resolved_queue.parents:
        raise ValueError("AstrBot campaign SQLite queue must be outside the repository")
    queue = StageQueue(queue_target)
    queue.create_run(run_id, inputs["snapshot_id"], manifest_sha, manifest, fencing_required=True)
    enqueue = queue.enqueue_jobs(run_id, plan["jobs"])
    return {"run_id": run_id, "manifest_sha256": manifest_sha, **enqueue, "counts": queue.counts(run_id)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--census-report", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True, help="fresh Linux Worker admission for --worker-image")
    parser.add_argument("--worker-image", required=True, help="digest-pinned AstrBot tool Worker image")
    parser.add_argument("--native-runtime-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new plan report path; existing files are never overwritten")
    parser.add_argument("--queue-db", type=Path, help="optional external SQLite queue; registers jobs but never dispatches Workers")
    parser.add_argument("--max-scenarios", type=int, default=16)
    options = parser.parse_args(argv)

    from lab.astrbot_native_tool_campaign import _runtime_archive_manifest

    lock, lock_path = _load_full_artifact_lock(options.snapshot)
    census_path = options.census_report.expanduser().resolve(strict=True)
    admission_path = options.admission.expanduser().resolve(strict=True)
    census_raw = census_path.read_bytes()
    admission_raw = admission_path.read_bytes()
    census = json.loads(census_raw)
    admission = json.loads(admission_raw)
    native_manifest, archive_sha, manifest_sha = _runtime_archive_manifest(
        options.native_runtime_archive.expanduser().resolve(strict=True)
    )
    native_runtime = {
        "archive_sha256": archive_sha,
        "manifest_sha256": manifest_sha,
        "site_packages_sha256": native_manifest["site_packages_sha256"],
        "requirements_lock_sha256": native_manifest["requirements_lock_sha256"],
    }
    from lab.astrbot_native_tool_campaign import _admission
    _admission(admission_path)
    plan = plan_astrbot_command_campaign(
        lock, census,
        artifact_lock_sha256=_sha256(lock_path.read_bytes()),
        census_report_sha256=_sha256(census_raw),
        worker_image=options.worker_image,
        admission=admission,
        admission_sha256=_sha256(admission_raw),
        native_runtime=native_runtime,
        max_scenarios=options.max_scenarios,
    )
    output = options.output.expanduser().absolute()
    sidecar = output.with_suffix(output.suffix + ".sha256")
    if output.exists() or sidecar.exists():
        raise FileExistsError(f"refusing to overwrite an existing campaign plan: {output}")
    if options.queue_db is not None:
        plan["queue_registration"] = register_campaign_plan(str(options.queue_db), plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        digest = _sha256(data)
        sidecar.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(json.dumps({"status": plan["status"], "run_id": plan["run_id"], "counts": plan["counts"],
                      "output": str(output), "sha256": digest,
                      "queue_registration": plan.get("queue_registration")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
