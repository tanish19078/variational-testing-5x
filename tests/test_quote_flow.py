"""Integration tests for the endpoints discovered from a live-tested client.

These go over real HTTP against the in-process mock, which emits the live
shapes -- in particular positions nested under ``position_info``. That nesting
is the point: if the parser regresses, these fail, whereas a fixture shaped to
match the parser would keep passing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from mock.mock_server import BOOK
from variational.client import VariationalClient
from variational.errors import VariationalAPIError
from variational.models import OrderType, Side


def run(coro_fn):
    return asyncio.run(coro_fn())


# ---- portfolio ------------------------------------------------------------


def test_get_portfolio_and_balance(config):
    async def body():
        async with VariationalClient(config) as c:
            portfolio = await c.get_portfolio()
            balance = await c.get_balance()
            return portfolio, balance

    portfolio, balance = run(body)
    assert balance == Decimal("10000")
    assert portfolio.balance == Decimal("10000")
    # compute_margin must actually be sent as a query param.
    assert portfolio.model_extra.get("margin_computed") is True


# ---- quote flow -----------------------------------------------------------


def test_indicative_quote_carries_no_side(config):
    """The request body must not contain `side` -- the quote is two-sided."""
    async def body():
        async with VariationalClient(config) as c:
            return await c.request_indicative_quote(Decimal("2"))

    quote = run(body)
    assert quote.quote_id
    assert quote.price == Decimal("100")
    assert quote.qty == Decimal("2")
    # The mock stores exactly what it was sent; side was never part of it.
    assert quote.side is None


def test_market_enter_opens_a_position(config):
    async def body():
        async with VariationalClient(config) as c:
            ack = await c.market_enter(Side.BUY, Decimal("3"))
            positions = await c.get_positions()
            return ack, positions

    ack, positions = run(body)
    assert ack.rfq_id
    assert len(positions) == 1
    # Parsed out of the nested position_info wrapper.
    assert positions[0].qty == Decimal("3")
    assert positions[0].signed_qty == Decimal("3")
    assert positions[0].instrument.symbol == "P-TRUMP-USDC-3600"
    assert positions[0].entry_price == Decimal("100")


def test_market_sell_produces_a_negative_signed_qty(config):
    """The live shape signs qty and omits side; abs() would flip the short."""
    async def body():
        async with VariationalClient(config) as c:
            await c.market_enter(Side.SELL, Decimal("2"))
            return await c.get_positions()

    positions = run(body)
    assert len(positions) == 1
    assert positions[0].side is None
    assert positions[0].signed_qty == Decimal("-2")


def test_quote_is_single_use(config):
    async def body():
        async with VariationalClient(config) as c:
            quote = await c.request_indicative_quote(Decimal("1"))
            await c.place_market_order(quote.quote_id, Side.BUY)
            # Executing the same quote twice must be refused.
            with pytest.raises(VariationalAPIError) as excinfo:
                await c.place_market_order(quote.quote_id, Side.BUY)
            return excinfo.value

    err = run(body)
    assert "quote not found" in str(err).lower()


def test_accept_quote_closes_via_reduce_only(config):
    """The close path the live reference client uses: flip side, reduce only."""
    async def body():
        async with VariationalClient(config) as c:
            await c.market_enter(Side.BUY, Decimal("5"))
            quote = await c.request_indicative_quote(Decimal("5"))
            await c.accept_quote(quote.quote_id, Side.SELL, is_reduce_only=True)
            return await c.get_positions()

    assert run(body) == []


def test_reduce_only_never_flips_through_zero(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.market_enter(Side.BUY, Decimal("2"))
            # Ask to sell more than we hold, reduce-only.
            quote = await c.request_indicative_quote(Decimal("10"))
            await c.accept_quote(quote.quote_id, Side.SELL, is_reduce_only=True)
            return await c.get_positions()

    # Flattened to zero, not reversed into a short.
    assert run(body) == []


def test_adding_to_a_position_averages_the_entry(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.market_enter(Side.BUY, Decimal("1"))  # at mark 100
            BOOK.mark = Decimal("200")
            await c.market_enter(Side.BUY, Decimal("1"))  # at mark 200
            return await c.get_positions()

    positions = run(body)
    assert positions[0].qty == Decimal("2")
    assert positions[0].entry_price == Decimal("150")


def test_max_slippage_defaults_from_config(config):
    """The field is max_slippage on quote endpoints, not slippage_limit."""
    from variational.models import MarketOrderRequest

    req = MarketOrderRequest(
        quote_id="q", side=Side.BUY, max_slippage=Decimal("0.005")
    )
    payload = req.to_payload()
    assert payload["max_slippage"] == "0.005"
    assert "slippage_limit" not in payload
    assert payload["is_reduce_only"] is False


# ---- resting-order fills --------------------------------------------------


def test_resting_buy_fills_when_mark_crosses_down(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.place_limit_order(Side.BUY, Decimal("95"), Decimal("1"))
            assert len(await c.get_open_orders()) == 1
            BOOK.mark = Decimal("94")
            BOOK.fill_crossing_orders()
            return await c.get_open_orders(), await c.get_positions()

    orders, positions = run(body)
    assert orders == []
    assert positions[0].qty == Decimal("1")
    assert positions[0].entry_price == Decimal("95")  # filled at the limit


def test_resting_buy_does_not_fill_above_its_limit(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.place_limit_order(Side.BUY, Decimal("95"), Decimal("1"))
            BOOK.mark = Decimal("96")
            BOOK.fill_crossing_orders()
            return await c.get_open_orders(), await c.get_positions()

    orders, positions = run(body)
    assert len(orders) == 1
    assert positions == []


# ---- trigger orders -------------------------------------------------------


def test_trigger_order_rejects_plain_limit(config):
    async def body():
        async with VariationalClient(config) as c:
            with pytest.raises(ValueError):
                await c.place_trigger_order(
                    Side.SELL, OrderType.LIMIT, Decimal("90"), Decimal("1")
                )

    run(body)


def test_trigger_order_sends_trigger_price(config):
    async def body():
        async with VariationalClient(config) as c:
            ack = await c.place_trigger_order(
                Side.SELL, OrderType.STOP_LOSS, Decimal("90"), Decimal("1")
            )
            return ack

    ack = run(body)
    assert ack.rfq_id
    stored = BOOK.orders[ack.rfq_id]
    assert stored["trigger_price"] == "90"
    assert stored["order_type"] == "stop_loss"


# ---- close_all ------------------------------------------------------------


def test_close_all_flattens_orders_and_positions(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.market_enter(Side.BUY, Decimal("1"))
            await c.place_limit_order(Side.SELL, Decimal("110"), Decimal("1"))
            await c.close_all()
            return await c.get_open_orders(), await c.get_positions()

    orders, positions = run(body)
    assert orders == []
    assert positions == []


# ---- dry run --------------------------------------------------------------


def test_dry_run_touches_nothing(mock_backend):
    from tests.conftest import make_config

    cfg = make_config(mock_backend, dry_run=True)

    async def body():
        async with VariationalClient(cfg) as c:
            await c.place_market_order("fake-quote", Side.BUY)
            await c.accept_quote("fake-quote", Side.SELL)
            await c.place_trigger_order(
                Side.SELL, OrderType.STOP_LOSS, Decimal("90"), Decimal("1")
            )
            await c.close_all()
            return await c.get_positions()

    # None of those reached the book, despite referencing a quote that
    # does not exist -- a live call would have errored.
    assert run(body) == []
    assert BOOK.orders == {}


# ---- metrics plumbing -----------------------------------------------------


def test_metrics_observe_the_real_request_path(config):
    async def body():
        async with VariationalClient(config) as c:
            await c.get_positions()
            await c.market_enter(Side.BUY, Decimal("1"))
            return c.metrics

    m = run(body)
    # positions + indicative + market = 3
    assert m.requests == 3
    assert m.status_counts[200] == 3
    assert m.orders_placed == 1
    assert m.latency_p50_ms is not None
