"""Тесты для скальпинг-стратегий: индикаторы + Gold ORB."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import (
    avg_volume,
    ema_slope,
    ols_hedge_ratio,
    resample_m5_to_h4,
    rolling_z_score,
    session_range,
    spread_series,
    vwap,
    vwap_series,
    z_score_series,
)
from fx_pro_bot.strategies.scalping.gold_orb import (
    GOLD_ORB_INSTRUMENT,
    GOLD_ORB_SOURCE,
    GOLD_ORB_TP_ATR_MULT,
    GoldOrbSignal,
    GoldOrbStrategy,
    SL_ATR_MULT as GOLD_ORB_SL_ATR_MULT,
)
from fx_pro_bot.strategies.scalping.squeeze_h4 import (
    BB_K,
    BB_N,
    KC_MULT,
    MIN_SQUEEZE_BARS,
    SMA_N,
    SQUEEZE_H4_SOURCE,
    SQUEEZE_H4_SYMBOLS,
    ATR_STOP_MULT as SQUEEZE_SL_MULT,
    SqueezeH4Signal,
    SqueezeH4Strategy,
)
from fx_pro_bot.strategies.scalping.turtle_h4 import (
    ENTRY_LOOKBACK_H4,
    EXIT_LOOKBACK_H4,
    MAX_HOLD_H4,
    TURTLE_H4_SOURCE,
    TURTLE_H4_SYMBOLS,
    ATR_STOP_MULT as TURTLE_SL_MULT,
    TurtleH4Signal,
    TurtleH4Strategy,
)
from fx_pro_bot.strategies.scalping.gbpjpy_fade import (
    COOLOFF_HOURS,
    ENTRY_DELAY_M5,
    GBPJPY_FADE_SOURCE,
    GBPJPY_FADE_SYMBOL,
    GBPJPY_FADE_TRIGGER,
    RETURN_WINDOW_M5,
    STD_WINDOW_M5,
    TRIGGER_SIGMA,
    GbpjpyFadeSignal,
    GbpjpyFadeStrategy,
)


def _make_bars(
    closes: list[float],
    instrument: str = "EURUSD=X",
    *,
    volumes: list[float] | None = None,
    base_ts: datetime | None = None,
    interval_min: int = 5,
) -> list[Bar]:
    inst = InstrumentId(symbol=instrument)
    base = base_ts or datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
    vols = volumes or [100.0] * len(closes)
    return [
        Bar(
            instrument=inst,
            ts=base + timedelta(minutes=interval_min * i),
            open=c - 0.0001,
            high=c + 0.0005,
            low=c - 0.0005,
            close=c,
            volume=vols[i] if i < len(vols) else 100.0,
        )
        for i, c in enumerate(closes)
    ]


def _store(tmp_path) -> StatsStore:
    return StatsStore(tmp_path / "test.sqlite")


# ── Indicators ──────────────────────────────────────────────


class TestVwap:
    def test_basic_vwap(self):
        bars = _make_bars([1.1000, 1.1010, 1.1020], volumes=[100, 200, 300])
        result = vwap(bars)
        expected_num = (
            (1.1000 + 0.0005 - 0.0005) / 3 * 100 +  # typical = (h+l+c)/3
            (1.1010 + 0.0005 - 0.0005) / 3 * 200 +
            (1.1020 + 0.0005 - 0.0005) / 3 * 300
        )
        assert result > 0
        assert 1.0990 < result < 1.1030

    def test_vwap_zero_volume_fallback(self):
        bars = _make_bars([1.1000, 1.1010, 1.1020], volumes=[0, 0, 0])
        result = vwap(bars)
        expected = (1.1000 + 1.1010 + 1.1020) / 3
        assert abs(result - expected) < 1e-6

    def test_vwap_empty(self):
        assert vwap([]) == 0.0

    def test_vwap_series_length(self):
        bars = _make_bars([1.1 + i * 0.001 for i in range(10)])
        series = vwap_series(bars)
        assert len(series) == 10
        assert all(s > 0 for s in series)


class TestZScore:
    def test_rolling_z_score_normal(self):
        values = [100.0] * 50 + [110.0]
        z = rolling_z_score(values, 50)
        assert z > 2.0

    def test_rolling_z_score_zero_std(self):
        values = [1.0] * 50
        z = rolling_z_score(values, 50)
        assert z == 0.0

    def test_rolling_z_score_insufficient_data(self):
        z = rolling_z_score([1.0, 2.0], 10)
        assert z == 0.0

    def test_z_score_series_length(self):
        values = list(range(100))
        series = z_score_series([float(v) for v in values], 20)
        assert len(series) == 100
        assert all(s == 0.0 for s in series[:19])


class TestSessionRange:
    def test_basic_range(self):
        bars = _make_bars([1.10, 1.12, 1.08, 1.11, 1.09])
        high, low = session_range(bars, 3)
        assert high == pytest.approx(1.12 + 0.0005, abs=1e-6)
        assert low == pytest.approx(1.08 - 0.0005, abs=1e-6)

    def test_empty_bars(self):
        assert session_range([], 3) == (0.0, 0.0)

    def test_fewer_than_n(self):
        bars = _make_bars([1.10, 1.12])
        high, low = session_range(bars, 5)
        assert high > 0 and low > 0


class TestEmaSlope:
    def test_upward_slope(self):
        vals = [float(i) for i in range(20)]
        s = ema_slope(vals, 5)
        assert s > 0

    def test_flat_slope(self):
        vals = [5.0] * 20
        s = ema_slope(vals, 5)
        assert s == 0.0

    def test_insufficient_data(self):
        s = ema_slope([1.0, 2.0], 5)
        assert s == 0.0


class TestOlsHedgeRatio:
    def test_perfect_correlation(self):
        a = [float(i) for i in range(100)]
        b = [float(i) * 2 for i in range(100)]
        beta = ols_hedge_ratio(a, b)
        assert abs(beta - 0.5) < 0.01

    def test_insufficient_data(self):
        beta = ols_hedge_ratio([1.0, 2.0], [3.0, 4.0])
        assert beta == 1.0


class TestSpreadSeries:
    def test_basic_spread(self):
        a = [10.0, 20.0, 30.0]
        b = [5.0, 10.0, 15.0]
        beta = 2.0
        result = spread_series(a, b, beta)
        assert all(abs(v) < 1e-10 for v in result)


class TestAvgVolume:
    def test_basic(self):
        bars = _make_bars([1.1] * 30, volumes=[float(i) for i in range(30)])
        avg = avg_volume(bars, 10)
        expected = sum(range(20, 30)) / 10
        assert abs(avg - expected) < 1e-6


class TestGoldOrbStrategy:
    """Тесты для Gold ORB Isolated стратегии (XAU/USD, touch-break, без ADX)."""

    def _gold_bars(self, n: int = 60, *, base_hour: int = 8, close_base: float = 2000.0):
        """Gold-like bars: 3 бара-коробка [1999..2001], остальные после пробоя."""
        base = datetime(2026, 3, 28, base_hour, 0, tzinfo=UTC)
        closes = [close_base + (i % 3) for i in range(n)]
        return [
            Bar(
                instrument=InstrumentId(symbol=GOLD_ORB_INSTRUMENT),
                ts=base + timedelta(minutes=5 * i),
                open=c, high=c + 0.5, low=c - 0.5, close=c, volume=100.0,
            )
            for i, c in enumerate(closes)
        ]

    def test_constants_match_backtest(self):
        """Параметры SL=1.5×ATR, TP=3×ATR зафиксированы по robustness grid (90d)."""
        assert GOLD_ORB_SL_ATR_MULT == 1.5
        assert GOLD_ORB_TP_ATR_MULT == 3.0
        assert GOLD_ORB_INSTRUMENT == "GC=F"

    def test_scan_no_crash_xau(self, tmp_path):
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store)
        bars = self._gold_bars()
        signals = strat.scan({GOLD_ORB_INSTRUMENT: bars}, {GOLD_ORB_INSTRUMENT: 2001.0})
        assert isinstance(signals, list)

    def test_scan_only_gc_f(self, tmp_path):
        """Стратегия торгует ТОЛЬКО XAU/USD, даже если в bars_map есть другие инструменты."""
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store)
        bars = _make_bars([1.1000] * 60)
        signals = strat.scan({"EURUSD=X": bars}, {"EURUSD=X": 1.1005})
        assert signals == []

    def test_touch_break_long_no_confirm_required(self, tmp_path):
        """Ключевое отличие от session_orb: touch-break (high>box_high, без close)."""
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store)
        # 50 исторических баров ДО сессии (warm-up для ATR/EMA) — эндуем в 08:00
        base_pre = datetime(2026, 3, 27, 20, 0, tzinfo=UTC)  # за 12h до London
        inst = InstrumentId(symbol=GOLD_ORB_INSTRUMENT)
        closes_pre = [2000.0 + 0.1 * (i % 5) for i in range(140)]  # uptrend-ish
        pre_bars = [
            Bar(instrument=inst, ts=base_pre + timedelta(minutes=5 * i),
                open=c, high=c + 0.3, low=c - 0.3, close=c, volume=100.0)
            for i, c in enumerate(closes_pre)
        ]
        # Session bars: 08:00 UTC, 3 бара коробки [1999..2001], потом пробой
        session_start = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
        box_bars = []
        for i, c in enumerate([2000.0, 2000.5, 2001.0]):
            box_bars.append(Bar(instrument=inst,
                                ts=session_start + timedelta(minutes=5 * i),
                                open=c, high=c + 0.3, low=c - 0.3,
                                close=c, volume=100.0))
        # 4-й бар (08:15 UTC): high пробивает box, close ВНУТРИ
        breakout_bar = Bar(instrument=inst,
                           ts=session_start + timedelta(minutes=15),
                           open=2001.0, high=2010.0, low=2000.5,
                           close=2001.0, volume=100.0)
        bars = pre_bars + box_bars + [breakout_bar]
        signals = strat.scan({GOLD_ORB_INSTRUMENT: bars}, {GOLD_ORB_INSTRUMENT: 2005.0})
        assert any(s.direction == TrendDirection.LONG for s in signals), (
            "touch-break: high>box_high должен давать LONG без confirm-bar"
        )

    def test_process_signals_opens_in_live(self, tmp_path):
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store, shadow=False)
        sig = GoldOrbSignal(
            instrument=GOLD_ORB_INSTRUMENT, direction=TrendDirection.LONG,
            source=GOLD_ORB_SOURCE, entry_level=2001.0,
            box_high=2001.0, box_low=1999.0, atr=5.0,
            session="london", detail="test",
        )
        opened = strat.process_signals([sig], {GOLD_ORB_INSTRUMENT: 2002.0})
        assert opened == 1
        assert store.count_open_positions(strategy="gold_orb") == 1

    def test_shadow_mode_no_open(self, tmp_path):
        """Shadow mode: сигналы только логируются, БД/брокер не затрагиваются."""
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store, shadow=True)
        sig = GoldOrbSignal(
            instrument=GOLD_ORB_INSTRUMENT, direction=TrendDirection.LONG,
            source=GOLD_ORB_SOURCE, entry_level=2001.0,
            box_high=2001.0, box_low=1999.0, atr=5.0,
            session="london", detail="test",
        )
        opened = strat.process_signals([sig], {GOLD_ORB_INSTRUMENT: 2002.0})
        assert opened == 1   # счётчик увеличивается (для логов)
        assert store.count_open_positions(strategy="gold_orb") == 0   # но в БД пусто

    def test_max_positions_respected(self, tmp_path):
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store, max_positions=1, max_per_instrument=1)
        sig = GoldOrbSignal(
            instrument=GOLD_ORB_INSTRUMENT, direction=TrendDirection.LONG,
            source=GOLD_ORB_SOURCE, entry_level=2001.0,
            box_high=2001.0, box_low=1999.0, atr=5.0,
            session="london", detail="test",
        )
        strat.process_signals([sig], {GOLD_ORB_INSTRUMENT: 2002.0})
        opened2 = strat.process_signals([sig], {GOLD_ORB_INSTRUMENT: 2002.0})
        assert opened2 == 0

    def test_session_detection_london(self):
        base_london = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        bars = [
            Bar(instrument=InstrumentId(symbol=GOLD_ORB_INSTRUMENT),
                ts=base_london + timedelta(minutes=5 * i),
                open=2000.0, high=2000.5, low=1999.5, close=2000.0, volume=100.0)
            for i in range(20)
        ]
        result, tag = GoldOrbStrategy._get_session_bars(bars)
        assert len(result) > 0
        assert tag == "london"

    def test_session_detection_off_session(self):
        base_off = datetime(2026, 3, 28, 3, 0, tzinfo=UTC)
        bars = [
            Bar(instrument=InstrumentId(symbol=GOLD_ORB_INSTRUMENT),
                ts=base_off + timedelta(minutes=5 * i),
                open=2000.0, high=2000.5, low=1999.5, close=2000.0, volume=100.0)
            for i in range(20)
        ]
        result, tag = GoldOrbStrategy._get_session_bars(bars)
        assert result == []
        assert tag == ""

    def test_contra_trend_blocked(self, tmp_path):
        """EMA-slope фильтр: LONG блокируется если slope<0."""
        store = _store(tmp_path)
        strat = GoldOrbStrategy(store)
        base = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
        # Даунтренд: цена падает от 2100 к 2000
        closes = [2100.0 - i * 2 for i in range(60)]
        bars = [
            Bar(instrument=InstrumentId(symbol=GOLD_ORB_INSTRUMENT),
                ts=base + timedelta(minutes=5 * i),
                open=c, high=c + 0.5, low=c - 0.5, close=c, volume=100.0)
            for i, c in enumerate(closes)
        ]
        # Последний бар: high пробивает коробку вверх (contra-trend)
        last = bars[-1]
        bars[-1] = Bar(instrument=last.instrument, ts=last.ts, open=last.open,
                       high=max(b.high for b in bars) + 10, low=last.low,
                       close=last.close, volume=last.volume)
        signals = strat.scan({GOLD_ORB_INSTRUMENT: bars}, {GOLD_ORB_INSTRUMENT: last.close})
        # LONG-сигнала быть не должно, т.к. slope<0
        longs = [s for s in signals if s.direction == TrendDirection.LONG]
        assert longs == []


# ── M5 → H4 resample ────────────────────────────────────────


class TestResampleH4:
    def test_empty(self):
        assert resample_m5_to_h4([]) == []

    def test_bucket_boundaries(self):
        """Бары 00:00-03:55 UTC → один H4 бар, 04:00-07:55 → другой."""
        base = datetime(2026, 3, 28, 0, 0, tzinfo=UTC)
        inst = InstrumentId(symbol="GC=F")
        bars: list[Bar] = []
        for i in range(96):   # 8 часов × 12 M5 bars = 96
            ts = base + timedelta(minutes=5 * i)
            bars.append(Bar(
                instrument=inst, ts=ts,
                open=2000.0 + i * 0.1, high=2001.0 + i * 0.1,
                low=1999.0 + i * 0.1, close=2000.5 + i * 0.1, volume=100,
            ))
        h4 = resample_m5_to_h4(bars)
        assert len(h4) == 2   # 00:00-03:55 и 04:00-07:55
        # Первый бар: high = max первых 48 баров
        first = h4[0]
        assert first.open == pytest.approx(2000.0)
        assert first.high == pytest.approx(2001.0 + 47 * 0.1)


# ── Squeeze H4 ──────────────────────────────────────────────


class TestSqueezeH4:
    """TTM Squeeze на commodities (GC=F, BZ=F)."""

    def test_constants(self):
        assert SQUEEZE_H4_SYMBOLS == ("GC=F", "BZ=F")
        assert BB_N == 20
        assert BB_K == 2.0
        assert KC_MULT == 1.5
        assert SMA_N == 50
        assert SQUEEZE_SL_MULT == 2.0
        assert MIN_SQUEEZE_BARS == 3

    def test_scan_empty_no_crash(self, tmp_path):
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store)
        assert strat.scan({}, {}) == []

    def test_scan_insufficient_data(self, tmp_path):
        """Нужно ≥55 H4 баров = 2640 M5."""
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store)
        bars = _make_bars([2000.0] * 100, instrument="GC=F")
        assert strat.scan({"GC=F": bars}, {"GC=F": 2000.0}) == []

    def test_scan_skips_non_commodity(self, tmp_path):
        """Стратегия не сканирует FX-пары, даже если они в bars_map."""
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store)
        # Много M5 баров EURUSD — но GC=F нет → нет сигналов
        bars = _make_bars([1.10] * 3000, instrument="EURUSD=X")
        signals = strat.scan({"EURUSD=X": bars}, {"EURUSD=X": 1.10})
        assert signals == []

    def test_process_signals_opens_in_live(self, tmp_path):
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store, shadow=False)
        sig = SqueezeH4Signal(
            instrument="GC=F", direction=TrendDirection.LONG,
            source=SQUEEZE_H4_SOURCE, entry_level=2050.0,
            sma50=2040.0, atr=5.0, squeeze_count=5, detail="test",
        )
        opened = strat.process_signals([sig], {"GC=F": 2050.0})
        assert opened == 1
        assert store.count_open_positions(strategy="squeeze_h4") == 1

    def test_shadow_mode_no_open(self, tmp_path):
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store, shadow=True)
        sig = SqueezeH4Signal(
            instrument="GC=F", direction=TrendDirection.LONG,
            source=SQUEEZE_H4_SOURCE, entry_level=2050.0,
            sma50=2040.0, atr=5.0, squeeze_count=5, detail="test",
        )
        opened = strat.process_signals([sig], {"GC=F": 2050.0})
        assert opened == 1
        assert store.count_open_positions(strategy="squeeze_h4") == 0

    def test_max_per_instrument_respected(self, tmp_path):
        store = _store(tmp_path)
        strat = SqueezeH4Strategy(store, max_per_instrument=1)
        sig = SqueezeH4Signal(
            instrument="GC=F", direction=TrendDirection.LONG,
            source=SQUEEZE_H4_SOURCE, entry_level=2050.0,
            sma50=2040.0, atr=5.0, squeeze_count=5, detail="test",
        )
        strat.process_signals([sig], {"GC=F": 2050.0})
        opened2 = strat.process_signals([sig], {"GC=F": 2050.0})
        assert opened2 == 0


# ── Turtle H4 ──────────────────────────────────────────────


class TestTurtleH4:
    """20-day breakout на commodities."""

    def test_constants(self):
        assert TURTLE_H4_SYMBOLS == ("GC=F", "BZ=F")
        assert ENTRY_LOOKBACK_H4 == 120
        assert EXIT_LOOKBACK_H4 == 60
        assert TURTLE_SL_MULT == 2.0
        assert MAX_HOLD_H4 == 180

    def test_scan_empty_no_crash(self, tmp_path):
        store = _store(tmp_path)
        strat = TurtleH4Strategy(store)
        assert strat.scan({}, {}) == []

    def test_scan_insufficient_data(self, tmp_path):
        store = _store(tmp_path)
        strat = TurtleH4Strategy(store)
        bars = _make_bars([2000.0] * 500, instrument="GC=F")   # 500 M5 < 120 H4
        assert strat.scan({"GC=F": bars}, {"GC=F": 2000.0}) == []

    def test_scan_skips_fx(self, tmp_path):
        """FX-пары не торгуются Turtle."""
        store = _store(tmp_path)
        strat = TurtleH4Strategy(store)
        bars = _make_bars([1.10] * 8000, instrument="EURUSD=X")
        signals = strat.scan({"EURUSD=X": bars}, {"EURUSD=X": 1.10})
        assert signals == []

    def test_process_signals_opens_in_live(self, tmp_path):
        store = _store(tmp_path)
        strat = TurtleH4Strategy(store, shadow=False)
        sig = TurtleH4Signal(
            instrument="BZ=F", direction=TrendDirection.LONG,
            source=TURTLE_H4_SOURCE, entry_level=85.0,
            atr=1.2, lookback_high=85.0, lookback_low=75.0, detail="test",
        )
        opened = strat.process_signals([sig], {"BZ=F": 85.1})
        assert opened == 1
        assert store.count_open_positions(strategy="turtle_h4") == 1

    def test_shadow_mode_no_open(self, tmp_path):
        store = _store(tmp_path)
        strat = TurtleH4Strategy(store, shadow=True)
        sig = TurtleH4Signal(
            instrument="GC=F", direction=TrendDirection.LONG,
            source=TURTLE_H4_SOURCE, entry_level=2100.0,
            atr=5.0, lookback_high=2100.0, lookback_low=2000.0, detail="test",
        )
        opened = strat.process_signals([sig], {"GC=F": 2100.0})
        assert opened == 1
        assert store.count_open_positions(strategy="turtle_h4") == 0


# ── GBPJPY Fade ────────────────────────────────────────────


class TestGbpjpyFade:
    """Trigger GBPUSD → fade entry GBPJPY."""

    def test_constants(self):
        assert GBPJPY_FADE_TRIGGER == "GBPUSD=X"
        assert GBPJPY_FADE_SYMBOL == "GBPJPY=X"
        assert RETURN_WINDOW_M5 == 48      # 4h
        assert ENTRY_DELAY_M5 == 12        # 1h
        assert STD_WINDOW_M5 == 30 * 288   # 30 дней
        assert TRIGGER_SIGMA == 2.0
        assert COOLOFF_HOURS == 4.0

    def test_scan_insufficient_data(self, tmp_path):
        store = _store(tmp_path)
        strat = GbpjpyFadeStrategy(store)
        bars = _make_bars([1.25] * 500, instrument="GBPUSD=X")
        signals = strat.scan(
            {"GBPUSD=X": bars, "GBPJPY=X": bars},
            {"GBPUSD=X": 1.25, "GBPJPY=X": 188.0},
        )
        assert signals == []   # < 30d M5 данных

    def test_scan_no_trigger_on_flat(self, tmp_path):
        """На плоской цене GBPUSD z-score должен быть 0 → нет сигнала."""
        store = _store(tmp_path)
        strat = GbpjpyFadeStrategy(store)
        # 30d+ плоских баров
        n = STD_WINDOW_M5 + RETURN_WINDOW_M5 + ENTRY_DELAY_M5 + 10
        flat_bars = _make_bars([1.25] * n, instrument="GBPUSD=X")
        flat_gbpjpy = _make_bars([188.0] * n, instrument="GBPJPY=X")
        signals = strat.scan(
            {"GBPUSD=X": flat_bars, "GBPJPY=X": flat_gbpjpy},
            {"GBPUSD=X": 1.25, "GBPJPY=X": 188.0},
        )
        assert signals == []

    def test_process_signals_fade_direction(self, tmp_path):
        """GBPUSD rally (z>0) → fade = SHORT на GBPJPY."""
        store = _store(tmp_path)
        strat = GbpjpyFadeStrategy(store, shadow=False)
        sig = GbpjpyFadeSignal(
            instrument=GBPJPY_FADE_SYMBOL, direction=TrendDirection.SHORT,
            source=GBPJPY_FADE_SOURCE, entry_level=188.50,
            z_score=2.5, reaction_pips=40.0, sigma_px=0.30, detail="test",
        )
        opened = strat.process_signals([sig], {GBPJPY_FADE_SYMBOL: 188.50})
        assert opened == 1
        assert store.count_open_positions(strategy="gbpjpy_fade") == 1

    def test_shadow_mode_no_open(self, tmp_path):
        store = _store(tmp_path)
        strat = GbpjpyFadeStrategy(store, shadow=True)
        sig = GbpjpyFadeSignal(
            instrument=GBPJPY_FADE_SYMBOL, direction=TrendDirection.LONG,
            source=GBPJPY_FADE_SOURCE, entry_level=188.50,
            z_score=-2.3, reaction_pips=40.0, sigma_px=0.30, detail="test",
        )
        opened = strat.process_signals([sig], {GBPJPY_FADE_SYMBOL: 188.50})
        assert opened == 1
        assert store.count_open_positions(strategy="gbpjpy_fade") == 0

    def test_cooloff_blocks_second_trigger(self, tmp_path):
        """После одного открытия в течение COOLOFF_HOURS второй сигнал не проходит."""
        store = _store(tmp_path)
        strat = GbpjpyFadeStrategy(store, shadow=False)
        # Первый сигнал
        sig = GbpjpyFadeSignal(
            instrument=GBPJPY_FADE_SYMBOL, direction=TrendDirection.LONG,
            source=GBPJPY_FADE_SOURCE, entry_level=188.50,
            z_score=-2.3, reaction_pips=40.0, sigma_px=0.30, detail="test",
        )
        opened1 = strat.process_signals([sig], {GBPJPY_FADE_SYMBOL: 188.50})
        assert opened1 == 1
        # Попытка вернуть второй сигнал через scan() — но scan_bars нет,
        # достаточно убедиться что _is_in_cooloff возвращает True после открытия.
        assert strat._is_in_cooloff()
