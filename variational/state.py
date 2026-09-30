"""Durable record of orders we believe are live.

Why this exists: if the process dies between "venue accepted my order" and
"I cancelled it", that order is still resting on the venue and nobody is
managing it. On a real venue that is an open, unmanaged money risk.

So every placement is journalled to disk *before* we consider it live, and
cleared on cancel. On the next start, ``recover()`` reports the rfq_ids we
thought were live so the bot can reconcile them against the venue's own view
and cancel anything stale.

The file is written atomically (temp file + replace) so a crash mid-write
cannot leave a truncated journal.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from loguru import logger


def _money(v: Decimal | str) -> str:
    """Exact decimal string, never scientific notation, never a float."""
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


class StateStore:
    """A tiny JSON journal of live order ids, keyed by rfq_id."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {"version": 1, "orders": {}}
        self._loaded = False

    # ---- disk --------------------------------------------------------------

    def load(self) -> "StateStore":
        """Read the journal if present. A corrupt file is quarantined, not fatal."""
        if not self._path.exists():
            self._loaded = True
            return self
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("orders"), dict):
                self._data = {"version": raw.get("version", 1), "orders": raw["orders"]}
            else:
                raise ValueError("unexpected journal shape")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            backup = self._path.with_suffix(self._path.suffix + ".corrupt")
            logger.warning(
                "state: journal unreadable ({}); moving aside to {}", e, backup.name
            )
            try:
                self._path.replace(backup)
            except OSError:
                pass
            self._data = {"version": 1, "orders": {}}
        self._loaded = True
        return self

    def _flush(self) -> None:
        """Atomic write: temp file in the same dir, then os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            # Never leave the temp file behind on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- journal -----------------------------------------------------------

    def record_order(
        self,
        rfq_id: str,
        *,
        side: str,
        limit_price: Decimal | str,
        qty: Decimal | str,
        instrument: str,
    ) -> None:
        """Note an order as live. Call this immediately after the venue acks.

        Money-like values are stringified rather than handed to json, which
        cannot encode a Decimal and would otherwise need a float cast -- the
        one thing this codebase never does with a price.
        """
        self._data["orders"][rfq_id] = {
            "side": str(side),
            "limit_price": _money(limit_price),
            "qty": _money(qty),
            "instrument": str(instrument),
            "recorded_at": time.time(),
        }
        self._flush()

    def clear_order(self, rfq_id: str) -> bool:
        """Forget an order (cancelled or filled). True if it was known."""
        if self._data["orders"].pop(rfq_id, None) is None:
            return False
        self._flush()
        return True

    def clear_all(self) -> int:
        n = len(self._data["orders"])
        self._data["orders"] = {}
        self._flush()
        return n

    # ---- reads -------------------------------------------------------------

    @property
    def live_order_ids(self) -> list[str]:
        return sorted(self._data["orders"])

    def get(self, rfq_id: str) -> Optional[dict[str, Any]]:
        return self._data["orders"].get(rfq_id)

    def recover(self) -> list[str]:
        """Order ids left over from a previous run (call right after load())."""
        if not self._loaded:
            self.load()
        stale = self.live_order_ids
        if stale:
            logger.warning(
                "state: {} order(s) left from a previous run: {}",
                len(stale), ", ".join(i[:8] for i in stale),
            )
        return stale

    def __len__(self) -> int:
        return len(self._data["orders"])
