"""Client-side rate limiting.

Reacting to HTTP 429 is necessary but not sufficient: by the time the venue
rate-limits you, you have already been noisy, and on some venues that counts
against you. A token bucket keeps us *proactively* under a chosen request rate.

The bucket refills continuously rather than in fixed windows, so a burst is
allowed up to ``burst`` tokens and the sustained rate settles at ``rate_per_s``.

Timing uses ``time.perf_counter`` rather than ``time.monotonic``. Both are
monotonic, but on Windows ``monotonic`` is GetTickCount64 with ~15.6ms
granularity -- measured, not assumed -- which would make a bucket meant to
smooth request pacing refill in visible 15.6ms lurches. ``perf_counter`` is
sub-microsecond on every platform Python supports.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Optional


class TokenBucket:
    """An asyncio-safe token bucket.

    ``rate_per_s <= 0`` disables limiting entirely, so callers can turn the
    feature off via config without branching.
    """

    def __init__(self, rate_per_s: float, burst: Optional[int] = None) -> None:
        self._rate = float(rate_per_s)
        self._capacity = float(burst if burst is not None else max(1.0, rate_per_s))
        self._tokens = self._capacity
        self._updated = time.perf_counter()
        self._lock = asyncio.Lock()
        # Observability: how long we have spent waiting on our own limiter.
        self.total_wait_s = 0.0
        self.throttled_count = 0

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    @property
    def tokens(self) -> float:
        """Current token estimate (refilled to now). For tests and logging."""
        return min(self._capacity, self._tokens + self._elapsed_refill())

    def _elapsed_refill(self) -> float:
        return (time.perf_counter() - self._updated) * self._rate

    async def acquire(self, cost: float = 1.0) -> float:
        """Block until ``cost`` tokens are available. Returns seconds waited."""
        if not self.enabled:
            return 0.0

        waited = 0.0
        while True:
            async with self._lock:
                now = time.perf_counter()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    self.total_wait_s += waited
                    if waited > 0:
                        self.throttled_count += 1
                    return waited
                deficit = cost - self._tokens
                delay = deficit / self._rate

            # Sleep outside the lock so other tasks can make progress.
            await asyncio.sleep(delay)
            waited += delay

    async def __aenter__(self) -> "TokenBucket":
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        return None
