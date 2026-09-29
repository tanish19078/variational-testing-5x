"""End-to-end client tests against the in-process mock backend.

These exercise the real httpx transport, real retry/backoff, and the real error
mapping - only the venue is the local mock. Async bodies are driven via
``asyncio.run`` so no pytest-asyncio dependency is required.
"""

from __future__ import annotations

import asyncio
import dataclasses
from decimal import Decimal

import pytest

from mock.mock_server import BOOK
from variational.client import VariationalClient
from variational.errors import (
    VariationalAPIError,
    VariationalAuthError,
    VariationalRateLimitError,
    VariationalServerError,
)
from variational.models import Side


def test_place_then_cancel_lifecycle(config):
    async def body():
        async with VariationalClient(config) as client:
            ack = await client.place_limit_order(Side.BUY, Decimal("8.4"), Decimal("2"))
            assert ack.rfq_id
            assert not ack.rfq_id.startswith("dry-run")

            orders = await client.get_open_orders()
            assert len(orders) == 1
            assert orders[0].rfq_id == ack.rfq_id
            assert orders[0].side == Side.BUY
            assert orders[0].limit_price == Decimal("8.4")

            assert await client.cancel_order(ack.rfq_id) is True
            assert await client.get_open_orders() == []

    asyncio.run(body())


def test_dry_run_suppresses_network_mutation(config):
    dry = dataclasses.replace(config, dry_run=True)

    async def body():
        async with VariationalClient(dry) as client:
            ack = await client.place_limit_order(Side.BUY, Decimal("8.4"), Decimal("1"))
            assert ack.rfq_id.startswith("dry-run")
            # Book is untouched: the order never left the process.
            assert await client.get_open_orders() == []
            # Cancel is likewise a no-op that still reports success.
            assert await client.cancel_order("whatever") is True

    asyncio.run(body())


def test_positions_parsed_from_backend(config):
    BOOK.positions.append(
        {
            "instrument": {
                "underlying": "TRUMP",
                "settlement_asset": "USDC",
                "funding_interval_s": 3600,
            },
            "qty": "5",
            "side": "sell",
        }
    )

    async def body():
        async with VariationalClient(config) as client:
            positions = await client.get_positions()
            assert len(positions) == 1
            assert positions[0].signed_qty == Decimal("-5")

    asyncio.run(body())


def test_cancel_all_clears_book(config):
    async def body():
        async with VariationalClient(config) as client:
            await client.place_limit_order(Side.BUY, Decimal("8.4"), Decimal("1"))
            await client.place_limit_order(Side.SELL, Decimal("8.6"), Decimal("1"))
            assert len(await client.get_open_orders()) == 2

            n = await client.cancel_all()
            assert n == 2
            assert await client.get_open_orders() == []

    asyncio.run(body())


def test_cancel_unknown_rfq_raises_api_error(config):
    async def body():
        async with VariationalClient(config) as client:
            with pytest.raises(VariationalAPIError) as excinfo:
                await client.cancel_order("does-not-exist")
            assert excinfo.value.status_code == 400

    asyncio.run(body())


def test_missing_field_raises_api_error(config):
    # Sending an order with an uninterpretable instrument trips the deserializer.
    async def body():
        async with VariationalClient(config) as client:
            # Directly hit the request path with a body missing 'side'.
            with pytest.raises(VariationalAPIError) as excinfo:
                await client._request(
                    "POST",
                    "/api/orders/new/limit",
                    json={"instrument": {"underlying": "TRUMP"}, "limit_price": "1"},
                )
            assert excinfo.value.status_code == 400
            assert "missing field" in excinfo.value.message

    asyncio.run(body())


def test_retry_then_success_on_503(config):
    # One injected 503, then normal service: the client must retry and succeed.
    BOOK.fail_next = 1
    BOOK.fail_status = 503

    async def body():
        async with VariationalClient(config) as client:
            ack = await client.place_limit_order(Side.BUY, Decimal("8.4"), Decimal("1"))
            assert ack.rfq_id

    asyncio.run(body())
    assert BOOK.fail_next == 0  # the fault was consumed by the retry


def test_5xx_exhaustion_raises_server_error(config):
    no_retry = dataclasses.replace(config, max_retries=0)
    BOOK.fail_next = 5
    BOOK.fail_status = 500

    async def body():
        async with VariationalClient(no_retry) as client:
            with pytest.raises(VariationalServerError):
                await client.get_positions()

    asyncio.run(body())


def test_401_maps_to_auth_error(config):
    no_retry = dataclasses.replace(config, max_retries=0)
    BOOK.fail_next = 1
    BOOK.fail_status = 401

    async def body():
        async with VariationalClient(no_retry) as client:
            with pytest.raises(VariationalAuthError):
                await client.get_positions()

    asyncio.run(body())


def test_429_maps_to_rate_limit_error(config):
    no_retry = dataclasses.replace(config, max_retries=0)
    BOOK.fail_next = 1
    BOOK.fail_status = 429

    async def body():
        async with VariationalClient(no_retry) as client:
            with pytest.raises(VariationalRateLimitError):
                await client.get_positions()

    asyncio.run(body())
