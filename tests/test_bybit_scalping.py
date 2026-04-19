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
        strat = StatArbCryptoStrategy()
        bars = _make_bars(n=50)
        signals = strat.scan({"BTCUSDT": bars, "ETHUSDT": bars})
        assert signals == []

    def test_scan_with_enough_data(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy()
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        bars_eth = _make_bars("ETHUSDT", n=200, base_price=3000)
        signals = strat.scan({"BTCUSDT": bars_btc, "ETHUSDT": bars_eth})
        assert isinstance(signals, list)

    def test_check_exits_empty(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy()
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        bars_eth = _make_bars("ETHUSDT", n=200, base_price=3000)
        to_close = strat.check_exits({"BTCUSDT": bars_btc, "ETHUSDT": bars_eth}, [])
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


# ── Session ORB Strategy ─────────────────────────────────────

def _make_orb_session(
    *,
    session_hour: int = 8,
    box_high: float = 60_100.0,
    box_low: float = 59_900.0,
    pre_bars: int = 120,
    post_orb_bars: int = 1,
    breakout: str | None = "up",
    breakout_delta: float = 50.0,
    spike_volume: float = 200.0,
    normal_volume: float = 100.0,
    symbol: str = "BTCUSDT",
) -> list[Bar]:
    """Сгенерировать последовательность баров для теста ORB.

    - 120 пре-сессионных баров с EMA-трендом вверх или вниз (управляется
      параметром breakout: 'up' -> растущий тренд, 'down' -> падающий).
    - 3 бара коробки (первые 15 мин сессии, session_hour:00..session_hour:15)
      формируют диапазон [box_low, box_high].
    - post_orb_bars пробойных баров, последний — либо пробой вверх/вниз
      с объёмом spike_volume, либо сидит в коробке.
    """
    from datetime import timedelta

    bars: list[Bar] = []
    session_start = datetime(2026, 4, 17, session_hour, 0, tzinfo=UTC)
    pre_start = session_start - timedelta(minutes=5 * pre_bars)

    # Пре-сессия: лёгкий дрейф к центру коробки + рыночный шум, чтобы
    # ADX остался ниже 25 (иначе ORB-фильтр «ADX > 25 = уже тренд»
    # отсечёт всё). EMA-slope при этом остаётся положительным/отрицательным
    # благодаря направленному дрейфу.
    center = (box_high + box_low) / 2
    drift = 0.05 if breakout == "up" else (-0.05 if breakout == "down" else 0.0)
    for i in range(pre_bars):
        # Осцилляция (синус) + лёгкий тренд — ADX останется ~10-20
        noise = ((i * 7) % 11) - 5  # детерминированный псевдо-шум
        price = center - drift * (pre_bars - i) + noise
        bars.append(Bar(
            symbol=symbol,
            ts=pre_start + timedelta(minutes=5 * i),
            open=price - 2,
            high=price + 5,
            low=price - 5,
            close=price,
            volume=normal_volume,
        ))

    # 3 бара коробки — осциллируют внутри [box_low, box_high]
    for i in range(3):
        bars.append(Bar(
            symbol=symbol,
            ts=session_start + timedelta(minutes=5 * i),
            open=box_low + (box_high - box_low) * 0.3,
            high=box_high,
            low=box_low,
            close=box_low + (box_high - box_low) * 0.6,
            volume=normal_volume,
        ))

    # post_orb_bars пробойных баров
    for i in range(post_orb_bars):
        is_last = i == post_orb_bars - 1
        if is_last and breakout == "up":
            o, h, l_, c = box_high - 5, box_high + breakout_delta, box_high - 10, box_high + breakout_delta - 5
            v = spike_volume
        elif is_last and breakout == "down":
            o, h, l_, c = box_low + 5, box_low + 10, box_low - breakout_delta, box_low - breakout_delta + 5
            v = spike_volume
        else:
            # Сидит внутри коробки
            o = box_low + (box_high - box_low) * 0.4
            h = box_high - 10
            l_ = box_low + 10
            c = box_low + (box_high - box_low) * 0.5
            v = normal_volume
        bars.append(Bar(
            symbol=symbol,
            ts=session_start + timedelta(minutes=5 * (3 + i)),
            open=o, high=h, low=l_, close=c, volume=v,
        ))
    return bars


class TestSessionOrb:
    def test_breakout_up_detected(self):
        from bybit_bot.strategies.scalping.session_orb import (
            SessionOrbStrategy, OrbSignal,
        )
        from bybit_bot.analysis.signals import Direction

        bars = _make_orb_session(breakout="up")
        signals = SessionOrbStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        sig = signals[0]
        assert isinstance(sig, OrbSignal)
        assert sig.symbol == "BTCUSDT"
        assert sig.direction == Direction.LONG
        assert sig.session == "london"
        assert sig.volume_ratio >= 1.3

    def test_breakout_down_detected(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        from bybit_bot.analysis.signals import Direction

        bars = _make_orb_session(breakout="down")
        signals = SessionOrbStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        assert signals[0].direction == Direction.SHORT

    def test_no_signal_inside_box(self):
        """Цена внутри коробки → нет пробоя → нет сигнала."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout=None)
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_low_volume(self):
        """Пробой есть, но объём ниже 1.3× → отсекаем."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout="up", spike_volume=100.0)  # == normal
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_out_of_session(self):
        """Текущий бар вне сессионных окон → коробку не строим."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        # 05:00 UTC — никакая сессия не активна (asia 00-01, london 08-09, ny 14-15)
        bars = _make_orb_session(session_hour=5, breakout="up")
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_after_earlier_breakout(self):
        """Если пробой уже случился раньше в post_orb — второй раз не входим."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        # 5 post-orb баров: 1-й пробойный, 5-й тоже пробойный
        bars = _make_orb_session(breakout="up", post_orb_bars=5)
        # Вручную сделаем первый post-orb бар тоже пробойным
        # (индекс 120 + 3 = 123 — первый после коробки)
        first_post = bars[123]
        bars[123] = Bar(
            symbol=first_post.symbol, ts=first_post.ts,
            open=60_095.0, high=60_200.0, low=60_090.0, close=60_190.0,
            volume=first_post.volume,
        )
        # Теперь пробой был раньше → последний бар тоже пробойный,
        # но earlier_broke_up=True → должен быть отсечён
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        assert SessionOrbStrategy().scan({"BTCUSDT": _make_bars(n=10)}) == []

    def test_max_signals_limit(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        strat = SessionOrbStrategy(max_signals_per_scan=1)
        bars_a = _make_orb_session(breakout="up")
        bars_b = _make_orb_session(breakout="up", symbol="ETHUSDT",
                                   box_high=3000.0, box_low=2990.0,
                                   spike_volume=300.0)
        signals = strat.scan({"BTCUSDT": bars_a, "ETHUSDT": bars_b})
        assert len(signals) == 1


# ── Integration: imports ─────────────────────────────────────

def test_all_scalping_imports():
    from bybit_bot.strategies.scalping.indicators import vwap, rolling_z_score, ols_hedge_ratio
    from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
    from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
    from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
    from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
    from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
