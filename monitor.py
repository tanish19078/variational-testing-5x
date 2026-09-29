"""Live read-only market monitor.

Reads the public Variational Omni stats endpoint, which is genuinely
unauthenticated (no session, no wallet) and returns live mark / bid / ask /
funding for every listed market. Used both as a standalone monitor and as the
mark-price source for the range bot.

    python monitor.py             # one snapshot for the configured underlying
    python monitor.py --watch     # refresh every POLL_INTERVAL_S
    python monitor.py --all       # table of all listings, once
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx
from loguru import logger

from config import Config


async def _get_stats(stats_url: str, timeout_s: int = 8) -> Optional[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        try:
            r = await c.get(stats_url, headers={"accept": "application/json"})
        except httpx.HTTPError as e:
            logger.warning("stats fetch failed: {}", e)
            return None
    if r.status_code != 200:
        logger.warning("stats HTTP {}", r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _find(stats: dict[str, Any], underlying: str) -> Optional[dict[str, Any]]:
    for listing in stats.get("listings", []):
        if listing.get("ticker") == underlying:
            return listing
    return None


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


async def fetch_mark_price(
    stats_url: str, underlying: str, timeout_s: int = 8
) -> Optional[Decimal]:
    """Return the current mark price for ``underlying`` as a Decimal, or None.

    This is the mark-price source consumed by the range bot.
    """
    stats = await _get_stats(stats_url, timeout_s)
    if stats is None:
        return None
    listing = _find(stats, underlying)
    if listing is None:
        logger.warning("{} not present in stats listings", underlying)
        return None
    return _dec(listing.get("mark_price"))


def _fmt_listing(listing: dict[str, Any]) -> str:
    mark = listing.get("mark_price", "?")
    quotes = listing.get("quotes", {}) or {}
    base = quotes.get("base", {}) or {}
    bid, ask = base.get("bid", "?"), base.get("ask", "?")
    funding = listing.get("funding_rate", "?")
    oi = listing.get("open_interest", {}) or {}
    return (
        f"{listing.get('ticker', '?'):<8} mark={mark:<18} "
        f"bid={bid:<12} ask={ask:<12} funding={funding:<10} "
        f"OI(L/S)={oi.get('long_open_interest', '?')}/{oi.get('short_open_interest', '?')}"
    )


async def _snapshot(cfg: Config, show_all: bool) -> int:
    stats = await _get_stats(cfg.stats_url, cfg.timeout_s)
    if stats is None:
        return 1
    if show_all:
        listings = stats.get("listings", [])
        print(f"{len(listings)} listings @ {stats.get('total_volume_24h', '?')} 24h vol\n")
        for listing in sorted(listings, key=lambda x: x.get("ticker", "")):
            print(_fmt_listing(listing))
        return 0
    listing = _find(stats, cfg.underlying)
    if listing is None:
        print(f"{cfg.underlying} not found")
        return 1
    print(_fmt_listing(listing))
    return 0


async def _watch(cfg: Config) -> int:
    logger.info("watching {} every {}s (Ctrl+C to stop)", cfg.underlying, cfg.poll_interval_s)
    while True:
        listing_line = "no data"
        stats = await _get_stats(cfg.stats_url, cfg.timeout_s)
        if stats is not None:
            listing = _find(stats, cfg.underlying)
            if listing is not None:
                listing_line = _fmt_listing(listing)
        logger.info(listing_line)
        await asyncio.sleep(cfg.poll_interval_s)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Variational Omni market monitor")
    parser.add_argument("--watch", action="store_true", help="refresh continuously")
    parser.add_argument("--all", action="store_true", help="show all listings once")
    args = parser.parse_args()

    cfg = Config.from_env()
    if args.watch:
        return await _watch(cfg)
    return await _snapshot(cfg, show_all=args.all)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("interrupted")
