"""Unified command-line entry point.

    python -m cli mock                    start the mock backend
    python -m cli status                  positions, open orders, balance
    python -m cli quote --qty 1           ask what price you would get
    python -m cli place --side buy --price 1.90 --qty 1
    python -m cli cancel-all
    python -m cli close-all
    python -m cli run                     the range bot
    python -m cli monitor                 live public stats feed

Every subcommand that can move money respects DRY_RUN, and `--live` is the
only way to turn it off -- there is deliberately no way to go live by
forgetting a flag.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from loguru import logger

from config import Config
from variational.client import VariationalClient
from variational.errors import VariationalError
from variational.logging_setup import setup_from_config
from variational.models import OrderType, Side


def _decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a decimal: {raw!r}")


def _config(args: argparse.Namespace) -> Config:
    """Build the config, applying CLI overrides on top of the environment."""
    cfg = Config.from_env()
    overrides: dict[str, Any] = {}
    if getattr(args, "live", False):
        overrides["dry_run"] = False
    if getattr(args, "dry_run", False):
        overrides["dry_run"] = True
    if getattr(args, "base_url", None):
        overrides["base_url"] = args.base_url.rstrip("/")
    if getattr(args, "underlying", None):
        overrides["underlying"] = args.underlying
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)
    return cfg


def _banner(cfg: Config) -> None:
    mode = "DRY RUN" if cfg.dry_run else "*** LIVE ***"
    logger.info("{} | {} | {}", mode, cfg.instrument_id, cfg.base_url)


# ---- subcommands -----------------------------------------------------------


async def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        positions = await client.get_positions()
        orders = await client.get_open_orders()
        try:
            balance = await client.get_balance()
        except VariationalError as e:
            # Not every deployment exposes /api/portfolio; do not fail status.
            logger.debug("balance unavailable: {}", e)
            balance = None

    if args.json:
        print(json.dumps({
            "balance": None if balance is None else str(balance),
            "positions": [p.model_dump(mode="json") for p in positions],
            "open_orders": [o.model_dump(mode="json") for o in orders],
        }, indent=2))
        return 0

    print(f"balance    {balance if balance is not None else 'n/a'}")
    print(f"positions  {len(positions)}")
    for p in positions:
        symbol = p.instrument.symbol if p.instrument else "?"
        print(f"    {symbol:<24} qty {p.signed_qty} @ {p.entry_price}")
    print(f"open orders {len(orders)}")
    for o in orders:
        side = o.side.value if o.side else "?"
        print(f"    {o.rfq_id[:8]}  {side:<4} {o.qty} @ {o.limit_price}")
    return 0


async def _cmd_quote(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        quote = await client.request_indicative_quote(args.qty)
    print(f"quote_id {quote.quote_id}")
    print(f"price    {quote.price}")
    print(f"qty      {quote.qty}")
    return 0


async def _cmd_place(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        if args.market:
            ack = await client.market_enter(
                Side(args.side), args.qty, is_reduce_only=args.reduce_only
            )
        else:
            if args.price is None:
                logger.error("--price is required unless --market is given")
                return 2
            ack = await client.place_limit_order(
                Side(args.side), args.price, args.qty,
                is_reduce_only=args.reduce_only,
            )
    print(ack.rfq_id)
    return 0


async def _cmd_cancel_all(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        n = await client.cancel_all()
    logger.info("cancelled {} order(s)", n)
    return 0


async def _cmd_close_all(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        await client.close_all()
    logger.info("close_all sent")
    return 0


async def _cmd_cancel(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        await client.cancel_order(args.rfq_id)
    return 0


async def _cmd_trigger(cfg: Config, args: argparse.Namespace) -> int:
    async with VariationalClient(cfg) as client:
        ack = await client.place_trigger_order(
            Side(args.side),
            OrderType(args.type),
            args.trigger_price,
            args.qty,
            limit_price=args.price,
        )
    print(ack.rfq_id)
    return 0


def _cmd_mock(cfg: Config, args: argparse.Namespace) -> int:
    from mock.mock_server import make_server

    server = make_server(args.host, args.port)
    host, port = server.server_address
    logger.info("mock Omni backend on http://{}:{}", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()
    return 0


async def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from range_bot import main as bot_main

    # Pass the CLI-overridden config through rather than letting the bot
    # rebuild one from the environment, so --live and --base-url apply.
    await bot_main(cfg)
    return 0


async def _cmd_monitor(cfg: Config, args: argparse.Namespace) -> int:
    from monitor import main as monitor_main

    return int(await monitor_main() or 0)


# ---- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli", description="Variational Omni automation suite"
    )
    parser.add_argument("--base-url", help="override VR_BASE_URL")
    parser.add_argument("--underlying", help="override UNDERLYING, e.g. TRUMP")
    parser.add_argument(
        "--live",
        action="store_true",
        help="disable DRY_RUN for this invocation (orders will be real)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="force DRY_RUN on"
    )
    parser.add_argument("--log-level", default=None)

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="positions, open orders, balance")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status, is_async=True)

    p = sub.add_parser("quote", help="request an indicative quote")
    p.add_argument("--qty", type=_decimal, required=True)
    p.set_defaults(func=_cmd_quote, is_async=True)

    p = sub.add_parser("place", help="place an order")
    p.add_argument("--side", choices=[s.value for s in Side], required=True)
    p.add_argument("--qty", type=_decimal, required=True)
    p.add_argument("--price", type=_decimal, help="limit price")
    p.add_argument("--market", action="store_true", help="quote-based market order")
    p.add_argument("--reduce-only", action="store_true")
    p.set_defaults(func=_cmd_place, is_async=True)

    p = sub.add_parser("trigger", help="place a conditional order")
    p.add_argument("--side", choices=[s.value for s in Side], required=True)
    p.add_argument(
        "--type",
        choices=[t.value for t in OrderType if t is not OrderType.LIMIT],
        required=True,
    )
    p.add_argument("--trigger-price", type=_decimal, required=True)
    p.add_argument("--qty", type=_decimal, required=True)
    p.add_argument("--price", type=_decimal, help="resting limit once triggered")
    p.set_defaults(func=_cmd_trigger, is_async=True)

    p = sub.add_parser("cancel", help="cancel one order by rfq_id")
    p.add_argument("rfq_id")
    p.set_defaults(func=_cmd_cancel, is_async=True)

    p = sub.add_parser("cancel-all", help="cancel every resting order")
    p.set_defaults(func=_cmd_cancel_all, is_async=True)

    p = sub.add_parser("close-all", help="flatten orders and positions")
    p.set_defaults(func=_cmd_close_all, is_async=True)

    p = sub.add_parser("mock", help="run the mock backend")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=_cmd_mock, is_async=False)

    p = sub.add_parser("run", help="run the range bot")
    p.set_defaults(func=_cmd_run, is_async=True)

    p = sub.add_parser("monitor", help="live public stats feed")
    p.set_defaults(func=_cmd_monitor, is_async=True)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _config(args)
    setup_from_config(
        dataclasses.replace(cfg, log_level=args.log_level or cfg.log_level)
    )

    if args.command not in ("mock", "monitor"):
        _banner(cfg)

    try:
        if args.is_async:
            return asyncio.run(args.func(cfg, args))
        return args.func(cfg, args)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    except VariationalError as e:
        logger.error("{}: {}", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
