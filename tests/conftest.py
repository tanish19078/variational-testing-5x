"""Shared pytest fixtures: a live mock backend on a random free port, and a
Config pointed at it.

The mock runs in a background thread in-process, so tests exercise the real
httpx transport, real retry/backoff, and the real client code path end to end -
only the venue is substituted.
"""

from __future__ import annotations

import socket
import threading
from decimal import Decimal

import pytest

from config import Config
from mock.mock_server import BOOK, make_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def mock_backend():
    """Start the mock server on a free port; yield its base URL."""
    port = _free_port()
    server = make_server("127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Reset shared book state between tests.
    BOOK.orders.clear()
    BOOK.positions.clear()
    BOOK.fail_next = 0
    BOOK.fail_status = 500

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def make_config(base_url: str, **overrides) -> Config:
    """A Config pointed at the mock, with fast timeouts for tests."""
    defaults = dict(
        base_url=base_url,
        cookie="",
        connected_address="0x0000000000000000000000000000000000000000",
        timeout_s=5,
        max_retries=3,
        dry_run=False,
        underlying="TRUMP",
        settlement_asset="USDC",
        funding_interval_s=3600,
        range_pct=Decimal("1.5"),
        order_size=Decimal("1"),
        poll_interval_s=1,
        requote_tolerance_pct=Decimal("0.25"),
        max_slippage=Decimal("0.005"),
        max_position_size=Decimal("10"),
        kill_switch_threshold=3,
        stats_url="http://127.0.0.1:1/unused",
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture()
def config(mock_backend):
    return make_config(mock_backend)
