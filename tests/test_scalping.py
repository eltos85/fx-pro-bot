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
