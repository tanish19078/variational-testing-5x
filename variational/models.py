"""Pydantic models mirroring the Variational Omni wire format.

Field names and shapes match what the Omni web client sends and receives:

  * order placement  -> POST /api/orders/new/limit
        { instrument, side, order_type, limit_price, qty,
          slippage_limit?, is_reduce_only?, is_auto_resize?, use_mark_price? }
    response contains `rfq_id`.

  * cancellation     -> POST /api/orders/cancel   { rfq_id }
  * open orders      -> GET  /api/orders/v2?status=pending&instrument=...
  * positions        -> GET  /api/positions

Money-like fields are typed as ``Decimal``. On the wire they are strings (to
preserve precision); the serializers below emit strings and the parsers accept
either strings or numbers.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    # Enum values confirmed from the backend deserializer:
    #   limit | stop_limit | take_profit | stop_loss
    LIMIT = "limit"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


class Instrument(BaseModel):
    """A perpetual future instrument descriptor."""

    model_config = ConfigDict(extra="ignore")

    underlying: str
    funding_interval_s: int = 3600
    settlement_asset: str = "USDC"
    instrument_type: str = "perpetual_future"

    @property
    def symbol(self) -> str:
        return (
            f"P-{self.underlying}-{self.settlement_asset}-{self.funding_interval_s}"
        )


class LimitOrderRequest(BaseModel):
    """Body for POST /api/orders/new/limit.

    Mirrors the object the web client builds. Optional fields are omitted from
    the payload when unset so we send exactly what the UI sends.
    """

    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    side: Side
    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal
    qty: Decimal
    slippage_limit: Optional[Decimal] = None
    is_reduce_only: bool = False
    is_auto_resize: bool = False
    use_mark_price: bool = False

    @field_serializer("limit_price", "qty", "slippage_limit", when_used="json")
    def _dec_to_str(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else format(v, "f")

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready dict with unset optionals dropped."""
        return self.model_dump(mode="json", exclude_none=True)


class OrderAck(BaseModel):
    """Response to a successful order placement.

    The web client asserts ``rfq_id`` is present and treats its absence as a
    hard error, so we require it too.
    """

    model_config = ConfigDict(extra="allow")

    rfq_id: str
    status: Optional[str] = None


class OpenOrder(BaseModel):
    """A resting order as returned by /api/orders/v2."""

    model_config = ConfigDict(extra="allow")

    rfq_id: str
    side: Optional[Side] = None
    order_type: Optional[OrderType] = None
    limit_price: Optional[Decimal] = None
    qty: Optional[Decimal] = None
    status: Optional[str] = None
    instrument: Optional[Instrument] = None


class Position(BaseModel):
    """An open position as returned by /api/positions.

    The live endpoint wraps each row in a ``position_info`` object::

        [{"position_info": {"instrument": {...}, "qty": "0.01", ...}, ...}]

    so the validator below unwraps it, while still accepting a flat row (which
    is what some other responses and our own fixtures use). Without this, every
    position silently parsed as qty=0 - the position cap and reduce-only logic
    would have been reading zeros against the real venue.
    """

    model_config = ConfigDict(extra="allow")

    instrument: Optional[Instrument] = None
    qty: Decimal = Decimal(0)
    side: Optional[Side] = None
    entry_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None

    @model_validator(mode="before")
    @classmethod
    def _unwrap_position_info(cls, data: Any) -> Any:
        """Flatten {position_info: {...}} into the top level, outer keys winning
        only where the inner object does not define them."""
        if not isinstance(data, dict):
            return data
        inner = data.get("position_info")
        if not isinstance(inner, dict):
            return data
        merged = {k: v for k, v in data.items() if k != "position_info"}
        merged.update(inner)
        return merged

    @property
    def signed_qty(self) -> Decimal:
        """Position size, signed so that shorts are negative.

        Two shapes exist in the wild: an explicit ``side`` alongside an absolute
        qty, or a already-signed ``qty`` with no side. Handle both - taking
        abs() unconditionally would turn every short into a long.
        """
        if self.side is None:
            return self.qty
        if self.side == Side.SELL:
            return -abs(self.qty)
        return abs(self.qty)


class QuoteExecutionRequest(BaseModel):
    """Body for POST /api/orders/new/market AND POST /api/quotes/accept.

    Both endpoints take the *same* shape, and neither takes an instrument or a
    qty: you first ask for an indicative quote, then execute against its
    ``quote_id``. That is the part of Omni's model most likely to surprise you
    coming from a normal exchange.

    Note the field is ``max_slippage`` here, not ``slippage_limit`` as on the
    limit-order endpoint. Confirmed against a client that runs live.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: str
    side: Side
    max_slippage: Optional[Decimal] = None
    is_reduce_only: bool = False

    @field_serializer("max_slippage", when_used="json")
    def _dec_to_str(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else format(v, "f")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# The market-order and quote-accept endpoints are payload-identical, so these
# names are aliases that document intent at the call site.
MarketOrderRequest = QuoteExecutionRequest
AcceptQuoteRequest = QuoteExecutionRequest


class TriggerOrderRequest(BaseModel):
    """Body for a conditional order: stop_limit, take_profit, or stop_loss.

    ``trigger_price`` is the level that arms the order; ``limit_price`` is where
    it then rests (omitted for a pure stop_loss/take_profit, which execute at
    market once triggered).

    NOTE: the ``trigger_price`` field name is inferred from the frontend bundle
    rather than confirmed by a live round-trip - see docs/ for the
    proven-vs-assumed scorecard.
    """

    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    side: Side
    order_type: OrderType
    trigger_price: Decimal
    qty: Decimal
    limit_price: Optional[Decimal] = None
    slippage_limit: Optional[Decimal] = None
    is_reduce_only: bool = True  # conditional orders are usually exits
    use_mark_price: bool = True  # trigger against mark by default

    @field_serializer(
        "trigger_price", "limit_price", "qty", "slippage_limit", when_used="json"
    )
    def _dec_to_str(self, v: Optional[Decimal]) -> Optional[str]:
        return None if v is None else format(v, "f")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class IndicativeQuoteRequest(BaseModel):
    """Body for POST /api/quotes/indicative - 'what price would I get?'.

    Deliberately has **no** ``side``: the quote comes back two-sided and you
    choose the direction when you execute against the ``quote_id``. Confirmed
    against a client that runs live.
    """

    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    qty: Decimal

    @field_serializer("qty", when_used="json")
    def _dec_to_str(self, v: Decimal) -> str:
        return format(v, "f")

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class Portfolio(BaseModel):
    """Response from GET /api/portfolio?compute_margin=true."""

    model_config = ConfigDict(extra="allow")

    balance: Optional[Decimal] = None


class Quote(BaseModel):
    """An indicative quote returned by the venue."""

    model_config = ConfigDict(extra="allow")

    quote_id: str
    price: Optional[Decimal] = None
    qty: Optional[Decimal] = None
    side: Optional[Side] = None
    expires_at: Optional[float] = None


class Fill(BaseModel):
    """An execution report. Shape is inferred; extra fields are preserved."""

    model_config = ConfigDict(extra="allow")

    rfq_id: Optional[str] = None
    side: Optional[Side] = None
    qty: Decimal = Decimal(0)
    price: Optional[Decimal] = None
    instrument: Optional[Instrument] = None


def parse_open_orders(payload: Any) -> list[OpenOrder]:
    """Accept either a bare list or the paginated {result: [...]} envelope."""
    if isinstance(payload, dict):
        rows = payload.get("result", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [OpenOrder.model_validate(r) for r in rows]


def parse_positions(payload: Any) -> list[Position]:
    if isinstance(payload, dict):
        rows = payload.get("result", payload.get("positions", []))
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [Position.model_validate(r) for r in rows]
