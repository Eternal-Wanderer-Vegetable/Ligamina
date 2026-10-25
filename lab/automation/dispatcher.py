"""Fenced single-host dispatcher with lease heartbeat and backend circuit admission."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from lab.queue import QueueError, StageQueue

from .circuit_breaker import BackendCircuitBreaker, CircuitOpen


class Dispatcher:
    def __init__(
        self,
        queue: StageQueue,
        run_id: str,
        worker_id: str,
        execute: Callable[[dict[str, Any], threading.Event], dict[str, Any]],
        breaker: BackendCircuitBreaker,
        *,
        heartbeat_seconds: float = 20,
        lease_seconds: float = 120,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if heartbeat_seconds <= 0 or lease_seconds <= heartbeat_seconds:
            raise ValueError("lease must exceed the positive heartbeat interval")
        self.queue = queue
        self.run_id = run_id
        self.worker_id = worker_id
        self.execute = execute
        self.breaker = breaker
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.sleeper = sleeper

    def dispatch_one(self, *, sentinel: bool = False) -> dict[str, Any] | None:
        self.breaker.require_dispatch(sentinel=sentinel)
        job = self.queue.claim(self.run_id, self.worker_id, lease_seconds=self.lease_seconds, now=self.clock())
        if job is None:
            return None
        lost = threading.Event()
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(job, lost, stop_heartbeat),
            name=f"lease-{job['attempt_id'][:8]}", daemon=True,
        )
        heartbeat.start()
        result: dict[str, Any]
        try:
            result = self.execute(job, lost)
            if not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "skipped", "unknown"}:
                raise ValueError("executor must return a terminal stage result")
        except BaseException as error:
            result = {"status": "unknown", "reason_code": "dispatcher_executor_error", "reason": type(error).__name__}
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1, self.heartbeat_seconds + 1))
        if lost.is_set():
            self.breaker.record(healthy=False, reason_code="lease_lost")
            return {"job_id": job["job_id"], "attempt_id": job["attempt_id"], "status": "unknown", "reason_code": "lease_lost", "committed": False}
        committed = self.queue.finish(
            job["job_id"], self.worker_id, result, attempt_id=job["attempt_id"],
            retryable=bool(result.get("retryable", False)), now=self.clock(),
        )
        reason = result.get("reason_code")
        backend_healthy = not (
            result["status"] == "unknown"
            and reason in {
                "worker_backend_unavailable", "docker_daemon_unavailable", "worker_cleanup_unconfirmed",
                "worker_launch_failed", "worker_lost", "worker_resource_limit",
            }
        )
        if result.get("cleanup_confirmed") is False:
            backend_healthy = False
            reason = "worker_cleanup_unconfirmed"
        self.breaker.record(healthy=backend_healthy, reason_code=reason)
        return {**committed, "committed": True}

    def _heartbeat_loop(self, job: dict[str, Any], lost: threading.Event, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                self.queue.heartbeat(
                    job["job_id"], self.worker_id, attempt_id=job["attempt_id"],
                    lease_seconds=self.lease_seconds, now=self.clock(),
                )
            except QueueError:
                lost.set()
                return


def dispatch_until_idle(
    queue: StageQueue,
    run_id: str,
    worker_id_prefix: str,
    execute: Callable[[dict[str, Any], threading.Event], dict[str, Any]],
    breaker: BackendCircuitBreaker,
    *,
    worker_count: int = 1,
    heartbeat_seconds: float = 20,
    lease_seconds: float = 120,
) -> bool:
    """Drain a run with a bounded worker pool; return whether infrastructure stopped it."""
    if worker_count < 1 or worker_count > 2:
        raise ValueError("worker_count must be between 1 and 2")
    if not worker_id_prefix:
        raise ValueError("worker_id_prefix must not be empty")

    stop = threading.Event()
    stop_lock = threading.Lock()
    errors_lock = threading.Lock()
    infrastructure_stop = False
    errors: list[BaseException] = []

    def request_stop(*, infrastructure: bool = False, interrupt: bool = False) -> None:
        nonlocal infrastructure_stop
        with stop_lock:
            infrastructure_stop = infrastructure_stop or infrastructure
            if stop.is_set():
                return
            stop.set()
            if interrupt and queue.run(run_id)["state"] == "running":
                queue.interrupt_run(run_id)

    def worker(index: int) -> None:
        try:
            dispatcher = Dispatcher(
                queue, run_id, f"{worker_id_prefix}-{index}", execute, breaker,
                heartbeat_seconds=heartbeat_seconds, lease_seconds=lease_seconds,
            )
            while not stop.is_set():
                try:
                    outcome = dispatcher.dispatch_one()
                except CircuitOpen:
                    request_stop(infrastructure=True, interrupt=True)
                    return
                except QueueError:
                    if stop.is_set() or queue.run(run_id)["state"] != "running":
                        request_stop()
                        return
                    raise
                if outcome is None:
                    return
                if not breaker.allow_dispatch():
                    request_stop(infrastructure=True, interrupt=True)
                    return
        except BaseException as error:
            with errors_lock:
                errors.append(error)
            request_stop(interrupt=True)

    threads = [
        threading.Thread(target=worker, args=(index,), name=f"dispatcher-{index}")
        for index in range(1, worker_count + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return infrastructure_stop


__all__ = ["Dispatcher", "CircuitOpen", "dispatch_until_idle"]
