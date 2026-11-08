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
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.workers import DockerWorker, WorkerLimits, WorkerSpec  # noqa: E402

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


def download_wheels(bundle: Path) -> dict[str, Any]:
    """Binary-only resolution in a pinned container with network; no build backend runs."""
    proxy_env = [arg for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY")
                 if os.environ.get(key) for arg in ("-e", f"{key}={os.environ[key]}")]
    started = time.monotonic()
    completed = subprocess.run(
        ["docker", "run", "--rm", "--network", "host", *proxy_env,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--per-stratum", type=int, default=6)
    options = parser.parse_args(argv)
    report = run(options)
    print(json.dumps({"status": report["status"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
