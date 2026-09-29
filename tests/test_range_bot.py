"""Integration tests for the range bot's per-tick reconciliation.

The mark price is stubbed (so the public stats feed is never hit) and orders are
placed against the in-process mock backend, letting us assert the full quoting,
idempotency, requote, risk-cap, and kill-switch behaviour.
"""

from __future__ import annotations

import asyncio
import dataclasses
from decimal import Decimal

import pytest

from mock.mock_server import BOOK
from range_bot import RangeTradingBot
from variational.client import VariationalClient
from variational.errors import KillSwitchError
from variational.models import Side
from variational.risk import RiskEngine


def _patch_mark(monkeypatch, price: str) -> None:
    async def fake_mark(*_args, **_kwargs):
        return Decimal(price)

    monkeypatch.setattr("range_bot.fetch_mark_price", fake_mark)


def _bot(cfg, client) -> RangeTradingBot:
    risk = RiskEngine(cfg.max_position_size, cfg.kill_switch_threshold)
    return RangeTradingBot(cfg, client, risk)


def test_tick_quotes_both_sides(config, monkeypatch):
    _patch_mark(monkeypatch, "8")

    async def body():
        async with VariationalClient(config) as client:
            bot = _bot(config, client)
            await bot._tick()

            orders = await client.get_open_orders()
            assert len(orders) == 2
            assert {o.side for o in orders} == {Side.BUY, Side.SELL}

            bid = next(o for o in orders if o.side == Side.BUY)
            ask = next(o for o in orders if o.side == Side.SELL)
            # Bid below mark, ask above.
            assert bid.limit_price < Decimal("8") < ask.limit_price

    asyncio.run(body())


def test_tick_is_idempotent_when_mark_unchanged(config, monkeypatch):
    _patch_mark(monkeypatch, "8")

    async def body():
        async with VariationalClient(config) as client:
            bot = _bot(config, client)
            await bot._tick()
            before = {o.rfq_id for o in await client.get_open_orders()}

            # Same mark -> same targets -> existing quotes kept, no churn.
            await bot._tick()
            after = await client.get_open_orders()
            assert {o.rfq_id for o in after} == before
            assert len(after) == 2

    asyncio.run(body())


def test_tick_requotes_when_mark_moves(config, monkeypatch):
    _patch_mark(monkeypatch, "8")

    async def body():
        async with VariationalClient(config) as client:
            bot = _bot(config, client)
            await bot._tick()
            before = {o.rfq_id for o in await client.get_open_orders()}

            # Move mark far beyond requote tolerance.
            _patch_mark(monkeypatch, "12")
            await bot._tick()
            after_orders = await client.get_open_orders()
            after = {o.rfq_id for o in after_orders}

            assert len(after_orders) == 2
            assert after.isdisjoint(before)  # old quotes cancelled, new placed

    asyncio.run(body())


def test_risk_cap_blocks_entry(config, monkeypatch):
    _patch_mark(monkeypatch, "8")
    capped = dataclasses.replace(config, max_position_size=Decimal("0"))

    async def body():
        async with VariationalClient(capped) as client:
            bot = _bot(capped, client)
            await bot._tick()
            # Cap of 0 means no side may open.
            assert await client.get_open_orders() == []

    asyncio.run(body())


def test_kill_switch_halts_tick(config, monkeypatch):
    _patch_mark(monkeypatch, "8")
    cfg = dataclasses.replace(config, max_retries=0, kill_switch_threshold=1)
    BOOK.fail_next = 10
    BOOK.fail_status = 500

    async def body():
        async with VariationalClient(cfg) as client:
            bot = _bot(cfg, client)
            with pytest.raises(KillSwitchError):
                await bot._tick()
            assert bot._risk.tripped

    asyncio.run(body())
