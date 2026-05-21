"""Тесты индикаторов AI Arena (Nof1 layout).

Берём монотонные / равномерные / известные edge-case ряды. Никакой
подгонки под результат — все ожидания вычислимы аналитически.
"""
from __future__ import annotations

import pytest

from ai_arena.analysis.indicators import (
    atr,
    build_intraday_snapshot,
    build_longer_term_snapshot,
    ema,
    macd,
    macd_series,
    rsi,
    rsi_series,
    sma,
    true_ranges,
    volume_avg,
)


class TestBasicHelpers:
    def test_sma_basic(self):
        assert sma([1, 2, 3, 4, 5], 5) == 3.0
        assert sma([1, 2, 3, 4, 5], 3) == 4.0
        assert sma([1, 2], 5) is None

    def test_ema_constant_series(self):
        assert ema([10.0] * 50, 14) == pytest.approx(10.0)

    def test_ema_short_series(self):
        assert ema([1.0, 2.0], 14) is None


class TestRsi:
    def test_rsi_all_gains(self):
        closes = [float(x) for x in range(1, 30)]
        assert rsi(closes, 14) == pytest.approx(100.0)

    def test_rsi_all_losses(self):
        closes = [float(x) for x in range(30, 1, -1)]
        assert rsi(closes, 14) == pytest.approx(0.0)

    def test_rsi_period_7_intraday(self):
        # На стабильно растущем ряду — RSI(7) тоже 100
        closes = [100.0 + i for i in range(20)]
        assert rsi(closes, 7) == pytest.approx(100.0)

    def test_rsi_short_series(self):
        assert rsi([1.0, 2.0, 3.0], 14) is None

    def test_rsi_series_length(self):
        n = 30
        closes = [100.0 + (i % 5) for i in range(n)]
        s = rsi_series(closes, 14)
        assert len(s) == n
        # Первые period значений должны быть None
        assert s[0] is None
        # Последнее должно быть числом в (0, 100)
        assert isinstance(s[-1], float)
        assert 0 < s[-1] < 100


class TestMacd:
    def test_macd_constant_series(self):
        # На постоянном ряду MACD ≈ 0
        m, sig, h = macd([10.0] * 60)
        assert m == pytest.approx(0.0, abs=1e-6)
        assert sig == pytest.approx(0.0, abs=1e-6)
        assert h == pytest.approx(0.0, abs=1e-6)

    def test_macd_short_series(self):
        m, sig, h = macd([1.0] * 10)
        assert (m, sig, h) == (None, None, None)

    def test_macd_series_length(self):
        closes = [100.0 + i for i in range(80)]
        m, s, h = macd_series(closes)
        assert len(m) == 80
        assert len(s) == 80
        assert len(h) == 80
        # На монотонно растущем — последний macd > 0
        assert m[-1] is not None and m[-1] > 0


class TestAtr:
    def test_atr_constant_no_volatility(self):
        # Все свечи с одинаковыми high/low/close → TR=0 → ATR=0
        n = 30
        result = atr([100.0] * n, [100.0] * n, [100.0] * n, 14)
        assert result == pytest.approx(0.0)

    def test_atr_known_range(self):
        # Каждая свеча range = 10 → TR = 10 → ATR = 10
        n = 30
        highs = [100.0 + i for i in range(n)]
        lows = [h - 10 for h in highs]
        closes = highs.copy()
        result = atr(highs, lows, closes, 14)
        # Из-за gap'ов (high[i] - close[i-1]) может быть >10, но не сильно
        assert result is not None
        assert result >= 9.5

    def test_atr_period_3_vs_14(self):
        n = 50
        highs = [100.0 + 0.1 * i for i in range(n)]
        lows = [h - 1 for h in highs]
        closes = highs.copy()
        a3 = atr(highs, lows, closes, 3)
        a14 = atr(highs, lows, closes, 14)
        assert a3 is not None and a14 is not None

    def test_true_ranges_length(self):
        highs = [10.0, 11.0, 12.0]
        lows = [9.0, 10.0, 11.0]
        closes = [9.5, 10.5, 11.5]
        trs = true_ranges(highs, lows, closes)
        assert len(trs) == 2  # n-1


class TestVolumeAvg:
    def test_volume_avg_basic(self):
        assert volume_avg([100.0] * 20, 20) == pytest.approx(100.0)
        assert volume_avg([100.0] * 10, 20) is None

    def test_volume_avg_known(self):
        vols = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert volume_avg(vols, 5) == pytest.approx(30.0)


class TestSnapshotBuilders:
    def test_intraday_snapshot_take_n(self):
        closes = [100.0 + i * 0.1 for i in range(50)]
        snap = build_intraday_snapshot(closes, take_n=10)
        assert len(snap.prices) == 10
        assert len(snap.ema20) == 10
        assert len(snap.macd) == 10
        assert len(snap.rsi7) == 10
        assert len(snap.rsi14) == 10
        # Последняя цена = последняя из ряда
        assert snap.prices[-1] == pytest.approx(closes[-1])

    def test_intraday_short_history_pads_none(self):
        closes = [100.0 + i for i in range(15)]
        snap = build_intraday_snapshot(closes, take_n=10)
        assert len(snap.ema20) == 10
        # MACD требует ≥35 точек — на 15 быть None
        assert all(v is None for v in snap.macd)

    def test_intraday_display_prices_override_used_for_prices(self):
        """v2.x bug-fix: ``display_prices`` (OHLC4) подставляется в ``prices``,
        индикаторы продолжают считаться по close (canonical)."""
        closes = [100.0 + i * 0.1 for i in range(50)]
        ohlc4 = [c + 0.05 for c in closes]  # имитация OHLC4 ≈ close + 0.05
        snap = build_intraday_snapshot(closes, take_n=10, display_prices=ohlc4)
        # display prices — это OHLC4, не closes
        assert snap.prices == ohlc4[-10:]
        # индикаторы РАВНЫ тем что были бы без display_prices (canonical close-base)
        snap_close = build_intraday_snapshot(closes, take_n=10)
        assert snap.ema20 == snap_close.ema20
        assert snap.macd == snap_close.macd
        assert snap.rsi7 == snap_close.rsi7
        assert snap.rsi14 == snap_close.rsi14

    def test_intraday_no_display_prices_falls_back_to_closes(self):
        """Backward compat: если display_prices не передан — prices=closes."""
        closes = [100.0 + i * 0.1 for i in range(50)]
        snap = build_intraday_snapshot(closes, take_n=10)  # без display_prices
        assert snap.prices == closes[-10:]

    def test_intraday_display_prices_short_history_pads_zeros(self):
        """display_prices короче take_n — pad-нулями слева, как fallback."""
        closes = [100.0 + i * 0.1 for i in range(50)]
        ohlc4_short = [200.0, 201.0, 202.0]  # длина 3
        snap = build_intraday_snapshot(closes, take_n=10, display_prices=ohlc4_short)
        # длина prices == 10, последние 3 = ohlc4_short, первые 7 = 0.0
        assert len(snap.prices) == 10
        assert snap.prices[-3:] == [200.0, 201.0, 202.0]
        assert snap.prices[:7] == [0.0] * 7

    def test_longer_term_snapshot(self):
        n = 60
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows = [h - 1 for h in highs]
        closes = highs.copy()
        vols = [1000.0 + i for i in range(n)]
        snap = build_longer_term_snapshot(highs, lows, closes, vols, take_n=10)
        assert snap.ema20 is not None
        assert snap.ema50 is not None
        assert snap.atr3 is not None
        assert snap.atr14 is not None
        assert snap.volume_current == vols[-1]
        assert len(snap.macd) == 10
        assert len(snap.rsi14) == 10
