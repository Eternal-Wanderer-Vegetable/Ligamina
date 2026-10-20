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


__all__ = ["Dispatcher", "CircuitOpen"]
