"""AB-04: AstrBot install/load pilot in admitted, network-disabled Linux Docker Workers.

Selects a deterministic, stratified pilot from a frozen AstrBot catalog snapshot,
prepares one external bundle per plugin (the archive's plugin tree, its requirements,
binary-only wheels pre-downloaded in a pinned container, and the Ligamina AstrBot
Host code), then loads each plugin in a fresh Worker with `--network none`.

Run inside WSL with the control environment:
  python -m lab.astrbot_census --snapshot catalogs/<ab-id> --admission <admission.json>
      --image ligamina-astrbot-runtime@sha256:<id> --bundle-root /home/<user>/<new dir>
      --output outputs/acceptance/<new report>.json [--per-stratum 6]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.workers import DockerWorker, WorkerLimits, WorkerSpec  # noqa: E402
from lab.automation.reconcile import DockerReconciler, resume_reconciled_run  # noqa: E402
from lab.queue import QueueError, StageQueue  # noqa: E402

DEFAULT_CACHE = ROOT.parent / "Ligamina-artifact-cache"
LOCK_FILE = ROOT / "deploy/lab/linux/astrbot/requirements.lock.txt"
DOWNLOAD_IMAGE = "python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a"
HOST_FILES = ["__init__.py", "bridge.py", "commands.py", "contracts.py", "lifecycle.py", "runtime.py"]
STRATA = {
    "plain": lambda notes: not notes & {"has_requirements", "platform_specific_code", "llm_dependent"},
    "requirements": lambda notes: "has_requirements" in notes and not notes & {"platform_specific_code", "llm_dependent"},
    "platform_specific": lambda notes: "platform_specific_code" in notes,
    "llm_dependent": lambda notes: "llm_dependent" in notes and "platform_specific_code" not in notes,
}
DEFAULT_FULL_CENSUS_QUEUE = Path.home() / ".local" / "share" / "ligamina" / "p7-astrbot-census.sqlite3"
FULL_CENSUS_LEASE_SECONDS = 1500
FULL_CENSUS_MAX_WORKERS = 2
MAX_CENSUS_COMMANDS = 512
MAX_CENSUS_COMMAND_BYTES = 256 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_pilot(lock: dict[str, Any], per_stratum: int) -> list[tuple[str, dict[str, Any]]]:
    """Deterministic: frozen entries ordered by registry_entry_id, first N per stratum."""
    frozen = sorted((entry for entry in lock["entries"] if entry["status"] == "frozen"), key=lambda e: e["registry_entry_id"])
    chosen: list[tuple[str, dict[str, Any]]] = []
    used: set[str] = set()
    for stratum, predicate in STRATA.items():
        picked = 0
        for entry in frozen:
            if picked >= per_stratum:
                break
            if entry["registry_entry_id"] in used or not predicate(set(entry.get("hosting_notes", []))):
                continue
            chosen.append((stratum, entry))
            used.add(entry["registry_entry_id"])
            picked += 1
    return chosen


def select_full_census(lock: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Select every frozen artifact exactly once, retaining the pilot's stratum precedence."""
    entries = lock.get("entries")
    if not isinstance(entries, list):
        raise ValueError("artifact lock entries must be a list")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("artifact lock entries must be objects")
    frozen = sorted((entry for entry in entries if entry.get("status") == "frozen"), key=lambda e: e.get("registry_entry_id", ""))
    ids = [entry.get("registry_entry_id") for entry in frozen]
    if (
        any(not isinstance(entry_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", entry_id) for entry_id in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("frozen artifact identities must be non-empty and unique")
    selected = []
    for entry in frozen:
        notes = set(entry.get("hosting_notes", []))
        stratum = next((name for name, predicate in STRATA.items() if predicate(notes)), "plain")
        selected.append((stratum, entry))
    return selected


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _load_full_artifact_lock(snapshot: Path) -> tuple[dict[str, Any], Path]:
    snapshot = Path(snapshot).expanduser().resolve(strict=True)
    lock_path = snapshot / "artifacts.lock.json"
    raw = lock_path.read_bytes()
    sidecar = lock_path.with_suffix(lock_path.suffix + ".sha256")
    try:
        expected_sha256 = sidecar.read_text(encoding="ascii").split()[0]
    except (OSError, IndexError, UnicodeDecodeError) as error:
        raise ValueError("frozen AstrBot artifact lock has no valid SHA-256 sidecar") from error
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ValueError("frozen AstrBot artifact lock SHA-256 sidecar mismatch")
    lock = json.loads(raw.decode("utf-8"))
    if not isinstance(lock, dict) or lock.get("snapshot_id") != snapshot.name:
        raise ValueError("AstrBot artifact lock snapshot identity does not match its frozen directory")
    select_full_census(lock)
    return lock, lock_path


def full_census_plan(snapshot: Path) -> dict[str, Any]:
    lock, lock_path = _load_full_artifact_lock(snapshot)
    selected = select_full_census(lock)
    selection_ids_sha256 = hashlib.sha256(_canonical_json([entry["registry_entry_id"] for _, entry in selected])).hexdigest()
    manifest = {
        "schema_version": 1,
        "kind": "astrbot_full_frozen_load_census",
        "snapshot_id": lock.get("snapshot_id"),
        "artifacts_lock_sha256": _sha256(lock_path),
        "selection_sha256": selection_ids_sha256,
        "selected_entries": len(selected),
        "snapshot_entries": len(lock["entries"]),
        "excluded_non_frozen": len(lock["entries"]) - len(selected),
        "policy": "all frozen artifact identities sorted by registry_entry_id exactly once",
    }
    manifest_sha256 = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    return {
        "schema_version": 1,
        "kind": "astrbot_full_census_plan",
        "status": "planned",
        "snapshot_id": lock.get("snapshot_id"),
        "artifacts_lock_sha256": manifest["artifacts_lock_sha256"],
        "denominator": {
            "snapshot_entries": len(lock["entries"]),
            "frozen_entries": len(selected),
            "selected_entries": len(selected),
            "excluded_non_frozen": len(lock["entries"]) - len(selected),
            "closed_exactly_once": len({entry["registry_entry_id"] for _, entry in selected}) == len(selected),
        },
        "selection": {
            "policy": manifest["policy"],
            "strata": {name: sum(stratum == name for stratum, _ in selected) for name in STRATA},
            "selected_ids_sha256": selection_ids_sha256,
        },
        "manifest": {**manifest, "manifest_sha256": manifest_sha256},
        "scope_note": "Plan only; does not install plugins, start Workers, or claim load results.",
    }


def _requirement_name(line: str) -> str:
    return re.split(r"[\s<>=!~;\[(@]", line.strip(), maxsplit=1)[0].lower().replace("_", "-").replace(".", "-")


def locked_names() -> set[str]:
    return {_requirement_name(line) for line in constraints_text().splitlines() if line}


def constraints_text() -> str:
    lines = []
    for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--hash")) or line.startswith("--"):
            continue
        lines.append(line.rstrip(" \\"))
    return "\n".join(lines) + "\n"


def metadata_dir_name(metadata_text: str) -> str | None:
    """AstrBot 4.29.1 installs a plugin into a directory named after metadata.yaml's `name`
    (stripped; must be a non-keyword Python identifier). Plugins rely on it, e.g.
    `from data.plugins.<name>.utils import ...`. Only the top-level scalar key is read;
    a leading UTF-8 BOM is ignored, as YAML loaders do."""
    for line in metadata_text.removeprefix("\ufeff").splitlines():
        match = re.match(r"name:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = re.sub(r"\s+#.*$", "", match.group(1)).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1].strip()
        return value if value.isidentifier() and not keyword.iskeyword(value) else None
    return None


def extract_plugin(archive_path: Path, plugin_root: str, target: Path) -> tuple[str, str]:
    """Extract only the plugin tree; paths were validated during the freeze and again here.

    Returns (directory name, source): the metadata name AstrBot's installer would use, or a
    sanitized archive-root name when the metadata has no usable name."""
    prefix = "" if plugin_root == "." else plugin_root + "/"
    with zipfile.ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
        metadata_text = next((archive.read(prefix + filename).decode("utf-8", "replace")
                              for filename in ("metadata.yaml", "metadata.yml") if prefix + filename in archive.namelist()), "")
        name, source = metadata_dir_name(metadata_text), "metadata_name"
        if name is None:
            name = re.sub(r"[^A-Za-z0-9_]", "_", PurePosixPath(plugin_root).name if plugin_root != "." else "plugin") or "plugin"
            source = "archive_root"
        destination = target / name
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            relative = PurePosixPath(info.filename[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe path in archive: {info.filename}")
            out = destination.joinpath(*relative.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(archive.read(info))
    return name, source


def download_wheels(
    bundle: Path,
    *,
    resource_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Binary-only resolution in a pinned container with network; no build backend runs."""
    proxy_env = [arg for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY")
                 if os.environ.get(key) for arg in ("-e", f"{key}={os.environ[key]}")]
    started = time.monotonic()
    resource_labels = []
    if resource_identity is not None:
        expected = {"campaign", "job", "attempt", "generation"}
        if set(resource_identity) != expected or any(not resource_identity[key] for key in expected):
            raise ValueError("dependency Worker requires exact campaign, job, attempt, and generation labels")
        resource_labels = [
            argument
            for key, label in (
                ("campaign", "io.ligamina.campaign"),
                ("job", "io.ligamina.job"),
                ("attempt", "io.ligamina.attempt"),
                ("generation", "io.ligamina.generation"),
            )
            for argument in ("--label", f"{label}={resource_identity[key]}")
        ]
    completed = subprocess.run(
        ["docker", "run", "--rm", "--network", "host", *proxy_env,
         *resource_labels,
         "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
         "-v", f"{bundle}:/bundle", DOWNLOAD_IMAGE,
         "python", "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
         "--dest", "/bundle/wheelhouse", "-r", "/bundle/requirements.txt", "-c", "/bundle/constraints.txt"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    wheels = sorted(path for path in (bundle / "wheelhouse").glob("*") if path.is_file())
    return {
        "status": "resolved" if completed.returncode == 0 else "unresolvable",
        "seconds": round(time.monotonic() - started, 1),
        "wheels": {path.name: _sha256(path) for path in wheels},
        "error_tail": None if completed.returncode == 0 else (completed.stderr or completed.stdout)[-1500:],
    }


def prepare_bundle(bundle: Path, entry: dict[str, Any], cache_root: Path) -> dict[str, Any]:
    bundle.mkdir(parents=True)
    (bundle / "wheelhouse").mkdir()
    artifact = cache_root / entry["artifact"]["cache_path"]
    if _sha256(artifact) != entry["artifact"]["computed_digests"]["sha256"]:
        raise ValueError(f"cached archive differs from the frozen digest: {entry['registry_entry_id']}")
    plugin_name, name_source = extract_plugin(artifact, entry["inspection"]["plugin_root"], bundle / "plugin")
    locked = locked_names()
    requirements, satisfied = [], []
    for line in entry["inspection"].get("requirements") or []:
        if line.startswith(("-", "git+", "http:", "https:")):
            requirements.append(line)  # options/URLs are resolved (and usually rejected) by pip
            continue
        (satisfied if _requirement_name(line) in locked else requirements).append(line)
    (bundle / "requirements.txt").write_text("\n".join(requirements) + ("\n" if requirements else ""), encoding="utf-8")
    (bundle / "constraints.txt").write_text(constraints_text(), encoding="utf-8")
    host = bundle / "project" / "hosts" / "astrbot" / "ligamina_astrbot"
    host.mkdir(parents=True)
    for name in HOST_FILES:
        shutil.copy2(ROOT / "hosts/astrbot/ligamina_astrbot" / name, host / name)
    for version in ("v0.1", "v0.2"):
        shutil.copytree(ROOT / "contracts" / version, bundle / "project" / "contracts" / version)
    (bundle / "side").mkdir()
    shutil.copy2(ROOT / "lab/astrbot_census_side.py", bundle / "side" / "astrbot_census_side.py")
    return {"plugin_dir": plugin_name, "plugin_dir_source": name_source, "requirement_count": len(requirements), "satisfied_by_runtime": satisfied}


def _checkpoint(output: Path, report: dict[str, Any]) -> None:
    """Persist progress after every plugin so an interrupted pilot keeps its settled rows."""
    path = output.with_name(output.stem + ".checkpoint.json")
    data = json.dumps({**report, "status": "in_progress", "checkpoint_at_utc": _now()}, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data + "\n", encoding="utf-8")


def run(options: argparse.Namespace) -> dict[str, Any]:
    output = options.output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite an existing report: {output}")
    admission = json.loads(options.admission.read_text(encoding="utf-8"))
    capabilities = admission.get("capabilities", {})
    if not (admission.get("status") == "admitted" and capabilities.get("network_disabled") and capabilities.get("cleanup_evidence_required")):
        raise RuntimeError("the Linux Worker backend has no admitted report")
    lock_path = options.snapshot / "artifacts.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    pilot = select_pilot(lock, options.per_stratum)
    bundle_root = options.bundle_root.expanduser().absolute()
    if bundle_root.exists():
        raise FileExistsError(f"refusing to reuse a bundle root: {bundle_root}")
    worker = DockerWorker(image_digest=options.image, repository_root=ROOT)
    run_id = f"ab04-{int(time.time())}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "astrbot_linux_install_load_pilot",
        "run_id": run_id,
        "started_at_utc": _now(),
        "snapshot_id": lock["snapshot_id"],
        "artifacts_lock_sha256": _sha256(lock_path),
        "admission_path": str(options.admission),
        "admission_sha256": _sha256(options.admission),
        "runtime_image": options.image,
        "download_image": DOWNLOAD_IMAGE,
        "selection": {"policy": "frozen entries ordered by registry_entry_id, first N per stratum", "per_stratum": options.per_stratum,
                      "strata": {name: sum(1 for stratum, _ in pilot if stratum == name) for name in STRATA}},
        "results": [],
    }
    for index, (stratum, entry) in enumerate(pilot, start=1):
        entry_id = entry["registry_entry_id"]
        bundle = bundle_root / entry_id
        row: dict[str, Any] = {"registry_entry_id": entry_id, "stratum": stratum,
                               "artifact_sha256": entry["artifact"]["computed_digests"]["sha256"],
                               "hosting_notes": entry.get("hosting_notes", [])}
        try:
            row["bundle"] = prepare_bundle(bundle, entry, options.cache_root)
        except Exception as error:
            row.update({"status": "skipped", "reason_code": "bundle_preparation_failed", "diagnostic": str(error)[:300]})
            report["results"].append(row)
            continue
        if row["bundle"]["requirement_count"]:
            row["dependencies"] = download_wheels(bundle)
            if row["dependencies"]["status"] != "resolved":
                row.update({"status": "skipped", "reason_code": "dependency_unresolvable"})
                report["results"].append(row)
                print(f"[{index}/{len(pilot)}] {entry_id} skipped: dependencies", file=sys.stderr, flush=True)
                continue
        spec = WorkerSpec(
            run_id=run_id, entry_id=entry_id, attempt_id=f"{entry_id}-load", generation=1, job_id=f"{entry_id}:load",
            command=("/opt/astrbot/venv/bin/python", "/input/side/astrbot_census_side.py"),
            input_dir=bundle,
            limits=WorkerLimits(timeout_seconds=420, memory_mb=3072, cpu_cores=2.0, process_limit=256, output_limit_bytes=1024 * 1024),
            entrypoint="/usr/bin/env",
        )
        outcome = worker.run(spec)
        try:
            side = json.loads(outcome.stdout.decode("utf-8").strip().splitlines()[-1])
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
            side = {"status": "unknown", "reason_code": "worker_output_unreadable"}
        if outcome.status != "passed" and side.get("status") in (None, "passed"):
            side = {"status": "unknown", "reason_code": outcome.reason_code or "worker_failed"}
        row.update({
            "status": side.get("status", "unknown"),
            "reason_code": side.get("reason_code"),
            "side": side,
            "worker": {"worker_id": outcome.worker_id, "status": outcome.status, "reason_code": outcome.reason_code,
                       "exit_code": outcome.exit_code, "cleanup_confirmed": outcome.cleanup_confirmed,
                       "elapsed_seconds": round(outcome.elapsed_seconds, 1), "peak_memory_bytes": outcome.peak_memory_bytes,
                       "stdout_sha256": outcome.stdout_sha256, "stderr_sha256": outcome.stderr_sha256},
        })
        report["results"].append(row)
        _checkpoint(output, report)
        print(f"[{index}/{len(pilot)}] {entry_id} {row['status']} {row.get('reason_code') or ''}", file=sys.stderr, flush=True)
        if not outcome.cleanup_confirmed:
            report["stop_reason"] = "worker_cleanup_unconfirmed"
            break
    from collections import Counter

    report["completed_at_utc"] = _now()
    report["counts"] = {
        "selected": len(pilot),
        "settled": len(report["results"]),
        "status": dict(sorted(Counter(row["status"] for row in report["results"]).items())),
        "reason_codes": dict(sorted(Counter(row.get("reason_code") or "none" for row in report["results"]).items())),
        "cleanup_unconfirmed": sum(1 for row in report["results"] if row.get("worker") and not row["worker"]["cleanup_confirmed"]),
    }
    report["status"] = "completed" if len(report["results"]) == len(pilot) and "stop_reason" not in report else "stopped"
    report["scope_note"] = "Install/load pilot only. Loading is not behavior compatibility; plugin code ran only inside network-disabled Workers."
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    output.with_suffix(output.suffix + ".sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  {output.name}\n", encoding="ascii")
    return report


def _full_census_identity(options: argparse.Namespace, lock_path: Path) -> tuple[dict[str, Any], str, str]:
    lock, verified_lock_path = _load_full_artifact_lock(lock_path.parent)
    selected = select_full_census(lock)
    selection_sha256 = hashlib.sha256(_canonical_json([entry["registry_entry_id"] for _, entry in selected])).hexdigest()
    admission_path = options.admission.expanduser().resolve(strict=True)
    bundle_root = options.bundle_root.expanduser().absolute().resolve()
    cache_root = options.cache_root.expanduser().resolve(strict=True)
    output = options.output.expanduser().absolute()
    manifest = {
        "schema_version": 1,
        "kind": "astrbot_full_frozen_load_census",
        "snapshot_id": lock["snapshot_id"],
        "artifacts_lock_sha256": _sha256(verified_lock_path),
        "selection_sha256": selection_sha256,
        "selected_entries": len(selected),
        "snapshot_entries": len(lock["entries"]),
        "excluded_non_frozen": len(lock["entries"]) - len(selected),
        "admission_path": str(admission_path),
        "admission_sha256": _sha256(admission_path),
        "runtime_image": options.image,
        "bundle_root": str(bundle_root),
        "cache_root": str(cache_root),
        "report_path": str(output),
        "workers": options.workers,
        "lease_seconds": FULL_CENSUS_LEASE_SECONDS,
        "worker_timeout_seconds": 420,
        "dependency_timeout_seconds": 900,
    }
    manifest_sha256 = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    run_id = f"p7-ab04-{lock['snapshot_id']}-{manifest_sha256[:12]}"
    return manifest, manifest_sha256, run_id


def _write_full_census_report(output: Path, report: dict[str, Any]) -> str:
    data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    digest = hashlib.sha256(data).hexdigest()
    sidecar = output.with_suffix(output.suffix + ".sha256")
    sidecar_tmp = sidecar.with_name(sidecar.name + ".tmp")
    sidecar_tmp.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    os.replace(sidecar_tmp, sidecar)
    return digest


def _summarize_worker_side(side: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded census facts; plugin diagnostics can contain private config."""
    summary: dict[str, Any] = {
        "status": side.get("status") if side.get("status") in {"passed", "failed", "skipped", "unknown"} else "unknown",
        "reason_code": side.get("reason_code") if isinstance(side.get("reason_code"), str) else None,
    }
    install = side.get("install")
    if isinstance(install, dict):
        summary["install"] = {
            "status": install.get("status") if isinstance(install.get("status"), str) else "unknown",
            "seconds": install.get("seconds") if isinstance(install.get("seconds"), (int, float)) else None,
        }
    load = side.get("load")
    if isinstance(load, dict):
        loaded = load.get("loaded_plugins")
        failed = load.get("failed_plugins")
        commands = load.get("commands")
        command_descriptors = []
        command_bytes = 0
        for descriptor in commands[:MAX_CENSUS_COMMANDS] if isinstance(commands, list) else []:
            if not isinstance(descriptor, dict):
                continue
            command_id = descriptor.get("command_id")
            if not isinstance(command_id, str) or not command_id.strip():
                continue
            arguments = []
            raw_arguments = descriptor.get("arguments")
            for argument in raw_arguments[:64] if isinstance(raw_arguments, list) else []:
                if not isinstance(argument, dict):
                    continue
                name, required, kind = argument.get("name"), argument.get("required"), argument.get("type")
                if isinstance(name, str) and isinstance(required, bool) and isinstance(kind, str):
                    arguments.append({"name": name[:128], "required": required, "type": kind[:64]})
            item = {"command_id": command_id[:96], "arguments": arguments}
            size = len(_canonical_json(item))
            if command_bytes + size > MAX_CENSUS_COMMAND_BYTES:
                break
            command_descriptors.append(item)
            command_bytes += size
        commands_truncated = bool(load.get("commands_truncated")) or (
            isinstance(commands, list) and len(commands) > len(command_descriptors)
        )
        summary["load"] = {
            "seconds": load.get("seconds") if isinstance(load.get("seconds"), (int, float)) else None,
            "loaded_plugins": len(loaded) if isinstance(loaded, list) else 0,
            "failed_plugins": len(failed) if isinstance(failed, dict) else 0,
            "commands": len(commands) if isinstance(commands, list) else 0,
            "command_descriptors": command_descriptors,
            "commands_truncated": commands_truncated,
            "startup_sends": load.get("startup_sends") if isinstance(load.get("startup_sends"), int) else 0,
        }
    return summary


def _full_census_report(
    queue: StageQueue,
    run_id: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
    selected: list[tuple[str, dict[str, Any]]],
    *,
    forced_status: str | None = None,
) -> dict[str, Any]:
    jobs = queue.list_jobs(run_id)
    attempts = queue.list_attempts(run_id)
    job_attempts: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        job_attempts.setdefault(attempt["unit_id"], []).append(attempt)
    entry_strata = {entry["registry_entry_id"]: stratum for stratum, entry in selected}
    entry_hashes = {
        entry["registry_entry_id"]: entry["artifact"]["computed_digests"]["sha256"]
        for _, entry in selected
    }
    results = []
    for job in jobs:
        result = job.get("result") or {}
        results.append({
            "registry_entry_id": job["registry_entry_id"],
            "stratum": entry_strata[job["registry_entry_id"]],
            "artifact_sha256": entry_hashes[job["registry_entry_id"]],
            "status": job["status"],
            "reason_code": (result.get("reason_code") if isinstance(result, dict) else None),
            "attempts_used": job["attempts_used"],
            "attempt_history": job_attempts.get(job["unit_id"], []),
            "result": result,
        })
    queue_counts = queue.counts(run_id)
    terminal_entries = sum(queue_counts.get(status, 0) for status in ("passed", "failed", "skipped", "unknown"))
    cleanup_observations = []
    for attempt in attempts:
        result = attempt.get("result") or {}
        worker = result.get("worker") if isinstance(result, dict) else None
        reconciliation = result.get("reconciliation") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("requires_reconciliation"):
            cleanup_observations.append(
                reconciliation.get("cleanup_confirmed") is True if isinstance(reconciliation, dict) else False
            )
        elif isinstance(worker, dict) and isinstance(worker.get("cleanup_confirmed"), bool):
            cleanup_observations.append(worker["cleanup_confirmed"])
        elif isinstance(reconciliation, dict) and isinstance(reconciliation.get("cleanup_confirmed"), bool):
            cleanup_observations.append(reconciliation["cleanup_confirmed"])
    cleanup_unconfirmed = sum(value is False for value in cleanup_observations)
    queue_state = queue.run(run_id)["state"]
    closed_once = (
        len(jobs) == len(selected)
        and len({job["registry_entry_id"] for job in jobs}) == len(selected)
        and terminal_entries == len(selected)
    )
    status = forced_status or (
        "completed" if closed_once and cleanup_unconfirmed == 0 and queue_state == "completed"
        else "blocked_cleanup" if cleanup_unconfirmed or queue_state == "interrupted"
        else "in_progress"
    )
    from collections import Counter

    return {
        "schema_version": 1,
        "kind": "astrbot_linux_install_load_census",
        "status": status,
        "run_id": run_id,
        "snapshot_id": manifest["snapshot_id"],
        "manifest": {**manifest, "manifest_sha256": manifest_sha256},
        "denominator": {
            "snapshot_entries": manifest["snapshot_entries"],
            "frozen_entries": len(selected),
        "selected_entries": len(selected),
            "terminal_entries": terminal_entries,
            "closed_exactly_once": closed_once,
        },
        "counts": {
            "selected": len(selected),
            "settled": terminal_entries,
            "status": {key: queue_counts.get(key, 0) for key in ("passed", "failed", "skipped", "unknown", "queued", "leased")},
            "reason_codes": dict(sorted(Counter(
                (job.get("result") or {}).get("reason_code") or "none"
                for job in jobs
            ).items())),
        },
        "cleanup": {
            "attempt_count": len(attempts),
            "cleanup_observations": len(cleanup_observations),
            "cleanup_unconfirmed": cleanup_unconfirmed,
            "all_observed_cleanup_confirmed": cleanup_unconfirmed == 0,
        },
        "queue": {
            "state": queue_state,
            "counts": queue_counts,
            "journal_event_count": len(queue.journal(run_id)),
        },
        "results": results,
        "attempts": attempts,
        "scope_note": "AstrBot Linux install/load census only. A passed load is not behavior compatibility or plugin support; unknown and skipped outcomes remain distinct.",
    }


def _remove_reconciled_bundles(queue: StageQueue, run_id: str, bundle_root: Path) -> None:
    attempts_by_unit: dict[str, list[dict[str, Any]]] = {}
    for attempt in queue.list_attempts(run_id):
        attempts_by_unit.setdefault(attempt["unit_id"], []).append(attempt)
    resolved_root = bundle_root.resolve(strict=True)
    for job in queue.list_jobs(run_id):
        if job["status"] != "queued" or job["attempts_used"] < 1:
            continue
        history = attempts_by_unit.get(job["unit_id"], [])
        latest = history[-1].get("result") if history else None
        reconciliation = latest.get("reconciliation") if isinstance(latest, dict) else None
        if not isinstance(reconciliation, dict) or reconciliation.get("cleanup_confirmed") is not True:
            continue
        target = bundle_root / job["registry_entry_id"]
        if target.exists():
            resolved = target.resolve(strict=True)
            if resolved == resolved_root or resolved_root not in resolved.parents:
                raise RuntimeError("AstrBot bundle retry target escaped its external bundle root")
            shutil.rmtree(resolved)


def _wait_for_leased_attempts(queue: StageQueue, run_id: str) -> None:
    """Wait for old executors to finish or reach their bounded Worker lease before reconciliation."""
    while True:
        leased = [job for job in queue.list_jobs(run_id) if job["status"] == "leased"]
        if not leased:
            return
        expires = [job["lease_expires_at"] for job in leased if isinstance(job.get("lease_expires_at"), (int, float))]
        if not expires:
            raise QueueError("leased AstrBot jobs have no expiry for safe reconciliation")
        delay = max(expires) - time.time()
        if delay <= 0:
            return
        print(f"waiting for {len(leased)} active AstrBot lease(s) to finish or expire", file=sys.stderr, flush=True)
        time.sleep(min(delay, 10.0))


def run_full_census(options: argparse.Namespace) -> dict[str, Any]:
    output = options.output.expanduser().absolute()
    snapshot = options.snapshot.expanduser().resolve(strict=True)
    lock, lock_path = _load_full_artifact_lock(snapshot)
    selected = select_full_census(lock)
    if options.workers < 1 or options.workers > FULL_CENSUS_MAX_WORKERS:
        raise ValueError(f"--workers must be between 1 and {FULL_CENSUS_MAX_WORKERS}")
    admission = json.loads(options.admission.read_text(encoding="utf-8"))
    capabilities = admission.get("capabilities", {})
    if not (
        admission.get("status") == "admitted"
        and capabilities.get("network_disabled") is True
        and capabilities.get("cleanup_evidence_required") is True
    ):
        raise RuntimeError("the Linux Worker backend has no admitted report")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", options.image):
        raise ValueError("runtime image must be pinned by sha256 digest")

    manifest, manifest_sha256, run_id = _full_census_identity(options, lock_path)
    if output.exists() and not options.resume:
        raise FileExistsError(f"refusing to overwrite an existing report: {output}")
    if options.resume and output.exists():
        existing_report = json.loads(output.read_text(encoding="utf-8"))
        if existing_report.get("run_id") != run_id or existing_report.get("manifest", {}).get("manifest_sha256") != manifest_sha256:
            raise ValueError("resume report belongs to a different frozen run manifest")

    bundle_root = options.bundle_root.expanduser().absolute()
    resolved_bundle_root = bundle_root.resolve()
    repository_root = ROOT.resolve(strict=True)
    if resolved_bundle_root == repository_root or repository_root in resolved_bundle_root.parents:
        raise ValueError("AstrBot Worker bundles must be outside the repository")
    if bundle_root.exists() and not options.resume:
        raise FileExistsError(f"refusing to reuse a bundle root: {bundle_root}")
    queue_path = options.queue_db.expanduser().absolute().resolve()
    if queue_path == repository_root or repository_root in queue_path.parents:
        raise ValueError("AstrBot census SQLite queue must be outside the repository")
    if options.resume and not queue_path.is_file():
        raise QueueError("cannot resume a full-census run without its SQLite queue")
    worker = DockerWorker(image_digest=options.image, repository_root=ROOT)
    bundle_root.mkdir(parents=True, exist_ok=True)

    queue = StageQueue(queue_path)
    if options.resume:
        try:
            existing_run = queue.run(run_id)
        except QueueError as error:
            raise QueueError("cannot resume the matching full-census run; frozen inputs or runtime identity changed") from error
        queue.create_run(run_id, lock["snapshot_id"], manifest_sha256, manifest, fencing_required=True)
        queue.enqueue_jobs(run_id, [
            {
                "unit_id": entry["registry_entry_id"],
                "registry_entry_id": entry["registry_entry_id"],
                "ecosystem": "astrbot",
                "platform": "linux",
                "profile_id": "astrbot-4.29.1-load",
                "stage": "load",
                "case_id": "full-frozen-catalog",
                "max_attempts": 2,
                "payload": {"stratum": stratum, "artifact_sha256": entry["artifact"]["computed_digests"]["sha256"]},
            }
            for stratum, entry in selected
        ])
        if existing_run["state"] != "completed":
            if existing_run["state"] != "interrupted":
                queue.interrupt_run(run_id)
            _wait_for_leased_attempts(queue, run_id)
            try:
                resume_reconciled_run(queue, run_id, DockerReconciler())
            except QueueError:
                blocked = _full_census_report(
                    queue, run_id, manifest, manifest_sha256, selected, forced_status="blocked_cleanup",
                )
                _write_full_census_report(output, blocked)
                return blocked
            _remove_reconciled_bundles(queue, run_id, bundle_root)
    else:
        created = queue.create_run(run_id, lock["snapshot_id"], manifest_sha256, manifest, fencing_required=True)
        if not created:
            raise FileExistsError(f"full-census queue run already exists; use --resume: {run_id}")
        queue.enqueue_jobs(run_id, [
            {
                "unit_id": entry["registry_entry_id"],
                "registry_entry_id": entry["registry_entry_id"],
                "ecosystem": "astrbot",
                "platform": "linux",
                "profile_id": "astrbot-4.29.1-load",
                "stage": "load",
                "case_id": "full-frozen-catalog",
                "max_attempts": 2,
                "payload": {"stratum": stratum, "artifact_sha256": entry["artifact"]["computed_digests"]["sha256"]},
            }
            for stratum, entry in selected
        ])

    entries_by_id = {entry["registry_entry_id"]: entry for _, entry in selected}
    stop = threading.Event()
    report_lock = threading.RLock()
    last_report: dict[str, Any] = {}

    def save_report(forced_status: str | None = None) -> dict[str, Any]:
        with report_lock:
            current = _full_census_report(queue, run_id, manifest, manifest_sha256, selected, forced_status=forced_status)
            _write_full_census_report(output, current)
            last_report.clear()
            last_report.update(current)
            return current

    def process_claimed(job: dict[str, Any], worker_id: str) -> bool:
        entry = entries_by_id[job["registry_entry_id"]]
        bundle = bundle_root / job["registry_entry_id"]
        result: dict[str, Any]
        try:
            cache_root = options.cache_root.expanduser().resolve(strict=True)
            artifact_path = (cache_root / entry["artifact"]["cache_path"]).resolve(strict=True)
            if cache_root not in artifact_path.parents:
                raise ValueError("cached AstrBot artifact escaped the configured cache root")
            if bundle.exists():
                raise FileExistsError(f"refusing to overwrite an existing per-entry bundle: {bundle}")
            bundle_result = prepare_bundle(bundle, entry, cache_root)
        except Exception as error:
            result = {"status": "skipped", "reason_code": "bundle_preparation_failed", "diagnostic": str(error)[:300], "cleanup_applicable": False}
            queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
            save_report()
            return True

        if bundle_result["requirement_count"]:
            try:
                resource_identity = {
                    "campaign": run_id,
                    "job": job["job_id"],
                    "attempt": job["attempt_id"],
                    "generation": str(job["generation"]),
                }
                dependencies = download_wheels(bundle, resource_identity=resource_identity)
                error_tail = dependencies.pop("error_tail", None)
                if error_tail:
                    error_bytes = str(error_tail).encode("utf-8", errors="replace")
                    dependencies["error_tail_sha256"] = hashlib.sha256(error_bytes).hexdigest()
                    dependencies["error_tail_bytes"] = len(error_bytes)
                dependency_cleanup = DockerReconciler().reconcile(
                    campaign_id=run_id,
                    job_id=job["job_id"],
                    attempt_id=job["attempt_id"],
                    generation=job["generation"],
                ).as_evidence()
                dependencies["container_cleanup"] = dependency_cleanup
            except Exception as error:
                result = {"status": "unknown", "reason_code": "dependency_resolution_interrupted", "diagnostic": type(error).__name__, "requires_reconciliation": True}
                queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
                queue.interrupt_run(run_id)
                save_report("blocked_cleanup")
                return False
            if not dependency_cleanup.get("cleanup_confirmed"):
                result = {
                    "status": "unknown",
                    "reason_code": "dependency_worker_cleanup_unconfirmed",
                    "dependencies": dependencies,
                    "requires_reconciliation": True,
                }
                queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
                queue.interrupt_run(run_id)
                save_report("blocked_cleanup")
                return False
            if dependencies["status"] != "resolved":
                result = {"status": "skipped", "reason_code": "dependency_unresolvable", "dependencies": dependencies, "cleanup_applicable": False}
                queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
                save_report()
                return True

        spec = WorkerSpec(
            run_id=run_id,
            entry_id=job["registry_entry_id"],
            attempt_id=job["attempt_id"],
            generation=job["generation"],
            job_id=job["job_id"],
            command=("/opt/astrbot/venv/bin/python", "/input/side/astrbot_census_side.py"),
            input_dir=bundle,
            limits=WorkerLimits(timeout_seconds=420, memory_mb=3072, cpu_cores=2.0, process_limit=256, output_limit_bytes=1024 * 1024),
            entrypoint="/usr/bin/env",
        )
        try:
            outcome = worker.run(spec)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            result = {
                "status": "unknown", "reason_code": "worker_dispatch_exception",
                "diagnostic": type(error).__name__,
                "requires_reconciliation": True,
                "worker": {"cleanup_confirmed": False},
            }
            queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
            queue.interrupt_run(run_id)
            save_report("blocked_cleanup")
            return False

        try:
            side = json.loads(outcome.stdout.decode("utf-8").strip().splitlines()[-1])
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError):
            side = {"status": "unknown", "reason_code": "worker_output_unreadable"}
        if not isinstance(side, dict):
            side = {"status": "unknown", "reason_code": "worker_output_invalid_shape"}
        if outcome.status != "passed" and side.get("status") in (None, "passed"):
            side = {"status": "unknown", "reason_code": outcome.reason_code or "worker_failed"}
        status = side.get("status", "unknown")
        if status not in {"passed", "failed", "skipped", "unknown"}:
            side = {"status": "unknown", "reason_code": "invalid_worker_status"}
            status = "unknown"
        cleanup_confirmed = outcome.cleanup_confirmed is True
        result_status = status if cleanup_confirmed else "unknown"
        reason_code = side.get("reason_code") if cleanup_confirmed else "worker_cleanup_unconfirmed"
        result = {
            "status": result_status,
            "reason_code": reason_code,
            "side": _summarize_worker_side(side),
            "worker": {
                "worker_id": outcome.worker_id,
                "status": outcome.status,
                "reason_code": outcome.reason_code,
                "exit_code": outcome.exit_code,
                "cleanup_confirmed": cleanup_confirmed,
                "elapsed_seconds": round(outcome.elapsed_seconds, 1),
                "peak_memory_bytes": outcome.peak_memory_bytes,
                "stdout_sha256": outcome.stdout_sha256,
                "stderr_sha256": outcome.stderr_sha256,
                "output_truncated": outcome.output_truncated,
            },
            "requires_reconciliation": not cleanup_confirmed,
        }
        queue.finish(job["job_id"], worker_id, result, attempt_id=job["attempt_id"])
        if not cleanup_confirmed:
            stop.set()
            queue.interrupt_run(run_id)
            save_report("blocked_cleanup")
            return False
        save_report()
        return True

    def drain(worker_index: int) -> None:
        worker_id = f"{run_id}-worker-{worker_index}"
        while not stop.is_set():
            try:
                job = queue.claim(run_id, worker_id, lease_seconds=FULL_CENSUS_LEASE_SECONDS)
            except QueueError:
                if queue.run(run_id)["state"] == "interrupted":
                    return
                raise
            if job is None:
                return
            if not process_claimed(job, worker_id):
                stop.set()
                return

    forced_status = None
    try:
        save_report()
        with ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="astrbot-census") as pool:
            futures = [pool.submit(drain, index) for index in range(options.workers)]
            for future in futures:
                future.result()
    except KeyboardInterrupt:
        stop.set()
        forced_status = "interrupted"
        try:
            if queue.run(run_id)["state"] != "interrupted":
                queue.interrupt_run(run_id)
        finally:
            save_report(forced_status)
    except Exception:
        stop.set()
        try:
            if queue.run(run_id)["state"] != "interrupted":
                queue.interrupt_run(run_id)
        finally:
            save_report("interrupted")
        raise
    report = save_report(forced_status)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--per-stratum", type=int, default=6)
    parser.add_argument("--full-census", action="store_true", help="select every frozen AstrBot artifact")
    parser.add_argument("--plan-only", action="store_true", help="print the hash-bound frozen selection without dispatching Workers")
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_FULL_CENSUS_QUEUE)
    parser.add_argument("--resume", action="store_true", help="resume the exact full-census run in --queue-db")
    parser.add_argument("--workers", type=int, default=2)
    options = parser.parse_args(argv)
    if options.plan_only:
        if not options.full_census:
            parser.error("--plan-only requires --full-census")
        plan = full_census_plan(options.snapshot)
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if not all((options.admission, options.image, options.bundle_root, options.output)):
        parser.error("--admission, --image, --bundle-root, and --output are required unless --plan-only is used")
    if options.resume and not options.full_census:
        parser.error("--resume is supported only with --full-census")
    if options.full_census:
        try:
            report = run_full_census(options)
        except (QueueError, OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            print(f"AstrBot full census failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps({"status": report["status"], "counts": report["counts"], "run_id": report["run_id"]}, ensure_ascii=False))
        return 0 if report["status"] == "completed" else (130 if report["status"] == "interrupted" else 3)
    report = run(options)
    print(json.dumps({"status": report["status"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
