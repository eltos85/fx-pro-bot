"""Тесты технических индикаторов AI-Trader.

Сверка с известными reference-значениями. Не подгоняем — берём
монотонные/равномерные ряды, для которых легко посчитать аналитически,
плюс известные edge-кейсы.
"""
from __future__ import annotations

import pytest

from ai_trader.analysis.indicators import (
    atr,
    bollinger,
    compute_snapshot,
    ema,
    format_snapshot,
    macd,
    rsi,
    sma,
    true_ranges,
)


class TestSmaEma:
    def test_sma_basic(self):
        assert sma([1, 2, 3, 4, 5], 5) == 3.0
        assert sma([1, 2, 3, 4, 5], 3) == 4.0  # последние 3: (3+4+5)/3
        assert sma([1, 2], 5) is None

    def test_ema_constant_series(self):
        # На постоянном ряду EMA = тому же значению
        result = ema([10.0] * 50, 14)
        assert result == pytest.approx(10.0)

    def test_ema_returns_none_for_short_series(self):
        assert ema([1.0, 2.0], 14) is None


class TestRsi:
    def test_rsi_all_gains(self):
        # Монотонно растущий ряд → RSI = 100 (нет losses)
        closes = list(range(1, 30))
        result = rsi([float(x) for x in closes], 14)
        assert result == pytest.approx(100.0)

    def test_rsi_all_losses(self):
        # Монотонно падающий → RSI = 0
        closes = list(range(30, 1, -1))
        result = rsi([float(x) for x in closes], 14)
        assert result == pytest.approx(0.0)

    def test_rsi_constant_returns_neutral_or_100(self):
        # Постоянный ряд: ни gain ни loss → avg_loss=0 → формула возвращает 100
        # Это известная "особенность" Wilder RSI на полностью flat-ряду.
        result = rsi([100.0] * 30, 14)
        assert result == pytest.approx(100.0)

    def test_rsi_returns_none_for_short_series(self):
        assert rsi([1.0, 2.0, 3.0], 14) is None

    def test_rsi_in_range(self):
        # Чередующийся ряд должен давать RSI около 50
        closes = []
        v = 100.0
        for i in range(30):
            v += 1 if i % 2 == 0 else -1
            closes.append(v)
        result = rsi(closes, 14)
        assert result is not None
        assert 30 < result < 70


class TestMacd:
    def test_macd_constant_series(self):
        # На постоянном ряду EMA(fast)=EMA(slow) → macd=0
        macd_line, sig, hist = macd([100.0] * 60)
        assert macd_line == pytest.approx(0.0, abs=1e-9)
        assert sig == pytest.approx(0.0, abs=1e-9)
        assert hist == pytest.approx(0.0, abs=1e-9)

    def test_macd_short_series_returns_none(self):
        result = macd([1.0] * 10)
        assert result == (None, None, None)

    def test_macd_uptrend_positive(self):
        # Монотонный рост → MACD line > 0 (fast EMA выше slow EMA).
        # На чисто линейном ряду histogram асимптотически = 0, поэтому его
        # не проверяем — это особенность линейности, не баг.
        closes = [100.0 + i for i in range(60)]
        macd_line, sig, _ = macd(closes)
        assert macd_line is not None and macd_line > 0
        assert sig is not None and sig > 0

    def test_macd_accelerating_uptrend_positive_histogram(self):
        # Ускоряющийся рост (квадратичный) → fast EMA отрывается → hist > 0
        closes = [100.0 + i * i * 0.01 for i in range(60)]
        macd_line, _, hist = macd(closes)
        assert macd_line is not None and macd_line > 0
        assert hist is not None and hist > 0


class TestAtr:
    def test_atr_basic(self):
        # Constant high-low spread, no gaps → ATR = high-low
        highs = [101.0] * 20
        lows = [100.0] * 20
        closes = [100.5] * 20
        result = atr(highs, lows, closes, 14)
        # TR = max(high-low=1, |101-100.5|=0.5, |100-100.5|=0.5) = 1
        assert result == pytest.approx(1.0)

    def test_atr_short_series_returns_none(self):
        assert atr([1.0, 2.0], [1.0, 2.0], [1.0, 2.0], 14) is None

    def test_true_ranges_with_gap(self):
        # Высокий gap вверх: prev_close=10, high=20, low=15
        # TR = max(20-15=5, |20-10|=10, |15-10|=5) = 10
        trs = true_ranges([10, 20], [10, 15], [10, 17])
        assert trs == [10.0]


class TestBollinger:
    def test_bollinger_constant_series(self):
        # Постоянный ряд → std=0 → upper=middle=lower
        u, m, l = bollinger([100.0] * 30, 20, 2.0)
        assert u == pytest.approx(100.0)
        assert m == pytest.approx(100.0)
        assert l == pytest.approx(100.0)

    def test_bollinger_short_returns_none(self):
        assert bollinger([1.0] * 5, 20, 2.0) == (None, None, None)

    def test_bollinger_bands_around_mean(self):
        closes = [100.0, 102.0] * 15  # 30 значений, чередуются
        u, m, l = bollinger(closes, 20, 2.0)
        assert m == pytest.approx(101.0)
        assert u is not None and u > m
        assert l is not None and l < m


class TestSnapshot:
    def test_snapshot_handles_short_data(self):
        snap = compute_snapshot([1.0] * 5, [1.0] * 5, [1.0] * 5)
        # Большинство индикаторов должны быть None при коротких данных
        assert snap.last_close == 1.0
        assert snap.rsi14 is None
        assert snap.atr14 is None
        assert snap.bb_upper is None

    def test_snapshot_full_data(self):
        # 100 свечей равномерного роста
        highs = [100.0 + i + 0.5 for i in range(100)]
        lows = [100.0 + i - 0.5 for i in range(100)]
        closes = [100.0 + i for i in range(100)]
        snap = compute_snapshot(highs, lows, closes)
        assert snap.last_close == 199.0
        assert snap.rsi14 == pytest.approx(100.0)  # все gain
        assert snap.macd_line is not None and snap.macd_line > 0
        assert snap.atr14 is not None and snap.atr14 > 0
        assert snap.ema20 is not None
        assert snap.ema50 is not None
        assert snap.bb_upper is not None and snap.bb_upper > snap.bb_middle
        # bb_position может быть >1 (далеко от средней при тренде)
        assert snap.bb_position is not None

    def test_format_snapshot_returns_string(self):
        snap = compute_snapshot([1.0] * 5, [1.0] * 5, [1.0] * 5)
        s = format_snapshot(snap)
        assert "RSI14=" in s
        assert "MACD=" in s
        assert "ATR14=" in s
        assert "BB(20,2)" in s
        # При нехватке данных должны быть n/a-метки, не падать
        assert "n/a" in s

    def test_format_snapshot_with_full_data_shows_labels(self):
        # uptrend: должна появиться метка [uptrend]
        closes = [100.0 + i for i in range(60)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        snap = compute_snapshot(highs, lows, closes)
        s = format_snapshot(snap)
        assert "[uptrend]" in s
        assert "[OVERBOUGHT]" in s  # RSI=100 на чистом росте
