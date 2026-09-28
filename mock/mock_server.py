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
    POST /api/orders/new/limit    { instrument, side, order_type, limit_price, qty, ... }
    POST /api/orders/cancel       { rfq_id }
    POST /api/orders/close_all

Fault injection for tests (query params on any request):
    ?__status=500        force one response to a given status
    ?__fail_n=3&__status=503   fail the next N calls then behave normally
    ?__rate_limit=1      respond 429 with a Retry-After header once
Or globally via the control endpoint:
    POST /__control  { "fail_next": 3, "status": 503 }
"""

from __future__ import annotations

import json
import threading
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
            }
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
        self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/__control":
            body = self._read_json()
            BOOK.fail_next = int(body.get("fail_next", 0))
            BOOK.fail_status = int(body.get("status", 500))
            self._send(200, {"ok": True, "fail_next": BOOK.fail_next})
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
