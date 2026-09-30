"""A local mock of the Variational Omni order API.

It implements the exact wire contract the real venue uses (endpoint paths,
field names, the rfq_id round-trip, and error-message strings), so the bot and
client can be exercised end to end without touching the live site.

It runs on the standard library only (no framework), so `pip install -r
requirements.txt` is enough. Start it with:

    python -m mock.mock_server            # binds 127.0.0.1:8787

Supported routes:
    GET  /api/positions
    GET  /api/orders/v2?status=pending&instrument=<id>
    GET  /api/portfolio?compute_margin=true
    POST /api/orders/new/limit    { instrument, side, order_type, limit_price, qty, ... }
    POST /api/orders/new/market   { quote_id, side, max_slippage, is_reduce_only }
    POST /api/quotes/indicative   { instrument, qty }  -> { quote_id, price }
    POST /api/quotes/accept       { quote_id, side, max_slippage, is_reduce_only }
    POST /api/orders/cancel       { rfq_id }
    POST /api/orders/close_all

Fault injection for tests (query params on any request):
    ?__status=500        force one response to a given status
    ?__fail_n=3&__status=503   fail the next N calls then behave normally
    ?__rate_limit=1      respond 429 with a Retry-After header once
Or globally via the control endpoint:
    POST /__control  { "fail_next": 3, "status": 503 }

Market simulation via the same control endpoint:
    POST /__control  { "mark": "101.5" }   move the mark and fill crossed orders
    POST /__control  { "balance": "5000" } set the account balance
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


class _Book:
    """In-memory order/position state, guarded by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = []
        # Fault injection counters.
        self.fail_next = 0
        self.fail_status = 500
        # Account + quote state.
        self.balance = Decimal("10000")
        self.mark = Decimal("100")
        self.quotes: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        """Return to a clean slate (used between tests)."""
        with self._lock:
            self.orders.clear()
            self.positions.clear()
            self.quotes.clear()
            self.fail_next = 0
            self.fail_status = 500
            self.balance = Decimal("10000")
            self.mark = Decimal("100")

    def place(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rfq_id = str(uuid.uuid4())
            order = {
                "rfq_id": rfq_id,
                "instrument": body.get("instrument"),
                "side": body.get("side"),
                "order_type": body.get("order_type", "limit"),
                "limit_price": body.get("limit_price"),
                "qty": body.get("qty"),
                "status": "pending",
                "is_reduce_only": bool(body.get("is_reduce_only", False)),
            }
            if body.get("trigger_price") is not None:
                order["trigger_price"] = body["trigger_price"]
            self.orders[rfq_id] = order
            return {"rfq_id": rfq_id, "status": "pending"}

    def cancel(self, rfq_id: str) -> bool:
        with self._lock:
            order = self.orders.get(rfq_id)
            if order is None or order["status"] != "pending":
                return False
            order["status"] = "canceled"
            del self.orders[rfq_id]
            return True

    def open_orders(self, instrument: Optional[str]) -> list[dict[str, Any]]:
        with self._lock:
            rows = [o for o in self.orders.values() if o["status"] == "pending"]
            if instrument:
                rows = [o for o in rows if _symbol(o.get("instrument")) == instrument]
            return list(rows)

    # ---- quote flow --------------------------------------------------------

    def make_quote(self, instrument: Any, qty: Any) -> dict[str, Any]:
        """Issue an indicative quote. Two-sided: no side is chosen yet."""
        with self._lock:
            quote_id = str(uuid.uuid4())
            quote = {
                "quote_id": quote_id,
                "instrument": instrument,
                "qty": str(qty),
                "price": str(self.mark),
                "expires_at": time.time() + 10,
            }
            self.quotes[quote_id] = quote
            return quote

    def execute_quote(
        self, quote_id: str, side: str, is_reduce_only: bool
    ) -> Optional[dict[str, Any]]:
        """Consume a quote and book the resulting fill. None if unknown/expired."""
        with self._lock:
            quote = self.quotes.pop(quote_id, None)
            if quote is None:
                return None
            qty = Decimal(quote["qty"])
            price = Decimal(quote["price"])
            self._apply_fill_locked(
                quote.get("instrument"), side, qty, price, is_reduce_only
            )
            return {"rfq_id": str(uuid.uuid4()), "status": "filled"}

    # ---- fills / positions -------------------------------------------------

    def fill_crossing_orders(self) -> list[str]:
        """Fill any resting order the current mark has crossed.

        A buy fills when mark <= its limit, a sell when mark >= its limit.
        Returns the rfq_ids filled.
        """
        filled: list[str] = []
        with self._lock:
            for rfq_id, order in list(self.orders.items()):
                if order["status"] != "pending" or order.get("limit_price") is None:
                    continue
                limit = Decimal(str(order["limit_price"]))
                side = order.get("side")
                crossed = (
                    (side == "buy" and self.mark <= limit)
                    or (side == "sell" and self.mark >= limit)
                )
                if not crossed:
                    continue
                self._apply_fill_locked(
                    order.get("instrument"),
                    str(side),
                    Decimal(str(order.get("qty") or 0)),
                    limit,
                    bool(order.get("is_reduce_only")),
                )
                del self.orders[rfq_id]
                filled.append(rfq_id)
        return filled

    def _apply_fill_locked(
        self,
        instrument: Any,
        side: str,
        qty: Decimal,
        price: Decimal,
        is_reduce_only: bool = False,
    ) -> None:
        """Update net position. Caller must hold the lock.

        Positions are emitted in the live nested shape:
        ``{"position_info": {"instrument": ..., "qty": ..., ...}}``
        """
        symbol = _symbol(instrument)
        signed = qty if side == "buy" else -qty

        row = None
        for candidate in self.positions:
            info = candidate.get("position_info", candidate)
            if _symbol(info.get("instrument")) == symbol:
                row = candidate
                break

        if row is None:
            if is_reduce_only:
                return  # nothing to reduce
            self.positions.append(
                {
                    "position_info": {
                        "instrument": instrument,
                        "qty": str(signed),
                        "entry_price": str(price),
                        "mark_price": str(self.mark),
                    }
                }
            )
            return

        info = row.setdefault("position_info", {})
        current = Decimal(str(info.get("qty", "0")))
        if is_reduce_only:
            # Never flip through zero on a reduce-only fill.
            if current > 0:
                signed = max(signed, -current)
            elif current < 0:
                signed = min(signed, -current)
            else:
                return
        new_net = current + signed

        if new_net == 0:
            self.positions.remove(row)
            return

        # Weighted average entry when adding in the same direction; keep the
        # existing entry when reducing.
        old_entry = Decimal(str(info.get("entry_price", price)))
        if (current >= 0 and signed > 0) or (current <= 0 and signed < 0):
            total = abs(current) + abs(signed)
            info["entry_price"] = str(
                ((old_entry * abs(current)) + (price * abs(signed))) / total
            ) if total else str(price)
        info["qty"] = str(new_net)
        info["mark_price"] = str(self.mark)



def _symbol(instr: Any) -> Optional[str]:
    if not isinstance(instr, dict):
        return None
    return (
        f"P-{instr.get('underlying')}-{instr.get('settlement_asset')}"
        f"-{instr.get('funding_interval_s')}"
    )


BOOK = _Book()


class Handler(BaseHTTPRequestHandler):
    # Silence the default noisy stderr logging.
    def log_message(self, *args: Any) -> None:  # noqa: D401
        pass

    # ---- helpers ----------------------------------------------------------

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error_message": message})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _maybe_fault(self, qs: dict[str, list[str]]) -> bool:
        """Apply fault injection. Returns True if a fault response was sent."""
        # Per-request forced status.
        if "__status" in qs:
            status = int(qs["__status"][0])
            n = int(qs.get("__fail_n", ["1"])[0])
            if n <= 1:
                self._fault_response(status)
                return True
            # Latch remaining failures onto the book.
            BOOK.fail_next = n - 1
            BOOK.fail_status = status
            self._fault_response(status)
            return True
        if "__rate_limit" in qs:
            self._fault_response(429)
            return True
        # Latched failures from a prior __fail_n or /__control.
        if BOOK.fail_next > 0:
            BOOK.fail_next -= 1
            self._fault_response(BOOK.fail_status)
            return True
        return False

    def _fault_response(self, status: int) -> None:
        if status == 429:
            self._send(429, {"error_message": "rate limited"}, {"retry-after": "1"})
        elif status in (401, 403):
            self._error(status, "unauthorized")
        else:
            self._error(status, f"injected fault {status}")

    # ---- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if self._maybe_fault(qs):
            return

        if parsed.path == "/api/positions":
            self._send(200, BOOK.positions)
            return
        if parsed.path == "/api/orders/v2":
            instrument = qs.get("instrument", [None])[0]
            rows = BOOK.open_orders(instrument)
            self._send(200, {
                "pagination": {"object_count": len(rows), "next_page": None},
                "result": rows,
            })
            return
        if parsed.path == "/api/portfolio":
            self._send(200, {
                "balance": str(BOOK.balance),
                "margin_computed": qs.get("compute_margin", ["false"])[0] == "true",
            })
            return
        self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/__control":
            body = self._read_json()
            if "fail_next" in body or "status" in body:
                BOOK.fail_next = int(body.get("fail_next", 0))
                BOOK.fail_status = int(body.get("status", 500))
            filled: list[str] = []
            if "mark" in body:
                with BOOK._lock:
                    BOOK.mark = Decimal(str(body["mark"]))
                filled = BOOK.fill_crossing_orders()
            if "balance" in body:
                with BOOK._lock:
                    BOOK.balance = Decimal(str(body["balance"]))
            self._send(200, {
                "ok": True,
                "fail_next": BOOK.fail_next,
                "mark": str(BOOK.mark),
                "filled": filled,
            })
            return

        if self._maybe_fault(qs):
            return

        if parsed.path == "/api/orders/new/limit":
            body = self._read_json()
            for field in ("instrument", "side", "order_type", "limit_price"):
                if field not in body:
                    self._error(
                        400,
                        "Failed to deserialize the JSON body into the target "
                        f"type: missing field `{field}` at line 1 column 2",
                    )
                    return
            if _symbol(body.get("instrument")) is None:
                self._error(400, "unsupported instrument")
                return
            self._send(200, BOOK.place(body))
            return

        if parsed.path == "/api/quotes/indicative":
            body = self._read_json()
            for field in ("instrument", "qty"):
                if field not in body:
                    self._error(
                        400,
                        "Failed to deserialize the JSON body into the target "
                        f"type: missing field `{field}` at line 1 column 2",
                    )
                    return
            if _symbol(body.get("instrument")) is None:
                self._error(400, "unsupported instrument")
                return
            self._send(200, BOOK.make_quote(body["instrument"], body["qty"]))
            return

        if parsed.path in ("/api/orders/new/market", "/api/quotes/accept"):
            body = self._read_json()
            for field in ("quote_id", "side"):
                if field not in body:
                    self._error(
                        400,
                        "Failed to deserialize the JSON body into the target "
                        f"type: missing field `{field}` at line 1 column 2",
                    )
                    return
            result = BOOK.execute_quote(
                body["quote_id"],
                str(body["side"]),
                bool(body.get("is_reduce_only", False)),
            )
            if result is None:
                self._error(400, "quote not found or expired")
                return
            self._send(200, result)
            return

        if parsed.path == "/api/orders/cancel":
            body = self._read_json()
            rfq_id = body.get("rfq_id")
            if not rfq_id:
                self._error(
                    400,
                    "Failed to deserialize the JSON body into the target type: "
                    "missing field `rfq_id` at line 1 column 2",
                )
                return
            if BOOK.cancel(rfq_id):
                self._send(200, True)
            else:
                self._error(
                    400,
                    "unable to cancel rfq, either rfq does not exist, is "
                    "inactive, or is currently pending clearing",
                )
            return

        if parsed.path == "/api/orders/close_all":
            with BOOK._lock:
                BOOK.orders.clear()
                BOOK.positions.clear()
            self._send(200, True)
            return

        self._error(404, "not found")


def make_server(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    server = make_server()
    host, port = server.server_address
    print(f"mock Omni backend listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
