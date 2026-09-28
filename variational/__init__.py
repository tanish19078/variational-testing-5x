"""Variational Omni range-trading automation suite (testing harness)."""

from .client import VariationalClient
from .errors import (
    KillSwitchError,
    VariationalAPIError,
    VariationalAuthError,
    VariationalError,
    VariationalNetworkError,
    VariationalRateLimitError,
    VariationalServerError,
    VariationalTimeoutError,
)
from .models import (
    Instrument,
    LimitOrderRequest,
    OpenOrder,
    OrderAck,
    OrderType,
    Position,
    Side,
)
from .risk import RiskEngine

__all__ = [
    "VariationalClient",
    "RiskEngine",
    "Instrument",
    "LimitOrderRequest",
    "OpenOrder",
    "OrderAck",
    "OrderType",
    "Position",
    "Side",
    "KillSwitchError",
    "VariationalError",
    "VariationalAPIError",
    "VariationalAuthError",
    "VariationalRateLimitError",
    "VariationalServerError",
    "VariationalTimeoutError",
    "VariationalNetworkError",
]
