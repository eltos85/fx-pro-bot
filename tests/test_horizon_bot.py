"""Сигналы horizon_bot: канон без подгонки, закрытые бары."""

import pytest

from horizon_bot.app.main import working_capital
from horizon_bot.signals import sma, sma20_50_4h, sma200_daily


def test_working_capital_caps_the_fat_demo_account():
    """На общем демо $47k, ставка считается от тысячи."""
    assert working_capital(47600.0, 1000.0) == pytest.approx(1000.0)
    assert working_capital(47600.0, 1000.0) * 0.15 == pytest.approx(150.0)


def test_working_capital_does_not_invent_money_if_wallet_is_smaller():
    assert working_capital(400.0, 1000.0) == pytest.approx(400.0)


def test_working_capital_zero_limit_means_use_the_live_wallet():
    assert working_capital(47600.0, 0.0) == pytest.approx(47600.0)


def test_sma_window():
    assert sma([1, 2, 3, 4], 3) == 3.0
    assert sma([1, 2], 3) is None


def test_sma200_long_when_close_above():
    closes = [100.0] * 199 + [110.0]
    assert sma200_daily(closes) == 1


def test_sma200_flat_when_close_below():
    closes = [100.0] * 199 + [90.0]
    assert sma200_daily(closes) == 0


def test_sma200_none_if_short():
    assert sma200_daily([1.0] * 50) is None


def test_sma20_50_long_when_fast_above_slow():
    # 50 баров: первые 30 по 10, последние 20 по 20 → SMA20=20, SMA50=14
    closes = [10.0] * 30 + [20.0] * 20
    assert sma20_50_4h(closes) == 1


def test_sma20_50_flat_when_fast_below_slow():
    closes = [20.0] * 30 + [10.0] * 20
    assert sma20_50_4h(closes) == 0
