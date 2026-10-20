from __future__ import annotations

import json
import argparse
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_ROOT))

from lab.workers import DockerWorker, WorkerLimits, WorkerSpec  # noqa: E402

IMAGE_LOCK = REPOSITORY_ROOT / "deploy" / "lab" / "linux" / "worker-image.lock.json"
REPORT_PATH = REPOSITORY_ROOT / "outputs" / "acceptance" / "phase2-workers-linux-20261012.json"


class CapturingDockerWorker(DockerWorker):
    worker_id: str | None = None

    def command(self, spec: WorkerSpec, *, worker_id: str) -> list[str]:
        self.worker_id = worker_id
        return super().command(spec, worker_id=worker_id)


def run_result(name: str, worker: DockerWorker, spec: WorkerSpec, expected_status: str, expected_reason: str | None) -> dict[str, Any]:
    result = worker.run(spec)
    record = {
        "name": name,
        "limits": {
            "timeout_seconds": spec.limits.timeout_seconds,
            "memory_mb": spec.limits.memory_mb,
            "cpu_cores": spec.limits.cpu_cores,
            "process_limit": spec.limits.process_limit,
            "output_limit_bytes": spec.limits.output_limit_bytes,
        },
        "status": result.status,
        "reason_code": result.reason_code,
        "exit_code": result.exit_code,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": result.stderr.decode("utf-8", errors="replace"),
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "output_truncated": result.output_truncated,
        "cleanup_confirmed": result.cleanup_confirmed,
        "worker_id": result.worker_id,
        "expected": {"status": expected_status, "reason_code": expected_reason},
    }
    record["passed"] = (
        result.status == expected_status
        and result.reason_code == expected_reason
        and result.cleanup_confirmed
    )
    return record


def docker_server_version() -> str:
    completed = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}} {{.OSType}} {{.Architecture}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Docker server unavailable")
    return completed.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run benign Linux Docker Worker admission probes")
    parser.add_argument("--report", type=Path, default=REPORT_PATH, help="write evidence to this report path")
    options = parser.parse_args(argv)
    image_lock = json.loads(IMAGE_LOCK.read_text(encoding="utf-8"))
    worker = DockerWorker(image_digest=image_lock["image"], repository_root=REPOSITORY_ROOT)
    records: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ligamina-linux-probes-") as temporary_directory:
        root = Path(temporary_directory)
        artifact_dir = root / "artifact"
        artifact_dir.mkdir()
        (artifact_dir / "readonly.txt").write_text("fixture-read-only\n", encoding="utf-8")
        previous_secret = os.environ.get("LIGAMINA_HOST_SECRET_TEST")
        os.environ["LIGAMINA_HOST_SECRET_TEST"] = "must-not-enter-container"
        try:
            boundary_code = (
                "import os,socket; from pathlib import Path; "
                "assert Path('/input/readonly.txt').read_text() == 'fixture-read-only\\n'; "
                "assert not Path('/repo').exists() and not Path('/mnt/e/stella/Ligamina').exists(); "
                "assert Path.home() == Path('/work') and os.access('/work',os.W_OK); "
                "assert os.environ.get('LIGAMINA_HOST_SECRET_TEST') is None; "
                "exec(\"try: open('/input/readonly.txt','w').write('tamper')\\n"
                "except OSError: pass\\nelse: raise AssertionError('input is writable')\"); "
                "exec(\"try: open('/etc/ligamina-worker-probe','w').write('tamper')\\n"
                "except OSError: pass\\nelse: raise AssertionError('image root is writable')\"); "
                "exec(\"try: socket.create_connection(('1.1.1.1',443),timeout=2)\\n"
                "except OSError: pass\\nelse: raise AssertionError('external network is reachable')\"); "
                "print('linux-worker-boundaries-passed')"
            )
            records.append(
                run_result(
                    "filesystem-network-and-secret-boundaries",
                    worker,
                    WorkerSpec("s2-04-linux-boundary", "linux-boundary", ("python", "-c", boundary_code), artifact_dir),
                    "passed",
                    None,
                )
            )
        finally:
            if previous_secret is None:
                os.environ.pop("LIGAMINA_HOST_SECRET_TEST", None)
            else:
                os.environ["LIGAMINA_HOST_SECRET_TEST"] = previous_secret

        first_sibling_code = "from pathlib import Path; Path('/work/sibling-marker').write_text('from-first-worker')"
        second_sibling_code = (
            "from pathlib import Path; assert not Path('/work/sibling-marker').exists(); "
            "print('sibling-isolated')"
        )
        records.append(
            run_result(
                "sibling-one-write",
                worker,
                WorkerSpec("s2-04-sibling-one", "linux-sibling-one", ("python", "-c", first_sibling_code), artifact_dir),
                "passed",
                None,
            )
        )
        records.append(
            run_result(
                "sibling-two-state-isolated",
                worker,
                WorkerSpec("s2-04-sibling-two", "linux-sibling-two", ("python", "-c", second_sibling_code), artifact_dir),
                "passed",
                None,
            )
        )
        records.append(
            run_result(
                "nonzero-exit",
                worker,
                WorkerSpec("s2-04-nonzero", "linux-nonzero", ("python", "-c", "raise SystemExit(17)"), artifact_dir),
                "failed",
                "worker_nonzero_exit",
            )
        )
        resource_code = "\n".join(
            (
                "import errno, subprocess",
                "from pathlib import Path",
                "memory_limit = int(Path('/sys/fs/cgroup/memory.max').read_text())",
                "quota, period = map(int, Path('/sys/fs/cgroup/cpu.max').read_text().split())",
                "pids_limit = int(Path('/sys/fs/cgroup/pids.max').read_text())",
                "assert memory_limit == 256 * 1024 * 1024, memory_limit",
                "assert 0 < quota <= period, (quota, period)",
                "assert pids_limit == 8, pids_limit",
                "children = []",
                "try:",
                "    for _ in range(100):",
                "        try:",
                "            children.append(subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))",
                "        except OSError as error:",
                "            assert error.errno == errno.EAGAIN, error",
                "            break",
                "    else:",
                "        raise AssertionError('pids limit did not reject excess children')",
                "    assert 1 <= len(children) <= 6, len(children)",
                "finally:",
                "    for child in children:",
                "        child.terminate()",
                "    for child in children:",
                "        child.wait(timeout=3)",
                "print(f'resource-limits-passed:memory={memory_limit}:cpu={quota}/{period}:pids={pids_limit}:children={len(children)}')",
            )
        )
        records.append(
            run_result(
                "cgroup-memory-cpu-and-pid-limits",
                worker,
                WorkerSpec(
                    "s2-04-resources",
                    "linux-resources",
                    ("python", "-c", resource_code),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=30, memory_mb=256, cpu_cores=1, process_limit=8),
                ),
                "passed",
                None,
            )
        )
        records.append(
            run_result(
                "timeout",
                worker,
                WorkerSpec(
                    "s2-04-timeout",
                    "linux-timeout",
                    ("python", "-c", "import time; time.sleep(30)"),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=3, memory_mb=256, cpu_cores=1, process_limit=32),
                ),
                "unknown",
                "worker_timeout",
            )
        )
        overflow_code = "import sys; [(sys.stdout.write('x'*4096+'\\n'),sys.stdout.flush()) for _ in iter(int,1)]"
        records.append(
            run_result(
                "output-overflow",
                worker,
                WorkerSpec(
                    "s2-04-overflow",
                    "linux-overflow",
                    ("python", "-c", overflow_code),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=30, memory_mb=256, cpu_cores=1, process_limit=32, output_limit_bytes=1024),
                ),
                "unknown",
                "worker_output_truncated",
            )
        )
        memory_code = "import time; memory=bytearray(512*1024*1024); time.sleep(2)"
        records.append(
            run_result(
                "memory-limit",
                worker,
                WorkerSpec(
                    "s2-04-memory",
                    "linux-memory",
                    ("python", "-c", memory_code),
                    artifact_dir,
                    WorkerLimits(timeout_seconds=30, memory_mb=256, cpu_cores=1, process_limit=32),
                ),
                "unknown",
                "worker_resource_limit",
            )
        )
        tree_code = "import subprocess; child=subprocess.Popen(['sleep','30']); print(f'child={child.pid}')"
        records.append(
            run_result(
                "process-tree-cleanup",
                worker,
                WorkerSpec("s2-04-process-tree", "linux-process-tree", ("python", "-c", tree_code), artifact_dir),
                "passed",
                None,
            )
        )

        lost_worker = CapturingDockerWorker(image_digest=image_lock["image"], repository_root=REPOSITORY_ROOT)
        long_spec = WorkerSpec(
            "s2-04-worker-loss",
            "linux-worker-loss",
            ("python", "-c", "import time; time.sleep(60)"),
            artifact_dir,
            WorkerLimits(timeout_seconds=90, memory_mb=256, cpu_cores=1, process_limit=32),
        )
        lost_results: list[dict[str, Any]] = []
        thread = threading.Thread(target=lambda: lost_results.append(run_result("worker-loss", lost_worker, long_spec, "unknown", "worker_lost")), daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while lost_worker.worker_id is None and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if lost_worker.worker_id is None:
            raise RuntimeError("Docker worker did not expose its container identity")
        inspect_deadline = time.monotonic() + 20
        while time.monotonic() < inspect_deadline:
            inspect = subprocess.run(
                ["docker", "inspect", lost_worker.worker_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if inspect.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Docker worker container did not become inspectable")
        killed = subprocess.run(
            ["docker", "kill", lost_worker.worker_id],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if killed.returncode != 0:
            raise RuntimeError(killed.stderr.strip() or "failed to simulate worker loss")
        thread.join(timeout=30)
        if thread.is_alive() or not lost_results:
            raise RuntimeError("Docker worker did not return after forced loss")
        records.append(lost_results[0])

    report = {
        "schema_version": 1,
        "date": "2026-10-12",
        "backend": "Docker Engine",
        "host_platform": platform.platform(),
        "docker_server": docker_server_version(),
        "worker_image": image_lock["image"],
        "worker_image_lock": str(IMAGE_LOCK.relative_to(REPOSITORY_ROOT)),
        "results": records,
        "passed": all(record["passed"] for record in records),
    }
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(options.report), "passed": report["passed"], "results": records}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
