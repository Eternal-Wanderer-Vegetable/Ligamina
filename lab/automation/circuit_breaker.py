"""Small deterministic backend circuit breaker for automation dispatch."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Callable


@dataclass(frozen=True)
class CircuitPolicy:
    consecutive_failures: int = 3
    window_size: int = 20
    window_failures: int = 4
    cooldown_seconds: float = 60
    half_open_passes: int = 3
    failed_recovery_windows_before_exit: int = 3


class CircuitOpen(RuntimeError):
    pass


class BackendCircuitBreaker:
    """Stop dispatch on backend failure; half-open admits only a caller-designated sentinel."""

    def __init__(self, policy: CircuitPolicy = CircuitPolicy(), *, clock: Callable[[], float]):
        self.policy = policy
        self.clock = clock
        self._lock = threading.RLock()
        self._outcomes: deque[bool] = deque(maxlen=policy.window_size)
        self._consecutive = 0
        self._state = "closed"
        self._opened_at: float | None = None
        self._half_open_passes = 0
        self._failed_windows = 0

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open" and self._opened_at is not None:
                if self.clock() - self._opened_at >= self.policy.cooldown_seconds:
                    self._state = "half_open"
                    self._half_open_passes = 0
            return self._state

    @property
    def exit_required(self) -> bool:
        with self._lock:
            return self._failed_windows >= self.policy.failed_recovery_windows_before_exit

    def allow_dispatch(self, *, sentinel: bool = False) -> bool:
        with self._lock:
            state = self.state
            return state == "closed" or (state == "half_open" and sentinel)

    def require_dispatch(self, *, sentinel: bool = False) -> None:
        if not self.allow_dispatch(sentinel=sentinel):
            raise CircuitOpen("backend circuit is open; dispatch is stopped")

    def record(self, *, healthy: bool, reason_code: str | None = None) -> None:
        # API/daemon loss and unconfirmed cleanup are immediate-open conditions.
        with self._lock:
            immediate = reason_code in {"worker_backend_unavailable", "docker_daemon_unavailable", "worker_cleanup_unconfirmed"}
            self._outcomes.append(bool(healthy))
            self._consecutive = 0 if healthy else self._consecutive + 1
            if self._state == "half_open":
                if healthy:
                    self._half_open_passes += 1
                    if self._half_open_passes >= self.policy.half_open_passes:
                        self._state = "closed"
                        self._consecutive = 0
                        self._outcomes.clear()
                else:
                    self._failed_windows += 1
                    self._open()
                return
            if immediate or self._consecutive >= self.policy.consecutive_failures or sum(not item for item in self._outcomes) >= self.policy.window_failures:
                self._open()

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = self.clock()
        self._half_open_passes = 0
