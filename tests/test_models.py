"""Model-level tests: the wire contract must serialise money as strings and
parse both envelope shapes the venue uses."""

from __future__ import annotations

from decimal import Decimal

from variational.models import (
    Instrument,
    LimitOrderRequest,
    OpenOrder,
    OrderAck,
    OrderType,
    Position,
    Side,
    parse_open_orders,
    parse_positions,
)


def _instrument() -> Instrument:
    return Instrument(underlying="TRUMP", funding_interval_s=3600, settlement_asset="USDC")


def test_instrument_symbol():
    assert _instrument().symbol == "P-TRUMP-USDC-3600"


def test_limit_order_payload_emits_string_decimals():
    req = LimitOrderRequest(
        instrument=_instrument(),
        side=Side.BUY,
        limit_price=Decimal("8.12345"),
        qty=Decimal("1"),
        slippage_limit=Decimal("0.005"),
    )
    payload = req.to_payload()

    # Money-like fields must be strings, never floats (no precision loss).
    assert payload["limit_price"] == "8.12345"
    assert isinstance(payload["limit_price"], str)
    assert payload["qty"] == "1"
    assert payload["slippage_limit"] == "0.005"
    assert payload["side"] == "buy"
    assert payload["order_type"] == "limit"
    # Nested instrument is serialised as an object with the raw fields.
    assert payload["instrument"]["underlying"] == "TRUMP"


def test_limit_order_payload_drops_unset_slippage():
    req = LimitOrderRequest(
        instrument=_instrument(),
        side=Side.SELL,
        limit_price=Decimal("9.0"),
        qty=Decimal("2"),
    )
    payload = req.to_payload()
    assert "slippage_limit" not in payload  # exclude_none drops it
    assert payload["limit_price"] == "9.0"


def test_limit_price_never_scientific_notation():
    # A tiny price must serialise in fixed-point, not 1E-8.
    req = LimitOrderRequest(
        instrument=_instrument(),
        side=Side.BUY,
        limit_price=Decimal("0.00000001"),
        qty=Decimal("1"),
    )
    assert req.to_payload()["limit_price"] == "0.00000001"


def test_order_ack_requires_rfq_id():
    ack = OrderAck.model_validate({"rfq_id": "abc-123", "status": "pending"})
    assert ack.rfq_id == "abc-123"
    assert ack.status == "pending"


def test_order_ack_missing_rfq_id_is_error():
    import pydantic

    try:
        OrderAck.model_validate({"status": "pending"})
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("OrderAck without rfq_id should fail validation")


def test_parse_open_orders_accepts_envelope_and_bare_list():
    row = {"rfq_id": "r1", "side": "buy", "limit_price": "8.1"}
    from_envelope = parse_open_orders({"result": [row], "pagination": {}})
    from_list = parse_open_orders([row])
    assert len(from_envelope) == len(from_list) == 1
    assert isinstance(from_envelope[0], OpenOrder)
    assert from_envelope[0].limit_price == Decimal("8.1")
    assert from_envelope[0].side == Side.BUY


def test_parse_open_orders_handles_garbage():
    assert parse_open_orders(None) == []
    assert parse_open_orders(42) == []


def test_parse_positions_signed_qty():
    rows = [
        {"instrument": {"underlying": "TRUMP"}, "qty": "3", "side": "sell"},
        {"instrument": {"underlying": "TRUMP"}, "qty": "2", "side": "buy"},
    ]
    positions = parse_positions(rows)
    assert positions[0].signed_qty == Decimal("-3")  # sell is negative
    assert positions[1].signed_qty == Decimal("2")


def test_parse_positions_envelope_keys():
    # Accepts result or positions envelope.
    assert parse_positions({"result": [{"qty": "1"}]})[0].qty == Decimal("1")
    assert parse_positions({"positions": [{"qty": "5"}]})[0].qty == Decimal("5")


def test_open_order_type_enum_roundtrip():
    o = OpenOrder.model_validate({"rfq_id": "x", "order_type": "stop_limit"})
    assert o.order_type == OrderType.STOP_LIMIT
