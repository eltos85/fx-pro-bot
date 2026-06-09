import pytest

from fx_momentum_bot.app.main import _calc_partial_close_volume, _r_multiple


def test_r_multiple_for_long_and_short() -> None:
    assert _r_multiple(
        "long", entry_price=1.2000, current_price=1.2030, risk_price=0.0010
    ) == pytest.approx(3.0)
    assert _r_multiple(
        "short", entry_price=1.2000, current_price=1.1970, risk_price=0.0010
    ) == pytest.approx(3.0)


def test_r_multiple_zero_on_nonpositive_risk() -> None:
    assert _r_multiple("long", entry_price=1.2, current_price=1.3, risk_price=0.0) == 0.0


def test_partial_close_respects_step_and_min_volume() -> None:
    # 100000 volume, 50% partial, step=1000 => closes 50000.
    assert _calc_partial_close_volume(
        current_volume=100000,
        fraction=0.5,
        step_volume=1000,
        min_volume=1000,
    ) == 50000


def test_partial_close_keeps_minimum_runner() -> None:
    # Requested 90% would close 90000, but we must leave at least min_volume (20000).
    assert _calc_partial_close_volume(
        current_volume=100000,
        fraction=0.9,
        step_volume=1000,
        min_volume=20000,
    ) == 80000


def test_partial_close_disabled_when_position_too_small() -> None:
    # Close would violate "leave at least min volume".
    assert _calc_partial_close_volume(
        current_volume=1500,
        fraction=0.5,
        step_volume=1000,
        min_volume=1000,
    ) == 0
