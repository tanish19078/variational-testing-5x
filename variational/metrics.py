"""Session metrics.

A trading process that cannot tell you what it did is hard to trust. This module
keeps cheap in-process counters and latency stats so the bot can print an honest
summary on shutdown and tests can assert on behaviour rather than log text.

Deliberately dependency-free: no Prometheus, no background threads. If you later
want to export these, ``as_dict()`` is the seam.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Metrics:
    """Counters for one bot session."""

    started_at: float = field(default_factory=time.time)

    requests: int = 0
    request_errors: int = 0
    retries: int = 0
    status_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    orders_placed: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    ticks: int = 0

    # Latency of successful requests, in milliseconds.
    _latencies_ms: list[float] = field(default_factory=list)

    # ---- recording ---------------------------------------------------------

    def record_request(self, status: Optional[int], elapsed_ms: float) -> None:
        """Record one completed attempt.

        ``status`` is None when the request never produced a response at all
        (timeout or transport error). Those count as errors too: otherwise a
        run where every call timed out would report "0 errors".
        """
        self.requests += 1
        if status is None:
            self.request_errors += 1
        else:
            self.status_counts[status] += 1
            if status >= 400:
                self.request_errors += 1
        self._latencies_ms.append(elapsed_ms)

    def record_retry(self) -> None:
        self.retries += 1

    def record_order_placed(self) -> None:
        self.orders_placed += 1

    def record_order_cancelled(self) -> None:
        self.orders_cancelled += 1

    def record_order_rejected(self) -> None:
        self.orders_rejected += 1

    def record_tick(self) -> None:
        self.ticks += 1

    # ---- derived -----------------------------------------------------------

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

    @property
    def latency_p50_ms(self) -> Optional[float]:
        return self._percentile(50)

    @property
    def latency_p95_ms(self) -> Optional[float]:
        return self._percentile(95)

    def _percentile(self, pct: int) -> Optional[float]:
        if not self._latencies_ms:
            return None
        ordered = sorted(self._latencies_ms)
        # Nearest-rank: ceil(pct/100 * N), 1-indexed. Uses ceil rather than
        # round() because round() is banker's rounding in Python, so an exact
        # .5 rank (p50 of 5 samples) would round down to the wrong element.
        rank = math.ceil(pct / 100 * len(ordered))
        idx = max(0, min(len(ordered) - 1, rank - 1))
        return round(ordered[idx], 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uptime_s": round(self.uptime_s, 1),
            "ticks": self.ticks,
            "requests": self.requests,
            "request_errors": self.request_errors,
            "retries": self.retries,
            "orders_placed": self.orders_placed,
            "orders_cancelled": self.orders_cancelled,
            "orders_rejected": self.orders_rejected,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "status_counts": dict(self.status_counts),
        }

    def summary(self) -> str:
        """A one-screen human-readable report."""
        d = self.as_dict()
        lines = [
            "session summary",
            f"  uptime            {d['uptime_s']}s over {d['ticks']} tick(s)",
            f"  requests          {d['requests']} "
            f"({d['request_errors']} error, {d['retries']} retried)",
            f"  orders            {d['orders_placed']} placed, "
            f"{d['orders_cancelled']} cancelled, {d['orders_rejected']} rejected",
        ]
        if d["latency_p50_ms"] is not None:
            lines.append(
                f"  latency           p50 {d['latency_p50_ms']}ms / "
                f"p95 {d['latency_p95_ms']}ms"
            )
        if d["status_counts"]:
            codes = ", ".join(
                f"{k}x{v}" for k, v in sorted(d["status_counts"].items())
            )
            lines.append(f"  statuses          {codes}")
        return "\n".join(lines)
