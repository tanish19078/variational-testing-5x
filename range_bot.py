"""Range-trading bot.

Strategy per iteration:
    1. Fetch the current mark price (from the public stats feed).
    2. Compute symmetric bounds at +/- RANGE_PCT around mark:
           bid target = mark * (1 - range_pct/100)
           ask target = mark * (1 + range_pct/100)
    3. Fetch resting open orders for the instrument.
    4. Cancel any order that has drifted further than REQUOTE_TOLERANCE_PCT
       from its side's current target (idempotency: in-tolerance orders are
       left in place, so we do not churn cancel/replace every tick).
    5. Place a resting limit order on any side that has no in-tolerance order,
       subject to the risk engine's position cap and kill switch.
    6. Sleep POLL_INTERVAL_S and repeat.

Every network call routes the risk engine's success/failure hooks so the kill
switch observes the true health of the venue.
"""

from __future__ import annotations

import asyncio
import signal
from decimal import Decimal
from typing import Optional

from loguru import logger

from config import Config
from variational.client import VariationalClient
from variational.errors import (
    KillSwitchError,
    VariationalAPIError,
    VariationalError,
)
from variational.models import OpenOrder, Position, Side
from variational.risk import RiskEngine

# Price source: the genuinely public, unauthenticated stats endpoint.
from monitor import fetch_mark_price

_HUNDRED = Decimal(100)


class RangeTradingBot:
    def __init__(
        self,
        config: Config,
        client: VariationalClient,
        risk: RiskEngine,
    ) -> None:
        self._cfg = config
        self._client = client
        self._risk = risk
        self._stop = asyncio.Event()

    # ---- lifecycle --------------------------------------------------------

    def request_stop(self) -> None:
        logger.info("stop requested")
        self._stop.set()

    async def run(self) -> None:
        logger.info(
            "starting range bot | instrument={} range=+/-{}% size={} dry_run={}",
            self._cfg.instrument_id, self._cfg.range_pct,
            self._cfg.order_size, self._cfg.dry_run,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except KillSwitchError:
                    logger.error("kill switch active - halting bot")
                    break
                except VariationalError as e:
                    # Already accounted in risk hooks; log and continue.
                    logger.warning("tick error: {}", e)

                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._cfg.poll_interval_s
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Best-effort cancel-all on the way out."""
        logger.info("shutting down: cancelling open orders")
        try:
            n = await self._client.cancel_all(self._cfg.instrument_id)
            logger.info("cancelled {} order(s)", n)
        except VariationalError as e:
            logger.warning("cancel-all during shutdown failed: {}", e)

    # ---- one iteration ----------------------------------------------------

    async def _tick(self) -> None:
        self._risk.ensure_live()

        mark = await fetch_mark_price(
            self._cfg.stats_url, self._cfg.underlying
        )
        if mark is None:
            logger.warning("no mark price available this tick")
            return

        rng = self._cfg.range_pct / _HUNDRED
        bid_target = (mark * (Decimal(1) - rng)).quantize(Decimal("0.00001"))
        ask_target = (mark * (Decimal(1) + rng)).quantize(Decimal("0.00001"))
        logger.info(
            "mark={} -> bid_target={} ask_target={}", mark, bid_target, ask_target
        )

        net_qty = await self._net_position()

        orders = await self._safe(self._client.get_open_orders(self._cfg.instrument_id))
        if orders is None:
            return

        # Reconcile each side independently.
        await self._reconcile_side(Side.BUY, bid_target, orders, net_qty)
        await self._reconcile_side(Side.SELL, ask_target, orders, net_qty)

    async def _reconcile_side(
        self,
        side: Side,
        target: Decimal,
        orders: list[OpenOrder],
        net_qty: Decimal,
    ) -> None:
        tol = self._cfg.requote_tolerance_pct / _HUNDRED
        same_side = [o for o in orders if o.side == side and o.limit_price is not None]

        in_tolerance = [
            o for o in same_side
            if abs(o.limit_price - target) <= target * tol
        ]
        drifted = [o for o in same_side if o not in in_tolerance]

        # Cancel drifted orders on this side.
        for o in drifted:
            await self._safe(self._client.cancel_order(o.rfq_id))

        # If an in-tolerance order already rests here, we are done (idempotent).
        if in_tolerance:
            logger.debug("{} side already quoted within tolerance", side.value)
            return

        # Otherwise place a fresh order, subject to risk.
        add = self._cfg.order_size if side == Side.BUY else -self._cfg.order_size
        if not self._risk.can_open(net_qty, add):
            logger.info("risk gate: skipping {} entry", side.value)
            return

        await self._safe(
            self._client.place_limit_order(
                side=side,
                limit_price=target,
                qty=self._cfg.order_size,
                slippage_limit=self._cfg.max_slippage,
            )
        )

    async def _net_position(self) -> Decimal:
        positions = await self._safe(self._client.get_positions())
        if not positions:
            return Decimal(0)
        total = Decimal(0)
        for p in positions:  # type: Position
            if p.instrument and p.instrument.symbol != self._cfg.instrument_id:
                continue
            total += p.signed_qty
        return total

    # ---- risk-instrumented call wrapper -----------------------------------

    async def _safe(self, coro):
        """Await a client coroutine, routing risk hooks and swallowing
        already-logged API errors so one bad call does not abort the tick."""
        try:
            result = await coro
        except VariationalError as e:
            self._risk.on_failure(e)
            logger.warning("call failed: {}", e)
            self._risk.ensure_live()  # re-raise as KillSwitchError if tripped
            return None
        else:
            self._risk.on_success()
            return result


async def main(cfg: Optional[Config] = None) -> None:
    """Run the bot. Accepts a Config so the CLI can pass overrides through;
    falls back to the environment when invoked directly."""
    cfg = cfg or Config.from_env()
    risk = RiskEngine(
        cfg.max_position_size,
        cfg.kill_switch_threshold,
        max_notional=cfg.max_notional,
        daily_loss_limit=cfg.daily_loss_limit,
        max_drawdown=cfg.max_drawdown,
    )

    async with VariationalClient(cfg) as client:
        bot = RangeTradingBot(cfg, client, risk)

        loop = asyncio.get_running_loop()
        # Graceful shutdown on Ctrl+C / SIGTERM where supported.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bot.request_stop)
            except NotImplementedError:
                # Windows: add_signal_handler is unsupported; KeyboardInterrupt
                # is handled by the try/except around run() below instead.
                pass

        try:
            await bot.run()
        except KeyboardInterrupt:
            bot.request_stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("interrupted")
