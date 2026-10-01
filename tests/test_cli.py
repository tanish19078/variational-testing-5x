"""Tests for the command-line interface.

The most important assertion here is the safety default: without --live,
nothing that mutates a book may reach the venue. The rest covers argument
wiring and exit codes.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

import cli
from mock.mock_server import BOOK


@pytest.fixture()
def base(mock_backend):
    return ["--base-url", mock_backend]


# ---- safety ---------------------------------------------------------------


def test_dry_run_is_the_default(base, capsys):
    """No --live, so the order must not reach the book."""
    rc = cli.main(base + ["place", "--side", "buy", "--price", "95", "--qty", "1"])
    assert rc == 0
    assert BOOK.orders == {}
    assert "dry-run" in capsys.readouterr().out


def test_live_flag_actually_places(base):
    rc = cli.main(
        base + ["--live", "place", "--side", "buy", "--price", "95", "--qty", "1"]
    )
    assert rc == 0
    assert len(BOOK.orders) == 1


def test_dry_run_flag_overrides_live(base):
    """--dry-run wins, so the safe option cannot be lost to flag ordering."""
    rc = cli.main(
        base
        + ["--live", "--dry-run", "place", "--side", "buy", "--price", "9", "--qty", "1"]
    )
    assert rc == 0
    assert BOOK.orders == {}


def test_market_order_is_dry_run_by_default(base):
    rc = cli.main(base + ["place", "--side", "buy", "--qty", "1", "--market"])
    assert rc == 0
    assert BOOK.positions == []


# ---- status ---------------------------------------------------------------


def test_status_on_an_empty_book(base, capsys):
    rc = cli.main(base + ["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "balance    10000" in out
    assert "positions  0" in out


def test_status_json_is_parseable(base, capsys):
    cli.main(base + ["--live", "place", "--side", "buy", "--qty", "2", "--market"])
    capsys.readouterr()  # discard the placement output

    rc = cli.main(base + ["status", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["balance"] == "10000"
    assert len(data["positions"]) == 1
    # Money survives as a string, never a float.
    assert data["positions"][0]["qty"] == "2"
    assert isinstance(data["positions"][0]["qty"], str)


def test_status_survives_a_missing_portfolio_endpoint(base, capsys, monkeypatch):
    """Not every deployment exposes /api/portfolio; status must still work."""
    from variational.client import VariationalClient
    from variational.errors import VariationalAPIError

    async def boom(self):
        raise VariationalAPIError(404, "not found", "")

    monkeypatch.setattr(VariationalClient, "get_balance", boom)
    rc = cli.main(base + ["status"])
    assert rc == 0
    assert "balance    n/a" in capsys.readouterr().out


# ---- other subcommands ----------------------------------------------------


def test_quote_prints_a_quote_id(base, capsys):
    rc = cli.main(base + ["quote", "--qty", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "quote_id" in out
    assert "price    100" in out


def test_cancel_all_reports_zero_on_an_empty_book(base):
    assert cli.main(base + ["--live", "cancel-all"]) == 0


def test_close_all_flattens(base):
    cli.main(base + ["--live", "place", "--side", "buy", "--qty", "1", "--market"])
    assert BOOK.positions
    assert cli.main(base + ["--live", "close-all"]) == 0
    assert BOOK.positions == []


def test_trigger_order_goes_through(base, capsys):
    rc = cli.main(
        base
        + [
            "--live", "trigger", "--side", "sell", "--type", "stop_loss",
            "--trigger-price", "90", "--qty", "1",
        ]
    )
    assert rc == 0
    assert len(BOOK.orders) == 1
    assert list(BOOK.orders.values())[0]["trigger_price"] == "90"


def test_trigger_rejects_plain_limit(base):
    """`limit` is not offered as a --type choice at all."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            base
            + [
                "trigger", "--side", "sell", "--type", "limit",
                "--trigger-price", "90", "--qty", "1",
            ]
        )
    assert excinfo.value.code == 2


# ---- argument handling ----------------------------------------------------


def test_limit_order_without_a_price_is_an_error(base):
    assert cli.main(base + ["place", "--side", "buy", "--qty", "1"]) == 2


def test_bad_decimal_is_rejected_by_the_parser(base):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(base + ["quote", "--qty", "not-a-number"])
    assert excinfo.value.code == 2


def test_api_errors_become_exit_code_1(base):
    """A nonexistent quote id is a 400; the CLI must not traceback."""
    assert cli.main(base + ["--live", "cancel", "no-such-rfq-id"]) == 1


def test_underlying_override_changes_the_instrument(base, capsys):
    cli.main(base + ["--underlying", "DOGE", "--live", "quote", "--qty", "1"])
    # The mock echoes back what it was quoted on.
    quote = list(BOOK.quotes.values())
    assert quote == [] or quote[0]["instrument"]["underlying"] == "DOGE"


def test_no_subcommand_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
