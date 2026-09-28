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

from pydantic import BaseModel, ConfigDict, Field, field_serializer


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
    """An open position as returned by /api/positions."""

    model_config = ConfigDict(extra="allow")

    instrument: Optional[Instrument] = None
    qty: Decimal = Decimal(0)
    side: Optional[Side] = None
    entry_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None

    @property
    def signed_qty(self) -> Decimal:
        """Position size signed by side (sell -> negative)."""
        if self.side == Side.SELL:
            return -abs(self.qty)
        return abs(self.qty)


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
