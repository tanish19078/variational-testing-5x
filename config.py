"""Typed configuration loaded from the environment / .env.

All numeric money-like values are kept as ``Decimal`` from the moment they
leave the environment, so no float ever touches price or size arithmetic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _dec(key: str, default: str) -> Decimal:
    raw = os.getenv(key, default)
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be a decimal, got {raw!r}") from exc


def _opt_dec(key: str) -> Optional[Decimal]:
    """A Decimal that stays None when the variable is unset or blank, so
    'limit not configured' is distinguishable from 'limit is zero'."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{key} must be a decimal, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of runtime configuration."""

    # Connection
    base_url: str
    cookie: str
    connected_address: str
    timeout_s: int
    max_retries: int

    # Safety
    dry_run: bool

    # Instrument
    underlying: str
    settlement_asset: str
    funding_interval_s: int

    # Strategy
    range_pct: Decimal
    order_size: Decimal
    poll_interval_s: int
    requote_tolerance_pct: Decimal
    max_slippage: Decimal

    # Risk
    max_position_size: Decimal
    kill_switch_threshold: int

    # Monitor
    stats_url: str

    # ---- optional extras -------------------------------------------------
    # These carry defaults so existing callers keep working. Dataclass rules
    # require defaulted fields last, which is why they live down here.

    # Proactive client-side rate limit. 0 disables it.
    rate_limit_per_s: float = 0.0
    rate_limit_burst: int = 0

    # Where the live-order journal lives (crash recovery).
    state_file: str = "state/orders.json"

    # Optional rotating log file. Empty means console only.
    log_file: str = ""
    log_level: str = "INFO"

    # Extra risk limits. None means not enforced.
    max_notional: Optional[Decimal] = None
    daily_loss_limit: Optional[Decimal] = None
    max_drawdown: Optional[Decimal] = None

    @property
    def instrument_id(self) -> str:
        """Canonical instrument identifier, e.g. ``P-TRUMP-USDC-3600``."""
        return (
            f"P-{self.underlying}-{self.settlement_asset}-{self.funding_interval_s}"
        )

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_url=_str("VR_BASE_URL", "http://127.0.0.1:8787").rstrip("/"),
            cookie=_str("VR_COOKIE", ""),
            connected_address=_str("VR_CONNECTED_ADDRESS", ""),
            timeout_s=_int("TIMEOUT_S", 8),
            max_retries=_int("MAX_RETRIES", 3),
            dry_run=_bool("DRY_RUN", True),
            underlying=_str("UNDERLYING", "TRUMP"),
            settlement_asset=_str("SETTLEMENT_ASSET", "USDC"),
            funding_interval_s=_int("FUNDING_INTERVAL_S", 3600),
            range_pct=_dec("RANGE_PCT", "1.5"),
            order_size=_dec("ORDER_SIZE", "1"),
            poll_interval_s=_int("POLL_INTERVAL_S", 10),
            requote_tolerance_pct=_dec("REQUOTE_TOLERANCE_PCT", "0.25"),
            max_slippage=_dec("MAX_SLIPPAGE", "0.005"),
            max_position_size=_dec("MAX_POSITION_SIZE", "10"),
            kill_switch_threshold=_int("KILL_SWITCH_THRESHOLD", 3),
            stats_url=_str(
                "STATS_URL",
                "https://omni-client-api.prod.ap-northeast-1.variational.io"
                "/metadata/stats",
            ),
            rate_limit_per_s=float(_dec("RATE_LIMIT_PER_S", "0")),
            rate_limit_burst=_int("RATE_LIMIT_BURST", 0),
            state_file=_str("STATE_FILE", "state/orders.json"),
            log_file=_str("LOG_FILE", ""),
            log_level=_str("LOG_LEVEL", "INFO"),
            max_notional=_opt_dec("MAX_NOTIONAL"),
            daily_loss_limit=_opt_dec("DAILY_LOSS_LIMIT"),
            max_drawdown=_opt_dec("MAX_DRAWDOWN"),
        )

