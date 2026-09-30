"""Async client for the Variational Omni order API.

Transport is ``httpx.AsyncClient`` with connection pooling. The single
``_request`` method centralises headers, timeouts, Pydantic validation,
exponential backoff for 429/5xx, and mapping onto the typed error hierarchy.

Endpoints (verified against the Omni web client's own route table):
    GET  /api/positions
    GET  /api/orders/v2?status=pending&instrument=<id>
    POST /api/orders/new/limit      -> { rfq_id }
    POST /api/orders/cancel         { rfq_id }
"""

from __future__ import annotations

import asyncio
import random
import time
from decimal import Decimal
from types import TracebackType
from typing import Any, Optional

import httpx
from loguru import logger

from config import Config
from .errors import (
    VariationalAPIError,
    VariationalAuthError,
    VariationalNetworkError,
    VariationalRateLimitError,
    VariationalServerError,
    VariationalTimeoutError,
)
from .models import (
    AcceptQuoteRequest,
    IndicativeQuoteRequest,
    Instrument,
    LimitOrderRequest,
    MarketOrderRequest,
    OpenOrder,
    OrderAck,
    OrderType,
    Portfolio,
    Position,
    Quote,
    Side,
    TriggerOrderRequest,
    parse_open_orders,
    parse_positions,
)
from .metrics import Metrics
from .ratelimit import TokenBucket

# Status codes worth retrying with backoff.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class VariationalClient:
    """Async API client. Use as an async context manager."""

    def __init__(
        self,
        config: Config,
        *,
        metrics: Optional[Metrics] = None,
        rate_limiter: Optional[TokenBucket] = None,
    ) -> None:
        self._cfg = config
        self._client: Optional[httpx.AsyncClient] = None
        self.metrics = metrics if metrics is not None else Metrics()
        if rate_limiter is not None:
            self._limiter = rate_limiter
        else:
            self._limiter = TokenBucket(
                config.rate_limit_per_s,
                config.rate_limit_burst or None,
            )


    # ---- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "VariationalClient":
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://omni.variational.io",
            "referer": "https://omni.variational.io/",
        }
        if self._cfg.connected_address:
            headers["vr-connected-address"] = self._cfg.connected_address
        if self._cfg.cookie:
            headers["cookie"] = self._cfg.cookie

        self._client = httpx.AsyncClient(
            base_url=self._cfg.base_url,
            headers=headers,
            timeout=httpx.Timeout(self._cfg.timeout_s),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- core request path -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Perform one API call with retries, returning parsed JSON.

        Raises a subclass of ``VariationalError`` on failure.
        """
        if self._client is None:
            raise RuntimeError("Client not started; use 'async with VariationalClient'")

        attempt = 0
        while True:
            attempt += 1
            # Proactive rate limiting: stay under our own budget rather than
            # discovering the venue's the hard way (429).
            await self._limiter.acquire()
            started = time.perf_counter()
            try:
                resp = await self._client.request(
                    method, path, params=params, json=json
                )
            except httpx.TimeoutException as e:
                self.metrics.record_request(
                    None, (time.perf_counter() - started) * 1000
                )
                if attempt <= self._cfg.max_retries:
                    self.metrics.record_retry()
                    await self._sleep_backoff(attempt)
                    continue
                raise VariationalTimeoutError(f"timeout after {attempt} attempts") from e
            except httpx.HTTPError as e:
                self.metrics.record_request(
                    None, (time.perf_counter() - started) * 1000
                )
                if attempt <= self._cfg.max_retries:
                    self.metrics.record_retry()
                    await self._sleep_backoff(attempt)
                    continue
                raise VariationalNetworkError(str(e)) from e

            self.metrics.record_request(
                resp.status_code, (time.perf_counter() - started) * 1000
            )

            if resp.status_code < 400:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text

            # Error path.
            should_retry = (
                resp.status_code in _RETRY_STATUS and attempt <= self._cfg.max_retries
            )
            if should_retry:
                retry_after = self._parse_retry_after(resp)
                self.metrics.record_retry()
                logger.warning(
                    "HTTP {} on {} {} (attempt {}/{}), retrying",
                    resp.status_code, method, path, attempt, self._cfg.max_retries,
                )
                await self._sleep_backoff(attempt, retry_after)
                continue

            self._raise_for_status(resp)

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
        raw = resp.headers.get("retry-after") or resp.headers.get(
            "x-rate-limit-resets-in-ms"
        )
        if raw is None:
            return None
        try:
            val = float(raw)
        except ValueError:
            return None
        # x-rate-limit-resets-in-ms is milliseconds; retry-after is seconds.
        if "x-rate-limit-resets-in-ms" in resp.headers:
            return val / 1000.0
        return val

    async def _sleep_backoff(
        self, attempt: int, retry_after: Optional[float] = None
    ) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            # Exponential backoff with full jitter, capped.
            delay = min(2.0 ** (attempt - 1), 10.0)
            delay = random.uniform(0, delay)
        await asyncio.sleep(delay)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        body = resp.text or ""
        message = body[:300]
        try:
            data = resp.json()
            if isinstance(data, dict) and "error_message" in data:
                message = data["error_message"]
        except ValueError:
            pass

        code = resp.status_code
        if code in (401, 403):
            raise VariationalAuthError(code, message, body)
        if code == 429:
            raise VariationalRateLimitError(
                code, message, body, VariationalClient._parse_retry_after(resp)
            )
        if code >= 500:
            raise VariationalServerError(code, message, body)
        raise VariationalAPIError(code, message, body)

    # ---- public API --------------------------------------------------------

    def _instrument(self) -> Instrument:
        return Instrument(
            underlying=self._cfg.underlying,
            funding_interval_s=self._cfg.funding_interval_s,
            settlement_asset=self._cfg.settlement_asset,
        )

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", "/api/positions")
        return parse_positions(data)

    async def get_open_orders(
        self, instrument: Optional[str] = None
    ) -> list[OpenOrder]:
        params: dict[str, Any] = {"status": "pending"}
        params["instrument"] = instrument or self._cfg.instrument_id
        data = await self._request("GET", "/api/orders/v2", params=params)
        return parse_open_orders(data)

    async def place_limit_order(
        self,
        side: Side,
        limit_price: Decimal,
        qty: Decimal,
        *,
        is_reduce_only: bool = False,
        slippage_limit: Optional[Decimal] = None,
    ) -> OrderAck:
        """Place a limit order. Honours DRY_RUN.

        In dry-run mode no network call is made; a synthetic ack is returned so
        callers can be exercised end to end without mutating any book.
        """
        req = LimitOrderRequest(
            instrument=self._instrument(),
            side=side,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            qty=qty,
            is_reduce_only=is_reduce_only,
            slippage_limit=slippage_limit,
        )
        payload = req.to_payload()

        if self._cfg.dry_run:
            logger.info(
                "[DRY_RUN] would POST /api/orders/new/limit {}", payload
            )
            return OrderAck(rfq_id="dry-run-000000000000", status="dry_run")

        data = await self._request("POST", "/api/orders/new/limit", json=payload)
        ack = OrderAck.model_validate(data)
        self.metrics.record_order_placed()
        logger.info("Placed {} {} @ {} -> rfq_id={}", side.value, qty, limit_price, ack.rfq_id)
        return ack

    async def cancel_order(self, rfq_id: str) -> bool:
        """Cancel a resting order by its rfq_id. Honours DRY_RUN."""
        if self._cfg.dry_run:
            logger.info("[DRY_RUN] would POST /api/orders/cancel {{rfq_id: {}}}", rfq_id)
            return True

        await self._request("POST", "/api/orders/cancel", json={"rfq_id": rfq_id})
        self.metrics.record_order_cancelled()
        logger.info("Cancelled rfq_id={}", rfq_id)
        return True

    async def cancel_all(self, instrument: Optional[str] = None) -> int:
        """Cancel every resting order for the instrument. Returns count."""
        orders = await self.get_open_orders(instrument)
        n = 0
        for o in orders:
            try:
                await self.cancel_order(o.rfq_id)
                n += 1
            except VariationalAPIError as e:
                logger.warning("cancel_all: failed to cancel {}: {}", o.rfq_id, e)
        return n

    async def close_all(self) -> bool:
        """POST /api/orders/close_all - flatten everything. Honours DRY_RUN."""
        if self._cfg.dry_run:
            logger.info("[DRY_RUN] would POST /api/orders/close_all")
            return True
        await self._request("POST", "/api/orders/close_all")
        logger.info("close_all sent")
        return True

    # ---- account -----------------------------------------------------------

    async def get_portfolio(self) -> Portfolio:
        """GET /api/portfolio?compute_margin=true - balance and margin."""
        data = await self._request(
            "GET", "/api/portfolio", params={"compute_margin": "true"}
        )
        if not isinstance(data, dict):
            return Portfolio()
        return Portfolio.model_validate(data)

    async def get_balance(self) -> Optional[Decimal]:
        """Convenience wrapper returning just the account balance."""
        return (await self.get_portfolio()).balance

    # ---- quote-based execution ---------------------------------------------

    async def request_indicative_quote(
        self, qty: Decimal, instrument: Optional[Instrument] = None
    ) -> Quote:
        """POST /api/quotes/indicative - ask what price you would get.

        Note there is no ``side`` here: the quote is two-sided and you pick the
        direction when executing against the returned ``quote_id``.
        """
        req = IndicativeQuoteRequest(
            instrument=instrument or self._instrument(), qty=qty
        )
        data = await self._request(
            "POST", "/api/quotes/indicative", json=req.to_payload()
        )
        quote = Quote.model_validate(data)
        logger.info("indicative quote {} -> price={}", quote.quote_id, quote.price)
        return quote

    async def place_market_order(
        self,
        quote_id: str,
        side: Side,
        *,
        max_slippage: Optional[Decimal] = None,
        is_reduce_only: bool = False,
    ) -> OrderAck:
        """POST /api/orders/new/market - execute against an indicative quote.

        Unlike a normal exchange this takes no instrument or qty: both are fixed
        by the quote you are accepting. Honours DRY_RUN.
        """
        req = MarketOrderRequest(
            quote_id=quote_id,
            side=side,
            max_slippage=max_slippage if max_slippage is not None else self._cfg.max_slippage,
            is_reduce_only=is_reduce_only,
        )
        payload = req.to_payload()
        if self._cfg.dry_run:
            logger.info("[DRY_RUN] would POST /api/orders/new/market {}", payload)
            return OrderAck(rfq_id="dry-run-000000000000", status="dry_run")

        data = await self._request("POST", "/api/orders/new/market", json=payload)
        ack = OrderAck.model_validate(data)
        self.metrics.record_order_placed()
        logger.info("Market {} on quote {} -> rfq_id={}", side.value, quote_id, ack.rfq_id)
        return ack

    async def accept_quote(
        self,
        quote_id: str,
        side: Side,
        *,
        max_slippage: Optional[Decimal] = None,
        is_reduce_only: bool = False,
    ) -> OrderAck:
        """POST /api/quotes/accept - payload-identical to the market endpoint.

        Used by the live reference client to *close* a position (side flipped,
        ``is_reduce_only=True``). Honours DRY_RUN.
        """
        req = AcceptQuoteRequest(
            quote_id=quote_id,
            side=side,
            max_slippage=max_slippage if max_slippage is not None else self._cfg.max_slippage,
            is_reduce_only=is_reduce_only,
        )
        payload = req.to_payload()
        if self._cfg.dry_run:
            logger.info("[DRY_RUN] would POST /api/quotes/accept {}", payload)
            return OrderAck(rfq_id="dry-run-000000000000", status="dry_run")

        data = await self._request("POST", "/api/quotes/accept", json=payload)
        ack = OrderAck.model_validate(data)
        self.metrics.record_order_placed()
        logger.info("Accepted quote {} {} -> rfq_id={}", quote_id, side.value, ack.rfq_id)
        return ack

    async def market_enter(
        self, side: Side, qty: Decimal, *, is_reduce_only: bool = False
    ) -> OrderAck:
        """The full two-step taker flow: get a quote, then execute it.

        This is the shape the live reference implementation uses, wrapped into
        one call so strategies do not have to remember the ordering.
        """
        quote = await self.request_indicative_quote(qty)
        return await self.place_market_order(
            quote.quote_id, side, is_reduce_only=is_reduce_only
        )

    # ---- conditional orders ------------------------------------------------

    async def place_trigger_order(
        self,
        side: Side,
        order_type: OrderType,
        trigger_price: Decimal,
        qty: Decimal,
        *,
        limit_price: Optional[Decimal] = None,
        is_reduce_only: bool = True,
    ) -> OrderAck:
        """Place a stop_limit / take_profit / stop_loss order.

        Field names here are inferred from the frontend bundle rather than
        confirmed live - see docs/ for the proven-vs-assumed scorecard.
        """
        if order_type == OrderType.LIMIT:
            raise ValueError("use place_limit_order for plain limit orders")
        req = TriggerOrderRequest(
            instrument=self._instrument(),
            side=side,
            order_type=order_type,
            trigger_price=trigger_price,
            qty=qty,
            limit_price=limit_price,
            is_reduce_only=is_reduce_only,
        )
        payload = req.to_payload()
        if self._cfg.dry_run:
            logger.info("[DRY_RUN] would POST /api/orders/new/limit {}", payload)
            return OrderAck(rfq_id="dry-run-000000000000", status="dry_run")

        data = await self._request("POST", "/api/orders/new/limit", json=payload)
        ack = OrderAck.model_validate(data)
        self.metrics.record_order_placed()
        logger.info(
            "Placed {} {} trigger={} -> rfq_id={}",
            order_type.value, side.value, trigger_price, ack.rfq_id,
        )
        return ack

