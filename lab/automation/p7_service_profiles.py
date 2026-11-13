"""Admit hash-bound offline service-profile variants on the P7 Linux Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.automation import p7_differential_campaign as p7
from lab.automation.behavior_v2 import ARTIFACT_CACHE, CATALOG_ROOT
from lab.profiles import _npm_command, _integrity_matches

ADMISSION_PATH = ROOT / "outputs/acceptance/phase7-linux-admission-20261110.json"
PILOT_PATH = ROOT / "outputs/acceptance/p7-p704-differential-pilot-20261110-retry3.json"
DEFAULT_OUTPUT = ROOT / "outputs/acceptance/p7-p708-profile-admission-20261110.json"
PROFILE_PATHS = {
    "koishi": ROOT / "samples/profiles/p7-koishi-offline-services-v1.json",
    "nonebot": ROOT / "samples/profiles/p7-nonebot-offline-services-v1.json",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _artifact_digest(record: dict[str, Any], platform: str) -> tuple[dict[str, Any], str]:
    item = next((row for row in record.get("artifacts", []) if row.get("platform") == platform), None)
    artifact = item.get("artifact") if isinstance(item, dict) else None
    digest = artifact.get("computed_digests", {}).get("sha256") if isinstance(artifact, dict) else None
    if not isinstance(artifact, dict) or not isinstance(digest, str):
        raise ValueError(f"frozen artifact is unavailable for {record.get('registry_entry_id')} on {platform}")
    return artifact, digest


def _frozen_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog, artifacts, profiles = p7._frozen_records()
    return catalog, artifacts, profiles


def validate_profile_documents() -> dict[str, dict[str, Any]]:
    catalog, artifacts, _ = _frozen_maps()
    profiles = {name: _read(path) for name, path in PROFILE_PATHS.items()}
    for ecosystem, profile in profiles.items():
        if profile.get("ecosystem") != ecosystem or profile.get("schema_version") != 1:
            raise ValueError(f"invalid P7-08 profile document: {ecosystem}")
        for provider in profile.get("providers", []):
            entry_id = provider.get("registry_entry_id")
            catalog_row = catalog.get(entry_id)
            artifact_row = artifacts.get(entry_id)
            if not isinstance(catalog_row, dict) or not isinstance(artifact_row, dict):
                raise ValueError(f"P7-08 provider is not a frozen catalog entry: {entry_id}")
            expected_package = provider.get("package") if ecosystem == "koishi" else provider.get("module")
            artifact, digest = _artifact_digest(artifact_row, "all" if ecosystem == "koishi" else "linux-x86_64")
            if (
                catalog_row.get("package") != expected_package
                or catalog_row.get("advertised_version") != provider.get("version")
                or digest != provider.get("artifact_sha256")
                or artifact_row.get("status") != "locked"
            ):
                raise ValueError(f"P7-08 provider identity/hash differs from frozen catalog: {entry_id}")
        database_provider = profile.get("database_provider")
        if database_provider is not None:
            if not isinstance(database_provider, dict):
                raise ValueError(f"P7-08 database provider must be a structured catalog reference: {ecosystem}")
            provider = next((row for row in profile.get("providers", []) if row.get("registry_entry_id") == database_provider.get("registry_entry_id")), None)
            if (
                not isinstance(provider, dict)
                or provider.get("package") != database_provider.get("package")
                or provider.get("version") != database_provider.get("version")
                or provider.get("artifact_sha256") != database_provider.get("artifact_sha256")
                or "database" not in provider.get("services", [])
            ):
                raise ValueError(f"P7-08 database provider is not the frozen database service provider: {ecosystem}")
        if ecosystem == "koishi":
            startup_accounts = profile.get("startup_accounts")
            if (
                not isinstance(startup_accounts, list)
                or len(startup_accounts) != 1
                or not isinstance(startup_accounts[0], dict)
                or not isinstance(startup_accounts[0].get("account_id"), str)
                or not startup_accounts[0]["account_id"].startswith("p7-offline-")
                or not isinstance(startup_accounts[0].get("guild_ids"), list)
                or not startup_accounts[0]["guild_ids"]
                or any(not isinstance(guild_id, str) or not guild_id.startswith("p7-offline-") for guild_id in startup_accounts[0]["guild_ids"])
            ):
                raise ValueError("P7-08 Koishi startup fixture must contain only synthetic offline account/group IDs")
        target = profile.get("sample", {})
        target_id = target.get("registry_entry_id")
        if not isinstance(catalog.get(target_id), dict):
            raise ValueError(f"P7-08 sample is not present in frozen catalog: {target_id}")
        if catalog[target_id].get("package") != target.get("package"):
            raise ValueError(f"P7-08 sample identity mismatch: {target_id}")
    return profiles


def _candidate_from_frozen(profile: dict[str, Any], *, ecosystem: str) -> dict[str, Any]:
    catalog, artifacts, profile_locks = _frozen_maps()
    target = profile["sample"]
    entry_id = target["registry_entry_id"]
    entry, artifact_record, profile_record = catalog[entry_id], artifacts[entry_id], profile_locks[entry_id]
    platform = "all" if ecosystem == "koishi" else "linux-x86_64"
    artifact, artifact_digest = _artifact_digest(artifact_record, platform)
    profile_value = profile_record.get("profiles", {}).get("linux-x86_64", {})
    lock_ref = profile_value.get("dependency_lock")
    if not isinstance(lock_ref, dict):
        raise ValueError(f"P7-08 sample has no exact Linux profile lock: {entry_id}")
    artifact_path = (ARTIFACT_CACHE / artifact["cache_path"]).resolve(strict=True)
    lock_path = (ARTIFACT_CACHE / lock_ref["cache_path"]).resolve(strict=True)
    if _sha256_file(artifact_path) != artifact_digest or _sha256_file(lock_path) != lock_ref.get("sha256"):
        raise ValueError(f"P7-08 sample artifact/profile lock changed: {entry_id}")
    pilot_entry = next((row for row in _read(PILOT_PATH).get("entries", []) if row.get("registry_entry_id") == entry_id), None)
    if ecosystem == "nonebot" and (not isinstance(pilot_entry, dict) or pilot_entry.get("status") != "completed"):
        raise ValueError("NoneBot service sample is not bound to a completed P7-04 pilot entry")
    service_path = PROFILE_PATHS[ecosystem]
    service_raw = service_path.read_bytes()
    providers = profile["providers"]
    candidate = {
        "ecosystem": ecosystem,
        "registry_entry_id": entry_id,
        "package": entry["package"],
        "version": entry["advertised_version"],
        "artifact_sha256": artifact_digest,
        "artifact_path": artifact_path,
        "artifact_filename": artifact.get("filename"),
        "artifact_record": artifact_record,
        "distribution_name": artifact_record.get("distribution_name") or entry["package"],
        "profile_lock_path": lock_path,
        "profile_lock_sha256": lock_ref["sha256"],
        "profile_id": profile_value.get("profile_id"),
        "catalog_entry": entry,
        "load_evidence": pilot_entry.get("load_evidence", []) if isinstance(pilot_entry, dict) else [],
        "static_inventory": {},
        "static_inventory_sha256": hashlib.sha256(b"{}").hexdigest(),
        "static_command_count": 0,
        "profile_document_path": service_path,
        "service_profile_sha256": _sha256(service_raw),
        "service_profile": profile,
        "preload_plugins": [row["package"] if ecosystem == "koishi" else row["module"] for row in providers],
        "required_services": sorted({service for row in providers for service in row.get("services", [])}),
    }
    if ecosystem == "koishi":
        candidate["previous_skip_evidence"] = {
            "path": target["previous_report"],
            "sha256": _sha256_file(ROOT / target["previous_report"]),
            "reason_code": target["previous_skip_reason"],
            "missing_services": target["previously_missing_services"],
        }
    else:
        _validate_nonebot_provider_lock(lock_path, providers)
    return candidate


def _validate_nonebot_provider_lock(lock_path: Path, providers: list[dict[str, Any]]) -> None:
    lines = lock_path.read_text(encoding="utf-8").splitlines()
    for provider in providers:
        distribution = provider["distribution"]
        version = provider["version"]
        start = next((index for index, line in enumerate(lines) if line.startswith(f"{distribution}=={version}")), None)
        if start is None:
            raise ValueError(f"NoneBot profile lock omits provider {distribution}=={version}")
        block = [lines[start]]
        for line in lines[start + 1:]:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            block.append(line)
        expected = f"--hash=sha256:{provider['artifact_sha256']}"
        if not any(expected in line for line in block):
            raise ValueError(f"NoneBot profile lock does not bind provider wheel bytes: {distribution}=={version}")


def _resolve_koishi_overlay_lock(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    npm_command = _npm_command()
    if not npm_command:
        raise RuntimeError("npm is unavailable for P7-08 combined Koishi lock generation")
    base_lock, _ = p7._load_npm_lock(candidate["profile_lock_path"], {
        "sha256": candidate["profile_lock_sha256"],
        "size_bytes": candidate["profile_lock_path"].stat().st_size,
    })
    base_core = base_lock.get("packages", {}).get("node_modules/@koishijs/core")
    if not isinstance(base_core, dict) or base_core.get("version") != "4.18.11":
        raise ValueError("P7-08 sample base lock does not contain the exact Koishi core package")
    dependencies = {"@koishijs/core": "4.18.11", candidate["package"]: candidate["version"]}
    dependencies.update({row["package"]: row["version"] for row in profile["providers"]})
    cache_root = ARTIFACT_CACHE / "npm-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ligamina-p7-p708-koishi-lock-") as temporary_dir:
        temporary = Path(temporary_dir)
        (temporary / "package.json").write_text(json.dumps({
            "name": "ligamina-p7-koishi-offline-service-profile",
            "version": "1.0.0",
            "private": True,
            "dependencies": dependencies,
        }, indent=2) + "\n", encoding="utf-8")
        user_config = temporary / "empty.npmrc"
        global_config = temporary / "empty-global.npmrc"
        user_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        global_config.write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
        command = [
            *npm_command, "install", "--package-lock-only", "--ignore-scripts", "--strict-peer-deps",
            "--engine-strict", "--no-audit", "--no-fund", "--lockfile-version=3",
            "--registry=https://registry.npmjs.org/", f"--userconfig={user_config}",
            f"--globalconfig={global_config}", f"--cache={cache_root}",
        ]
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env.update({"NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/", "NPM_CONFIG_CACHE": str(cache_root)})
        env.update({key: os.environ[key] for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy") if key in os.environ})
        try:
            result = subprocess.run(command, cwd=temporary, capture_output=True, text=True, timeout=300, check=False, env=env)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("P7-08 npm combined profile lock resolution timed out") from error
        lock_path = temporary / "package-lock.json"
        if result.returncode or not lock_path.is_file():
            digest = _sha256((result.stdout + "\n" + result.stderr).encode("utf-8", errors="replace"))
            raise RuntimeError(f"P7-08 npm combined profile lock resolution failed (diagnostic sha256={digest})")
        raw = lock_path.read_bytes()
        lock = json.loads(raw)
    packages = lock.get("packages", {})
    if lock.get("lockfileVersion") != 3 or not isinstance(packages.get(""), dict):
        raise ValueError("P7-08 combined Koishi lock is not npm lockfile v3")
    for package, version in dependencies.items():
        record = packages.get(f"node_modules/{package}")
        if not isinstance(record, dict) or record.get("version") != version:
            raise ValueError(f"P7-08 combined Koishi lock has the wrong exact package version: {package}")
        if not isinstance(record.get("resolved"), str) or not isinstance(record.get("integrity"), str):
            raise ValueError(f"P7-08 combined Koishi lock omits archive integrity: {package}")
    if (
        packages["node_modules/@koishijs/core"].get("resolved") != base_core.get("resolved")
        or packages["node_modules/@koishijs/core"].get("integrity") != base_core.get("integrity")
    ):
        raise ValueError("P7-08 combined lock changed the exact Koishi core archive from the sample's frozen lock")
    _, artifact_records, _ = _frozen_maps()
    expected_packages = [
        {"registry_entry_id": "ko-49faa76157ad072ec5791025", "package": candidate["package"], "version": candidate["version"], "sha256": candidate["artifact_sha256"]},
        *[{
            "registry_entry_id": provider["registry_entry_id"],
            "package": provider["package"],
            "version": provider["version"],
            "sha256": provider["artifact_sha256"],
        } for provider in profile["providers"]],
    ]
    for provider in expected_packages:
        record = packages[f"node_modules/{provider['package']}"]
        artifact_row = artifact_records[provider["registry_entry_id"]]
        locked_artifact, frozen_digest = _artifact_digest(artifact_row, "all")
        upstream = locked_artifact.get("upstream_digests", [])
        if frozen_digest != provider["sha256"] or not _integrity_matches(record["integrity"], upstream):
            raise ValueError(f"P7-08 npm lock bytes differ from frozen upstream integrity: {provider['package']}")
    raw_digest = _sha256(raw)
    relative = Path("profile-locks/p7-koishi/sha256") / raw_digest[:2] / raw_digest
    destination = ARTIFACT_CACHE / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _sha256_file(destination) != raw_digest:
        raise ValueError("P7-08 content-addressed Koishi lock cache is corrupt")
    if not destination.exists():
        temporary_lock = destination.with_suffix(".tmp")
        temporary_lock.write_bytes(raw)
        os.replace(temporary_lock, destination)
    candidate["profile_lock_path"] = destination
    candidate["profile_lock_sha256"] = raw_digest
    candidate["profile_id"] = profile["profile_id"]
    return {
        "path": str(destination),
        "sha256": raw_digest,
        "size_bytes": len(raw),
        "locked_package_count": max(0, len(packages) - 1),
        "direct_package_count": len(dependencies),
        "lockfile_version": 3,
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def run_admission(
    *,
    output_path: Path = DEFAULT_OUTPUT,
    admission_path: Path = ADMISSION_PATH,
    pilot_admission_path: Path = ADMISSION_PATH,
    pilot_path: Path = PILOT_PATH,
    bundle_root: Path | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    output_path = output_path.expanduser().absolute()
    progress_path = output_path.with_suffix(output_path.suffix + ".progress.json")
    if output_path.exists() or output_path.with_suffix(output_path.suffix + ".sha256").exists() or progress_path.exists():
        raise FileExistsError(f"refusing to overwrite P7-08 report/progress: {output_path}")
    if workers != 1:
        raise ValueError("P7-08 service profile admission uses one Worker at a time")
    admission = _read(admission_path)
    pilot_admission = _read(pilot_admission_path)
    pilot = _read(pilot_path)
    admission_raw = admission_path.read_bytes()
    pilot_admission_raw = pilot_admission_path.read_bytes()
    if not (
        admission.get("status") == "admitted"
        and admission.get("platform") == "linux"
        and admission.get("capabilities", {}).get("claim_allowed") is True
        and admission.get("capabilities", {}).get("network_disabled") is True
        and admission.get("capabilities", {}).get("cleanup_evidence_required") is True
        and admission.get("facts", {}).get("backend") == "docker"
        and admission.get("facts", {}).get("probe", {}).get("status") == "passed"
        and pilot_admission.get("status") == "admitted"
        and pilot_admission.get("platform") == "linux"
        and pilot.get("admission_sha256") == _sha256(pilot_admission_raw)
    ):
        raise RuntimeError("P7-08 current Linux Worker admission or the P7-04 source-admission binding is invalid")
    runtime_image = pilot.get("runtime_image")
    if not isinstance(runtime_image, str) or "@sha256:" not in runtime_image:
        raise ValueError("P7-04 pilot does not name a digest-pinned Linux runtime image")
    bundle_root = (bundle_root or ARTIFACT_CACHE / f"p7-p708-service-bundles-{int(time.time())}").expanduser().absolute()
    try:
        bundle_root.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise ValueError("P7-08 Worker bundles must be outside the repository")
    if bundle_root.exists():
        raise FileExistsError(f"refusing to overwrite P7-08 bundles: {bundle_root}")
    bundle_root.mkdir(parents=True)

    source_bundles = pilot.get("bundle_preparation", [])
    if not source_bundles:
        raise ValueError("P7-04 retry3 report has no source bundle for the pinned Core binary")
    source_core = Path(source_bundles[0]["path"]) / "project/sample/ligamina-cli"
    if not source_core.is_file() or _sha256_file(source_core) != pilot.get("core_cli_sha256"):
        raise ValueError("P7-04 source bundle Core binary is missing or differs from the frozen pilot hash")
    profiles = validate_profile_documents()
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "p7_p708_offline_service_profile_admission",
        "campaign_id": f"p7-p708-{int(time.time())}",
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "running",
        "scope": {
            "admission_path": str(admission_path),
            "admission_sha256": _sha256(admission_raw),
            "pilot_source_admission_path": str(pilot_admission_path),
            "pilot_source_admission_sha256": _sha256(pilot_admission_raw),
            "source_pilot_path": str(pilot_path),
            "source_pilot_sha256": _sha256_file(pilot_path),
            "runtime_image": runtime_image,
            "core_cli_sha256": pilot["core_cli_sha256"],
            "network": "none",
            "bundle_root": str(bundle_root),
            "purpose": "one load-only profile variant per ecosystem; no generated campaign or behavior claims",
        },
        "profiles": [],
        "counts": {"profiles": len(profiles), "admitted": 0, "failed_or_unknown": 0, "worker_runs": 0, "cleanup_confirmed": 0},
    }
    _atomic_json(progress_path, report)
    # Re-probe the exact profile runtime image with the fresh admitted Docker backend
    # before creating any bundle that contains third-party plugin artifacts.
    runtime_probe_root = bundle_root / "_runtime_probe"
    runtime_probe_root.mkdir()
    runtime_probe = p7.phase4_campaign._runtime_probe(
        p7.phase4_campaign.DockerWorker(image_digest=runtime_image, repository_root=ROOT),
        runtime_probe_root,
        run_id=f"{report['campaign_id']}-runtime-probe",
    )
    report["runtime_probe"] = runtime_probe
    if runtime_probe.get("status") != "passed":
        report["status"] = "blocked"
        report["stop_reason"] = "runtime_image_probe_failed"
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        _atomic_json(progress_path, report)
        raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        output_path.with_suffix(output_path.suffix + ".sha256").write_text(
            f"{_sha256(raw)}  {output_path.name}\n", encoding="ascii"
        )
        return report
    for ecosystem in ("koishi", "nonebot"):
        profile = profiles[ecosystem]
        candidate = _candidate_from_frozen(profile, ecosystem=ecosystem)
        base_profile_lock_sha256 = candidate["profile_lock_sha256"]
        lock_summary = None
        if ecosystem == "koishi":
            lock_summary = _resolve_koishi_overlay_lock(candidate, profile)
        effective_profile_lock_sha256 = p7.object_sha256({
            "base_dependency_lock_sha256": candidate["profile_lock_sha256"],
            "service_profile_document_sha256": candidate["service_profile_sha256"],
            "preload_plugins": candidate["preload_plugins"],
        })
        bundle_info = p7.prepare_bundle(candidate, bundle_root=bundle_root, core_binary=source_core)
        bundle = Path(bundle_info["path"])
        p7.validate_bundle(bundle, candidate, bundle_info["manifest_sha256"])
        request = p7._build_request(
            candidate, {"events": []}, bundle_info["runtime_artifacts"], mode="native", seed=p7.FIXED_SEED,
            discover_commands=False, case_id=f"{candidate['registry_entry_id']}-service-profile",
        )
        request["context"]["execution"]["harness_timeout_seconds"] = 30
        request["context"]["execution"]["startup_accounts"] = profile.get("startup_accounts", [])
        result = p7._run_side(
            request, candidate, bundle, runtime_image,
            campaign_id=report["campaign_id"], round_no=1, side="service-profile-native-discovery",
        )
        summary = p7._worker_summary(result)
        expected_services = profile.get("providers", [])
        expected_plugin_names = sorted({candidate["package"], *candidate["preload_plugins"]})
        if ecosystem == "koishi":
            expected = sorted({service for provider in expected_services for service in provider.get("services", [])})
            observed = set(summary.get("runtime_services", []))
            missing = sorted(set(expected) - observed)
        else:
            observed_plugins = set(summary.get("runtime_plugins", []))
            missing = sorted(set(expected_plugin_names) - observed_plugins)
        summary["expected_profile_services"] = sorted({service for provider in expected_services for service in provider.get("services", [])})
        summary["missing_profile_services_or_plugins"] = missing
        terminal = (
            result.get("status") == "completed"
            and result.get("profile_setup", {}).get("status") == "passed"
            and result.get("outer_worker", {}).get("cleanup_confirmed") is True
            and not missing
        )
        row = {
            "profile_id": profile["profile_id"],
            "ecosystem": ecosystem,
            "sample": {
                "registry_entry_id": candidate["registry_entry_id"],
                "package": candidate["package"],
                "version": candidate["version"],
                "artifact_sha256": candidate["artifact_sha256"],
                "previous_skip_evidence": candidate.get("previous_skip_evidence"),
                "source_pilot_profile_lock_sha256": base_profile_lock_sha256,
            },
            "profile_document_sha256": candidate["service_profile_sha256"],
            "base_profile_lock_sha256": base_profile_lock_sha256,
            "dependency_profile_lock_sha256": candidate["profile_lock_sha256"],
            "effective_profile_lock_sha256": effective_profile_lock_sha256,
            "profile_lock": lock_summary or {
                "sha256": candidate["profile_lock_sha256"],
                "path": str(candidate["profile_lock_path"]),
                "provider_wheels_verified_in_lock": expected_plugin_names,
            },
            "providers": profile["providers"],
            "startup_fixture": {
                "accounts": profile.get("startup_accounts", []),
                "event_count": len(request["stimulus"]["events"]),
                "network": "disabled",
                "purpose": (
                    "satisfy the Koishi ready-hook group discovery precondition; no events are dispatched"
                    if ecosystem == "koishi"
                    else "no events or synthetic startup accounts are dispatched"
                ),
            },
            "worker_bundle": {"path": bundle_info["path"], "manifest_sha256": bundle_info["manifest_sha256"], "bundle_bytes": bundle_info["bundle_bytes"]},
            "worker": summary,
            "status": "admitted" if terminal else "failed_or_unknown",
        }
        report["profiles"].append(row)
        report["counts"]["worker_runs"] += 1
        report["counts"]["cleanup_confirmed"] += int(result.get("outer_worker", {}).get("cleanup_confirmed") is True)
        report["counts"]["admitted"] += int(terminal)
        report["counts"]["failed_or_unknown"] += int(not terminal)
        _atomic_json(progress_path, report)
        if result.get("outer_worker", {}).get("cleanup_confirmed") is not True:
            report["stop_reason"] = f"worker_cleanup_unconfirmed:{candidate['registry_entry_id']}"
            break
    report["status"] = "completed" if report["counts"]["admitted"] == len(profiles) else "incomplete"
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(output_path)
    (output_path.with_suffix(output_path.suffix + ".sha256")).write_text(f"{_sha256(raw)}  {output_path.name}\n", encoding="ascii")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--admission", type=Path, default=ADMISSION_PATH)
    parser.add_argument("--pilot-admission", type=Path, default=ADMISSION_PATH)
    parser.add_argument("--pilot-report", type=Path, default=PILOT_PATH)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    options = parser.parse_args(argv)
    try:
        report = run_admission(
            output_path=options.output,
            admission_path=options.admission,
            pilot_admission_path=options.pilot_admission,
            pilot_path=options.pilot_report,
            bundle_root=options.bundle_root,
            workers=options.workers,
        )
    except Exception as error:
        print(f"P7-08 service profile admission failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "counts": report["counts"], "output": str(options.output)}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
