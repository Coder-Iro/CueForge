"""Shared provider rate limiting for parallel workers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

Sleeper = Callable[[float], None]
Clock = Callable[[], float]


@dataclass
class ProviderRateLimiter:
    last_request_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, interval_seconds: float, *, sleeper: Sleeper = time.sleep, clock: Clock = time.monotonic) -> None:
        if interval_seconds <= 0:
            return
        with self.lock:
            now = clock()
            wait_seconds = interval_seconds - (now - self.last_request_at)
            if wait_seconds > 0:
                sleeper(wait_seconds)
            self.last_request_at = clock()


_LIMITERS: dict[str, ProviderRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def global_rate_limiter(name: str) -> ProviderRateLimiter:
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(name)
        if limiter is None:
            limiter = ProviderRateLimiter()
            _LIMITERS[name] = limiter
        return limiter
