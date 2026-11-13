"""Run one Native or Federated side in an admitted, network-disabled Docker Worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.workers import DockerWorker, WorkerLimits, WorkerSpec
from lab.automation.p7_context_profiles import profile_manifest_sha256
from lab.automation.p7_koishi_context_profiles import profile_manifest_sha256 as koishi_context_profile_manifest_sha256
from lab.automation.p7_loopback_services import validate_bound_profile
from lab.automation.p7_bilibili_image_diagnostic import validate_bound_profile as validate_bilibili_fixture_profile
from lab.automation.p7_hjm_rcon_fixture import profile_manifest_sha256 as hjm_rcon_profile_manifest_sha256


def _worker_host_environment(
    context: dict[str, Any], stimulus: dict[str, Any] | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LIGAMINA_CORE_EXECUTABLE": "/usr/local/bin/ligamina-cli",
        "LIGAMINA_NODE_EXECUTABLE": "/usr/local/bin/node",
        "LIGAMINA_PYTHON_EXECUTABLE": "/usr/local/bin/python",
        "LIGAMINA_PROFILE_SITE_PACKAGES": "/work/profile/site-packages",
        "PYTHONPATH": "/input/project/hosts/nonebot:/work/profile/site-packages",
    }
    supplied = context.get("host_environment", {})
    profile = context.get("test_context_profile")
    if not isinstance(supplied, dict):
        raise ValueError("P7 Worker host environment must be an object")
    if profile is None:
        if "SUPERUSERS" in supplied:
            raise ValueError("P7 SUPERUSERS requires a hash-bound synthetic context profile")
        return environment
    if not isinstance(profile, dict):
        raise ValueError("P7 test context profile must be an object")
    payload = {key: value for key, value in profile.items() if key != "profile_sha256"}
    claimed = profile.get("profile_sha256")
    observed = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(claimed, str)
        or claimed != observed
        or profile.get("execution_scope") != "linux_worker_only_network_disabled"
        or profile.get("authority_scope") != "synthetic_test_actor_only"
    ):
        raise ValueError("P7 Worker context profile hash or scope is invalid")
    sample = context.get("sample", {})
    if (
        profile.get("registry_entry_id") != str(sample.get("sample_id", "")).removeprefix("p7-")
        or profile.get("package") != sample.get("package")
        or profile.get("version") != sample.get("version")
        or profile.get("artifact_sha256") != sample.get("_artifact_hash")
    ):
        raise ValueError("P7 Worker context profile does not match the installed sample artifact")
    if sample.get("ecosystem") == "nonebot":
        if (
            profile.get("manifest_sha256") != profile_manifest_sha256()
            or profile.get("event_envelope_role_claims") is not False
        ):
            raise ValueError("P7 NoneBot context profile hash or EventEnvelope scope is invalid")
        superusers = [profile["synthetic_superuser_id"]]
        expected = {"SUPERUSERS": json.dumps(superusers, separators=(",", ":"))}
        if supplied != expected or context.get("config", {}).get("nonebot_config") != {"superusers": superusers}:
            raise ValueError("P7 Native and Federated superuser settings do not match")
        environment.update(expected)
        return environment
    if sample.get("ecosystem") == "koishi":
        actor_id = profile.get("synthetic_actor_id")
        expected_config = profile.get("plugin_config")
        expected_manifest_sha256 = (
            hjm_rcon_profile_manifest_sha256()
            if profile.get("profile_id") == "p7-hjm-rcon-v1"
            else koishi_context_profile_manifest_sha256()
        )
        if (
            profile.get("manifest_sha256") != expected_manifest_sha256
            or not isinstance(actor_id, str)
            or not isinstance(expected_config, dict)
            or supplied
            or context.get("config", {}).get("plugin_config") != expected_config
            or sample.get("fixture", {}).get("plugin_config") != expected_config
            or (
                "synthetic_authority" in profile
                and (
                    not isinstance(profile.get("synthetic_authority"), int)
                    or isinstance(profile.get("synthetic_authority"), bool)
                    or not 0 <= profile["synthetic_authority"] <= 5
                )
            )
            or (stimulus is not None and any(
                event.get("actor_id") != actor_id
                for event in stimulus.get("events", [])
                if isinstance(event, dict)
            ))
        ):
            raise ValueError("P7 Koishi Worker config or event actor differs from its source-bound profile")
        return environment
    raise ValueError("P7 context profile is attached to an unsupported ecosystem")


def _docker_side(request: dict[str, Any], options: argparse.Namespace) -> dict[str, Any]:
    context = request.get("context")
    if not isinstance(context, dict):
        return {"status": "failed", "diagnostic": "side request has no context"}
    for key in ("oracle", "assertions"):
        if key in context:
            return {"status": "failed", "diagnostic": "behavior Oracle material is forbidden in the plugin Worker"}
    context["repo_root"] = "/input/project"
    context["work_dir"] = "/work/profile"
    context["site_packages"] = "/work/profile/site-packages"
    context["node_executable"] = "/usr/local/bin/node"
    context["fingerprint_paths"] = {
        "artifact": "/input/project/sample/artifact.bin",
        "lockfile": (
            "/input/project/sample/requirements.txt"
            if context.get("sample", {}).get("ecosystem") == "nonebot"
            else "/input/project/sample/original-package-lock.json"
            if (options.bundle / "project/sample/original-package-lock.json").is_file()
            else "/input/project/sample/package-lock.json"
        ),
        "profile": "/input/project/" + str(context.get("profile_document") or (
            "samples/profiles/nonebot-group-v0.1.json"
            if context.get("sample", {}).get("ecosystem") == "nonebot"
            else "samples/profiles/koishi-group-v0.1.json"
        )),
    }
    context["host_environment"] = _worker_host_environment(context, request.get("stimulus"))
    loopback_service_profile = context.get("loopback_service_profile")
    bilibili_fixture_profile = context.get("bilibili_https_fixture_profile")
    loopback_host_aliases: tuple[str, ...] = ()
    if loopback_service_profile is not None:
        loopback_service_profile = validate_bound_profile(loopback_service_profile, context.get("sample"))
        if context.get("sample", {}).get("ecosystem") != "nonebot":
            raise ValueError("P7 loopback service profiles are restricted to their bound NoneBot artifact")
        loopback_host_aliases = (loopback_service_profile["fixture_host"],)
        context["loopback_service_profile"] = loopback_service_profile
    if bilibili_fixture_profile is not None:
        bilibili_fixture_profile = validate_bilibili_fixture_profile(bilibili_fixture_profile, context.get("sample"))
        if context.get("sample", {}).get("ecosystem") != "nonebot":
            raise ValueError("Bilibili HTTPS fixture profiles are restricted to their bound NoneBot artifact")
        loopback_host_aliases += tuple(bilibili_fixture_profile["host_aliases"])
        context["bilibili_https_fixture_profile"] = bilibili_fixture_profile
        context["host_environment"]["SSL_CERT_FILE"] = "/input/project/" + bilibili_fixture_profile["tls_certificate_path"]
    request["context"] = context
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    worker = DockerWorker(image_digest=options.image, repository_root=ROOT)
    spec = WorkerSpec(
        run_id=options.run_id,
        job_id=f"{options.entry_id}:{request.get('case_id', 'unknown')}:{options.attempt_id}:{options.side}",
        entry_id=options.entry_id,
        attempt_id=f"{options.attempt_id}-{options.side}",
        generation=options.generation,
        command=(
            "/usr/local/bin/python",
            "/input/project/lab/automation/phase4_worker_side.py",
            "--request-stdin",
        ),
        input_dir=options.bundle,
        limits=WorkerLimits(timeout_seconds=240, memory_mb=4096, cpu_cores=2.0, process_limit=256, output_limit_bytes=4 * 1024 * 1024),
        entrypoint="/usr/bin/env",
        stdin_data=encoded.encode("utf-8"),
        loopback_host_aliases=loopback_host_aliases,
    )
    outcome = worker.run(spec)
    result: dict[str, Any]
    try:
        value = json.loads(outcome.stdout.decode("utf-8"))
        result = value if isinstance(value, dict) else {"status": "failed", "diagnostic": "Worker stdout is not a JSON object"}
    except (UnicodeDecodeError, json.JSONDecodeError):
        result = {"status": "failed", "diagnostic": "Worker stdout is not valid JSON"}
    if outcome.status != "passed":
        result["status"] = "failed"
        result["worker_failure"] = {
            "status": outcome.status,
            "reason_code": outcome.reason_code,
            "exit_code": outcome.exit_code,
            "stdout_sha256": outcome.stdout_sha256,
            "stderr_sha256": outcome.stderr_sha256,
            "output_truncated": outcome.output_truncated,
        }
    cleanup = result.get("cleanup")
    if isinstance(cleanup, dict):
        cleanup["worker_cleanup_confirmed"] = outcome.cleanup_confirmed
    else:
        result["cleanup"] = {
            "active_capability_count": None,
            "live_processes": None,
            "worker_cleanup_confirmed": outcome.cleanup_confirmed,
        }
    result["outer_worker"] = {
        "worker_id": outcome.worker_id,
        "status": outcome.status,
        "reason_code": outcome.reason_code,
        "exit_code": outcome.exit_code,
        "elapsed_seconds": round(outcome.elapsed_seconds, 3),
        "cleanup_confirmed": outcome.cleanup_confirmed,
        "peak_memory_bytes": outcome.peak_memory_bytes,
        "stdout_sha256": outcome.stdout_sha256,
        "stderr_sha256": outcome.stderr_sha256,
        "output_truncated": outcome.output_truncated,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--entry-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--side", choices=("native", "federated", "tool"), required=True)
    options = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        result = _docker_side(request, options)
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except Exception as error:
        sys.stdout.write(json.dumps({"status": "failed", "diagnostic": str(error), "cleanup": {"worker_cleanup_confirmed": False}}, ensure_ascii=False) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
