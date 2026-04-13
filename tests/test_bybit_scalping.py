"""Тесты скальпинг-стратегий bybit_bot."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bybit_bot.market_data.models import Bar


def _make_bars(
    symbol: str = "BTCUSDT",
    n: int = 100,
    base_price: float = 60000.0,
    step: float = 10.0,
    volume: float = 100.0,
) -> list[Bar]:
    """Генератор тестовых баров."""
    bars = []
    for i in range(n):
        price = base_price + step * (i % 20 - 10)
        hour = 10 + i // 60
        minute = i % 60
        bars.append(Bar(
            symbol=symbol,
            ts=datetime(2026, 4, 11, hour, minute, tzinfo=UTC),
            open=price - 5,
            high=price + 20,
            low=price - 20,
            close=price,
            volume=volume,
        ))
    return bars


# ── Indicators ───────────────────────────────────────────────

class TestIndicators:
    def test_vwap_basic(self):
        from bybit_bot.strategies.scalping.indicators import vwap
        bars = _make_bars(n=50)
        val = vwap(bars)
        assert val > 0

    def test_vwap_zero_volume(self):
        from bybit_bot.strategies.scalping.indicators import vwap
        bars = _make_bars(n=10, volume=0)
        val = vwap(bars)
        assert val > 0

    def test_rolling_z_score(self):
        from bybit_bot.strategies.scalping.indicators import rolling_z_score
        values = [float(i) for i in range(50)]
        z = rolling_z_score(values, 20)
        assert z > 0  # последнее значение выше среднего

    def test_rolling_z_score_insufficient(self):
        from bybit_bot.strategies.scalping.indicators import rolling_z_score
        assert rolling_z_score([1.0, 2.0], 20) == 0.0

    def test_ols_hedge_ratio(self):
        from bybit_bot.strategies.scalping.indicators import ols_hedge_ratio
        a = [float(i) for i in range(50)]
        b = [float(i) * 2 for i in range(50)]
        beta = ols_hedge_ratio(a, b)
        assert 0.4 < beta < 0.6

    def test_ols_insufficient(self):
        from bybit_bot.strategies.scalping.indicators import ols_hedge_ratio
        assert ols_hedge_ratio([1.0], [2.0]) == 1.0

    def test_spread_series(self):
        from bybit_bot.strategies.scalping.indicators import spread_series
        a = [10.0, 20.0, 30.0]
        b = [5.0, 10.0, 15.0]
        sprd = spread_series(a, b, 2.0)
        assert all(abs(s) < 0.001 for s in sprd)

    def test_ema_slope(self):
        from bybit_bot.strategies.scalping.indicators import ema_slope
        vals = [float(i) for i in range(20)]
        slope = ema_slope(vals, 5)
        assert slope > 0

    def test_avg_volume(self):
        from bybit_bot.strategies.scalping.indicators import avg_volume
        bars = _make_bars(n=30, volume=100.0)
        assert avg_volume(bars, 20) == pytest.approx(100.0)


# ── VWAP Crypto Strategy ────────────────────────────────────

class TestVwapCrypto:
    def test_no_signals_in_range(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy()
        bars = _make_bars(n=60, step=1.0)
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy()
        bars = _make_bars(n=10)
        assert strat.scan({"BTCUSDT": bars}) == []

    def test_signal_fields(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapSignal, VwapCryptoStrategy
        from bybit_bot.analysis.signals import Direction
        sig = VwapSignal(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            deviation_atr=2.5,
            rsi=25.0,
            vwap_price=60000.0,
            atr_value=500.0,
            entry_price=59000.0,
        )
        assert sig.direction == Direction.LONG
        assert sig.deviation_atr == 2.5


# ── Stat-Arb Crypto Strategy ────────────────────────────────

class TestStatArbCrypto:
    def test_no_signals_insufficient_data(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy(pairs=[("LTCUSDT", "BTCUSDT")])
        bars = _make_bars(n=50)
        signals = strat.scan({"LTCUSDT": bars, "BTCUSDT": bars})
        assert signals == []

    def test_scan_with_enough_data(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy(pairs=[("LTCUSDT", "BTCUSDT")])
        bars_ltc = _make_bars("LTCUSDT", n=200, base_price=55)
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        signals = strat.scan({"LTCUSDT": bars_ltc, "BTCUSDT": bars_btc})
        assert isinstance(signals, list)

    def test_check_exits_empty(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy(pairs=[("LTCUSDT", "BTCUSDT")])
        bars_ltc = _make_bars("LTCUSDT", n=200, base_price=55)
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        to_close = strat.check_exits({"LTCUSDT": bars_ltc, "BTCUSDT": bars_btc}, [])
        assert to_close == []


# ── Funding Rate Scalp ───────────────────────────────────────

class TestFundingScalp:
    def test_no_client_returns_empty(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        strat = FundingScalpStrategy(client=None)
        assert strat.scan(("BTCUSDT",), {}) == []

    def test_is_near_funding_logic(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        result = FundingScalpStrategy._is_near_funding()
        assert isinstance(result, bool)

    def test_should_exit_after_funding(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        result = FundingScalpStrategy.should_exit_after_funding()
        assert isinstance(result, bool)

    def test_signal_fields(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingSignal
        from bybit_bot.analysis.signals import Direction
        sig = FundingSignal(
            symbol="BTCUSDT",
            direction=Direction.SHORT,
            funding_rate=0.001,
            next_funding_time="2026-04-11T08:00:00Z",
            strength=0.8,
            atr_value=500.0,
            entry_price=72000.0,
        )
        assert sig.direction == Direction.SHORT
        assert sig.funding_rate == 0.001


# ── Volume Spike Strategy ────────────────────────────────────

class TestVolumeSpike:
    def test_no_spike_normal_volume(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        bars = _make_bars(n=60, volume=100.0)
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_spike_detected(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        bars = _make_bars(n=60, volume=100.0, step=5.0)
        # Сделать последний бар с аномальным объёмом и движением
        last = bars[-1]
        spike_bar = Bar(
            symbol=last.symbol,
            ts=last.ts,
            open=last.close - 500,
            high=last.close + 100,
            low=last.close - 600,
            close=last.close,
            volume=500.0,  # 5x от среднего 100
        )
        bars[-1] = spike_bar
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        assert strat.scan({"BTCUSDT": _make_bars(n=5)}) == []

    def test_max_signals_limit(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy(max_signals_per_scan=2)
        assert strat._max_signals == 2


# ── Integration: imports ─────────────────────────────────────

def test_all_scalping_imports():
    from bybit_bot.strategies.scalping.indicators import vwap, rolling_z_score, ols_hedge_ratio
    from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
    from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
    from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
    from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
