"""Risk-engine tests: only server/auth failures count toward the kill switch,
successes reset the streak, and the position cap gates entries."""

from __future__ import annotations

from decimal import Decimal

import pytest

from variational.errors import (
    KillSwitchError,
    VariationalAPIError,
    VariationalAuthError,
    VariationalError,
    VariationalServerError,
)
from variational.risk import RiskEngine


def _server_error() -> VariationalServerError:
    return VariationalServerError(500, "boom")


def test_kill_switch_trips_after_threshold_server_errors():
    r = RiskEngine(Decimal("10"), kill_switch_threshold=3)
    r.on_failure(_server_error())
    r.on_failure(_server_error())
    assert not r.tripped
    assert r.consecutive_failures == 2

    r.on_failure(_server_error())
    assert r.tripped
    with pytest.raises(KillSwitchError):
        r.ensure_live()


def test_auth_errors_count_toward_kill_switch():
    r = RiskEngine(Decimal("10"), kill_switch_threshold=2)
    r.on_failure(VariationalAuthError(401, "unauthorized"))
    r.on_failure(VariationalAuthError(401, "unauthorized"))
    assert r.tripped


def test_client_4xx_does_not_count():
    r = RiskEngine(Decimal("10"), kill_switch_threshold=3)
    for _ in range(5):
        r.on_failure(VariationalAPIError(400, "bad price"))
    assert r.consecutive_failures == 0
    assert not r.tripped


def test_success_resets_failure_streak():
    r = RiskEngine(Decimal("10"), kill_switch_threshold=3)
    r.on_failure(_server_error())
    r.on_failure(_server_error())
    r.on_success()
    assert r.consecutive_failures == 0
    # Two fresh failures must not trip after the reset.
    r.on_failure(_server_error())
    r.on_failure(_server_error())
    assert not r.tripped


def test_position_cap_boundary():
    r = RiskEngine(Decimal("10"))
    assert r.can_open(Decimal("9"), Decimal("1")) is True    # -> |10|, at cap OK
    assert r.can_open(Decimal("10"), Decimal("1")) is False  # -> |11|, over
    assert r.can_open(Decimal("-9"), Decimal("-1")) is True  # short side, |10|
    assert r.can_open(Decimal("0"), Decimal("11")) is False


def test_tripped_engine_blocks_all_opens():
    r = RiskEngine(Decimal("100"))
    r.trip("manual halt")
    assert r.can_open(Decimal("0"), Decimal("1")) is False


def test_check_open_raises_on_cap_breach():
    r = RiskEngine(Decimal("1"))
    with pytest.raises(VariationalError) as excinfo:
        r.check_open(Decimal("1"), Decimal("1"))
    assert "cap" in str(excinfo.value)


def test_check_open_raises_kill_switch_when_tripped():
    r = RiskEngine(Decimal("100"))
    r.trip("manual halt")
    with pytest.raises(KillSwitchError):
        r.check_open(Decimal("0"), Decimal("1"))


def test_reset_clears_tripped_and_counter():
    r = RiskEngine(Decimal("10"), kill_switch_threshold=1)
    r.on_failure(_server_error())
    assert r.tripped
    r.reset()
    assert not r.tripped
    assert r.consecutive_failures == 0
    r.ensure_live()  # must not raise
