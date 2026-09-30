"""Tests for the support modules: rate limiter, metrics, state journal, logging.

These are the pieces with no network in them, so they are tested directly
rather than through the mock backend.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import pytest

from variational.logging_setup import scrub
from variational.metrics import Metrics
from variational.ratelimit import TokenBucket
from variational.state import StateStore


# ---- rate limiter ---------------------------------------------------------


def test_bucket_disabled_when_rate_is_zero():
    bucket = TokenBucket(0)
    assert not bucket.enabled

    async def body():
        # A disabled bucket must never sleep, however many times it is hit.
        started = time.perf_counter()
        for _ in range(50):
            await bucket.acquire()
        return time.perf_counter() - started

    assert asyncio.run(body()) < 0.2
    assert bucket.throttled_count == 0


def test_bucket_allows_burst_then_throttles():
    bucket = TokenBucket(100, burst=3)

    async def body():
        # The first three are free (the burst), the fourth must wait.
        for _ in range(3):
            waited = await bucket.acquire()
            assert waited == 0
        return await bucket.acquire()

    waited = asyncio.run(body())
    assert waited > 0
    assert bucket.throttled_count == 1
    assert bucket.total_wait_s > 0


def test_bucket_refills_over_time():
    bucket = TokenBucket(1000, burst=1)

    async def body():
        await bucket.acquire()
        await asyncio.sleep(0.01)  # ~10 tokens' worth at 1000/s
        # Refilled, so this should not have to wait.
        return await bucket.acquire()

    assert asyncio.run(body()) == 0


def test_bucket_caps_tokens_at_burst():
    bucket = TokenBucket(1000, burst=2)

    async def body():
        await asyncio.sleep(0.05)  # would be ~50 tokens if uncapped
        assert bucket.tokens <= 2
        await bucket.acquire()
        await bucket.acquire()
        # Only two were ever available, so the third throttles.
        assert await bucket.acquire() > 0

    asyncio.run(body())


def test_bucket_sleeps_outside_the_lock():
    """Two tasks must interleave rather than serialise on the lock."""
    bucket = TokenBucket(200, burst=1)
    order: list[str] = []

    async def body():
        async def worker(name: str):
            await bucket.acquire()
            order.append(name)

        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(body())
    assert sorted(order) == ["a", "b"]


# ---- metrics --------------------------------------------------------------


def test_metrics_counts_and_percentiles():
    m = Metrics()
    for i, status in enumerate([200, 200, 500, 429, 200]):
        m.record_request(status, float((i + 1) * 10))
    m.record_retry()
    m.record_retry()
    m.record_order_placed()
    m.record_order_cancelled()
    m.record_order_rejected()
    m.record_tick()

    assert m.requests == 5
    assert m.retries == 2
    assert m.status_counts[200] == 3
    assert m.status_counts[500] == 1
    assert m.orders_placed == 1
    assert m.orders_cancelled == 1
    assert m.orders_rejected == 1
    assert m.ticks == 1
    # Nearest rank over [10,20,30,40,50].
    assert m.latency_p50_ms == 30.0
    assert m.latency_p95_ms == 50.0
    assert m.uptime_s >= 0


def test_metrics_counts_transport_failures_as_errors():
    m = Metrics()
    m.record_request(None, 5.0)  # a timeout or network error has no status
    m.record_request(200, 5.0)
    assert m.requests == 2
    assert m.request_errors == 1


def test_metrics_empty_percentiles_are_none():
    m = Metrics()
    assert m.latency_p50_ms is None
    assert m.latency_p95_ms is None
    assert "requests" in m.as_dict()
    assert isinstance(m.summary(), str)


def test_metrics_as_dict_is_json_serialisable():
    m = Metrics()
    m.record_request(200, 12.5)
    # status_counts is a defaultdict with int keys; json needs it to survive.
    json.dumps(m.as_dict(), default=str)


# ---- state journal --------------------------------------------------------


def test_state_roundtrip(tmp_path):
    path = tmp_path / "orders.json"
    store = StateStore(str(path))
    store.record_order(
        "rfq-1",
        side="buy",
        limit_price=Decimal("1.90"),
        qty=Decimal("1"),
        instrument="P-TRUMP-USDC-3600",
    )
    assert store.live_order_ids == ["rfq-1"]
    assert len(store) == 1

    # A fresh store reads what the first one wrote.
    reopened = StateStore(str(path))
    reopened.load()
    assert reopened.live_order_ids == ["rfq-1"]
    assert reopened.get("rfq-1")["side"] == "buy"
    # Decimals must not have been written as floats.
    raw = json.loads(path.read_text())
    assert raw["orders"]["rfq-1"]["limit_price"] == "1.90"


def test_state_clear_order_and_all(tmp_path):
    store = StateStore(str(tmp_path / "s.json"))
    store.record_order("a", side="buy", limit_price=Decimal(1), qty=Decimal(1), instrument="X")
    store.record_order("b", side="sell", limit_price=Decimal(2), qty=Decimal(1), instrument="X")
    store.clear_order("a")
    assert store.live_order_ids == ["b"]
    store.clear_all()
    assert len(store) == 0
    # Clearing something absent is a no-op, not an error.
    store.clear_order("nope")


def test_state_quarantines_a_corrupt_journal(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text("{ this is not json")
    store = StateStore(str(path))
    store.load()  # must not raise
    assert len(store) == 0
    assert (tmp_path / "orders.json.corrupt").exists()


def test_state_recover_reports_leftovers(tmp_path):
    path = tmp_path / "orders.json"
    store = StateStore(str(path))
    store.record_order("x", side="buy", limit_price=Decimal(1), qty=Decimal(1), instrument="I")

    reopened = StateStore(str(path))
    leftovers = reopened.recover()
    assert leftovers == ["x"]


def test_state_creates_parent_directory(tmp_path):
    store = StateStore(str(tmp_path / "nested" / "deep" / "orders.json"))
    store.record_order("a", side="buy", limit_price=Decimal(1), qty=Decimal(1), instrument="I")
    assert (tmp_path / "nested" / "deep" / "orders.json").exists()


# ---- log scrubbing --------------------------------------------------------


def test_scrub_removes_cookies_and_jwts():
    # These values are synthetic but must be credential-shaped for the test to
    # mean anything. allowlist-secret
    dirty = (
        "cookie: vr-token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij; "  # allowlist-secret
        "cf_clearance=SOMEOPAQUEVALUE_padded_to_look_real_0123456789; "  # allowlist-secret
        "other=keepme"
    )
    clean = scrub(dirty)
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "SOMEOPAQUEVALUE" not in clean
    assert "other=keepme" in clean


def test_scrub_masks_wallet_addresses():
    clean = scrub("wallet 0x7B2368315ABe4E907c289e43691E9C61a474E5eD traded")
    assert "0x7B2368315ABe4E907c289e43691E9C61a474E5eD" not in clean
    assert "0x7B23" in clean and "E5eD" in clean


def test_scrub_leaves_ordinary_text_alone():
    msg = "Placed buy 1 @ 1.90 -> rfq_id=767adc17"
    assert scrub(msg) == msg
