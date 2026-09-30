"""Risk engine: the gatekeeper every order must pass through.

Responsibilities:
  * Enforce a hard cap on absolute net position size.
  * Track consecutive server/auth failures and trip a kill switch after a
    configurable threshold, latching the bot into a halted state.

The engine holds no network handles; it is a pure decision component so it can
be unit-tested in isolation. The bot wires its ``on_success`` / ``on_failure``
hooks into the client call sites and consults ``can_open`` before entering.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from loguru import logger

from .errors import (
    KillSwitchError,
    VariationalAuthError,
    VariationalError,
    VariationalServerError,
)


class RiskEngine:
    def __init__(
        self,
        max_position_size: Decimal,
        kill_switch_threshold: int = 3,
        *,
        max_notional: Optional[Decimal] = None,
        daily_loss_limit: Optional[Decimal] = None,
        max_drawdown: Optional[Decimal] = None,
    ) -> None:
        self._max_position = abs(Decimal(max_position_size))
        self._threshold = int(kill_switch_threshold)
        self._consecutive_failures = 0
        self._tripped = False

        # Optional limits. None means "not enforced", so a caller that only
        # cares about position size keeps the original behaviour.
        self._max_notional = (
            None if max_notional is None else abs(Decimal(max_notional))
        )
        self._daily_loss_limit = (
            None if daily_loss_limit is None else abs(Decimal(daily_loss_limit))
        )
        self._max_drawdown = (
            None if max_drawdown is None else abs(Decimal(max_drawdown))
        )

        # PnL tracking for the loss/drawdown limits.
        self._realized = Decimal(0)
        self._peak_equity = Decimal(0)


    # ---- state ------------------------------------------------------------

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def max_position_size(self) -> Decimal:
        return self._max_position

    # ---- failure accounting ----------------------------------------------

    def on_success(self) -> None:
        """Reset the consecutive-failure counter after any good response."""
        if self._consecutive_failures:
            logger.debug("risk: failure streak reset after success")
        self._consecutive_failures = 0

    def on_failure(self, exc: BaseException) -> None:
        """Record a failure. Only server (5xx) and auth (401/403) errors count
        toward the kill switch; client-side 4xx (e.g. a rejected price) do not,
        as they indicate a bad request rather than a failing venue."""
        if not isinstance(exc, (VariationalServerError, VariationalAuthError)):
            return
        self._consecutive_failures += 1
        logger.warning(
            "risk: counted failure {}/{} ({})",
            self._consecutive_failures, self._threshold, type(exc).__name__,
        )
        if self._consecutive_failures >= self._threshold and not self._tripped:
            self.trip("kill switch threshold reached")

    def trip(self, reason: str) -> None:
        self._tripped = True
        logger.error("risk: KILL SWITCH TRIPPED - {}", reason)

    def reset(self) -> None:
        """Manually clear a tripped state (operator intervention)."""
        self._tripped = False
        self._consecutive_failures = 0
        logger.info("risk: engine reset")

    # ---- pnl / drawdown accounting ----------------------------------------

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized

    @property
    def drawdown(self) -> Decimal:
        """How far below the session's peak equity we currently sit (>= 0)."""
        return max(Decimal(0), self._peak_equity - self._realized)

    def record_pnl(self, delta: Decimal) -> None:
        """Book a realized PnL change, then enforce loss and drawdown limits.

        Trips the kill switch if either limit is breached, because both mean
        "stop trading now" rather than "retry".
        """
        self._realized += Decimal(delta)
        if self._realized > self._peak_equity:
            self._peak_equity = self._realized

        if (
            self._daily_loss_limit is not None
            and self._realized <= -self._daily_loss_limit
            and not self._tripped
        ):
            self.trip(
                f"daily loss limit: realized {self._realized} "
                f"<= -{self._daily_loss_limit}"
            )
            return

        if (
            self._max_drawdown is not None
            and self.drawdown >= self._max_drawdown
            and not self._tripped
        ):
            self.trip(
                f"max drawdown: {self.drawdown} >= {self._max_drawdown}"
            )

    # ---- gates ------------------------------------------------------------

    def ensure_live(self) -> None:
        """Raise if the kill switch has tripped."""
        if self._tripped:
            raise KillSwitchError("trading halted by kill switch")

    def can_open(
        self,
        current_net_qty: Decimal,
        add_qty: Decimal,
        price: Optional[Decimal] = None,
    ) -> bool:
        """Whether adding ``add_qty`` stays within every configured cap.

        ``price`` is only needed for the notional check; without it that check
        is skipped rather than guessed at.
        """
        if self._tripped:
            return False
        projected = abs(Decimal(current_net_qty) + Decimal(add_qty))
        if projected > self._max_position:
            logger.warning(
                "risk: blocked - projected |net| {} exceeds cap {}",
                projected, self._max_position,
            )
            return False
        if self._max_notional is not None and price is not None:
            notional = projected * abs(Decimal(price))
            if notional > self._max_notional:
                logger.warning(
                    "risk: blocked - projected notional {} exceeds cap {}",
                    notional, self._max_notional,
                )
                return False
        return True

    def check_open(
        self,
        current_net_qty: Decimal,
        add_qty: Decimal,
        price: Optional[Decimal] = None,
    ) -> None:
        """Raising variant of :meth:`can_open`."""
        self.ensure_live()
        if not self.can_open(current_net_qty, add_qty, price):
            raise VariationalError(
                f"risk gate rejected {current_net_qty} + {add_qty} "
                f"(cap {self._max_position}, notional cap {self._max_notional})"
            )
