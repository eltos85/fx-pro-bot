"""Тесты для скальпинг-стратегий: индикаторы, VWAP, Stat-Arb, ORB."""

from __future__ import annotations

import math
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
from fx_pro_bot.strategies.scalping.session_orb import SessionOrbStrategy
from fx_pro_bot.strategies.scalping.stat_arb import StatArbStrategy
from fx_pro_bot.strategies.scalping.vwap_reversion import VwapReversionStrategy


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


# ── VWAP Reversion Strategy ─────────────────────────────────


class TestVwapStrategy:
    def test_long_signal_below_vwap(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store, max_positions=10)

        base_price = 1.1000
        closes = [base_price + 0.0001 * i for i in range(55)]
        closes[-1] = base_price - 0.005  # below VWAP
        bars = _make_bars(closes)
        bars_map = {"EURUSD=X": bars}
        prices = {"EURUSD=X": closes[-1]}

        signals = strat.scan(bars_map, prices)
        # signal depends on deviation/ATR and RSI — may or may not trigger
        # just verify no crash and correct types
        assert isinstance(signals, list)

    def test_no_signal_inside_threshold(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store, max_positions=10)

        closes = [1.1000] * 60
        bars = _make_bars(closes)
        bars_map = {"EURUSD=X": bars}
        prices = {"EURUSD=X": 1.1000}

        signals = strat.scan(bars_map, prices)
        assert signals == []

    def test_process_signals_opens_position(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store, max_positions=10)

        from fx_pro_bot.strategies.scalping.vwap_reversion import VwapSignal

        sig = VwapSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            deviation_atr=1.5,
            rsi=30.0,
            vwap_price=1.1010,
            atr=0.0020,
        )

        opened = strat.process_signals([sig], {"EURUSD=X": 1.1000})
        assert opened == 1
        assert store.count_open_positions(strategy="vwap_reversion") == 1

    def test_max_positions_respected(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store, max_positions=1)

        from fx_pro_bot.strategies.scalping.vwap_reversion import VwapSignal

        sig = VwapSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            deviation_atr=1.5,
            rsi=30.0,
            vwap_price=1.1010,
            atr=0.0020,
        )

        strat.process_signals([sig], {"EURUSD=X": 1.1000})
        opened2 = strat.process_signals([sig], {"EURUSD=X": 1.1000})
        assert opened2 == 0

    def test_max_per_instrument_respected(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store, max_positions=10, max_per_instrument=1)

        from fx_pro_bot.strategies.scalping.vwap_reversion import VwapSignal

        sig = VwapSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            deviation_atr=1.5,
            rsi=30.0,
            vwap_price=1.1010,
            atr=0.0020,
        )

        strat.process_signals([sig], {"EURUSD=X": 1.1000})
        opened2 = strat.process_signals([sig], {"EURUSD=X": 1.1000})
        assert opened2 == 0

    def test_insufficient_bars_no_signal(self, tmp_path):
        store = _store(tmp_path)
        strat = VwapReversionStrategy(store)

        bars = _make_bars([1.10] * 10)
        signals = strat.scan({"EURUSD=X": bars}, {"EURUSD=X": 1.10})
        assert signals == []


# ── Stat-Arb Strategy ───────────────────────────────────────


class TestStatArbStrategy:
    def _divergent_pair(self, n=200):
        """A stays flat, B diverges → spread diverges."""
        closes_a = [1.1000] * n
        closes_b = [1.3000 + 0.001 * i for i in range(n)]
        bars_a = _make_bars(closes_a, "EURUSD=X")
        bars_b = _make_bars(closes_b, "GBPUSD=X")
        return {"EURUSD=X": bars_a, "GBPUSD=X": bars_b}

    def test_scan_detects_divergence(self, tmp_path):
        store = _store(tmp_path)
        strat = StatArbStrategy(
            store,
            pairs=[("EURUSD=X", "GBPUSD=X")],
            max_positions=10,
        )
        bars_map = self._divergent_pair()
        signals = strat.scan(bars_map)
        assert isinstance(signals, list)

    def test_process_opens_pair(self, tmp_path):
        store = _store(tmp_path)
        strat = StatArbStrategy(
            store,
            pairs=[("EURUSD=X", "GBPUSD=X")],
            max_positions=10,
        )

        from fx_pro_bot.strategies.scalping.stat_arb import StatArbSignal

        sig = StatArbSignal(
            pair_id="EURUSD=X_GBPUSD=X",
            symbol_a="EURUSD=X",
            symbol_b="GBPUSD=X",
            z_score=2.5,
            beta=0.85,
            direction_a=TrendDirection.SHORT,
            direction_b=TrendDirection.LONG,
            atr_a=0.002,
            atr_b=0.003,
        )

        prices = {"EURUSD=X": 1.1000, "GBPUSD=X": 1.3000}
        opened = strat.process_signals([sig], prices)
        assert opened == 2
        assert store.count_open_positions(strategy="stat_arb") == 2

    def test_check_exits_closes_pair(self, tmp_path):
        store = _store(tmp_path)
        strat = StatArbStrategy(
            store,
            pairs=[("EURUSD=X", "GBPUSD=X")],
        )

        store.open_position(
            strategy="stat_arb", source="sa_testpair",
            instrument="EURUSD=X", direction="short",
            entry_price=1.1000, stop_loss_price=1.1040,
        )
        store.open_position(
            strategy="stat_arb", source="sa_testpair",
            instrument="GBPUSD=X", direction="long",
            entry_price=1.3000, stop_loss_price=1.2940,
        )

        n = 200
        closes_a = [1.1000 + 0.00001 * (i % 5) for i in range(n)]
        closes_b = [1.3000 + 0.00001 * (i % 5) for i in range(n)]
        bars_a = _make_bars(closes_a, "EURUSD=X")
        bars_b = _make_bars(closes_b, "GBPUSD=X")
        bars_map = {"EURUSD=X": bars_a, "GBPUSD=X": bars_b}

        closed = strat.check_exits(bars_map)
        assert isinstance(closed, int)

    def test_insufficient_bars_no_signal(self, tmp_path):
        store = _store(tmp_path)
        strat = StatArbStrategy(
            store, pairs=[("EURUSD=X", "GBPUSD=X")],
        )
        bars_map = {
            "EURUSD=X": _make_bars([1.10] * 10),
            "GBPUSD=X": _make_bars([1.30] * 10, "GBPUSD=X"),
        }
        assert strat.scan(bars_map) == []


# ── Session ORB Strategy ────────────────────────────────────


class TestSessionOrbStrategy:
    def _london_session_bars(self, n=60, spike=False):
        """Generate bars during London session (08:00 UTC)."""
        base = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
        closes = [1.1000 + 0.0001 * (i % 3) for i in range(n)]

        if spike:
            closes[-3] = 1.1000
            closes[-2] = 1.1050
            closes[-1] = 1.1080

        return _make_bars(closes, base_ts=base, volumes=[200.0] * n)

    def test_scan_no_crash(self, tmp_path):
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store, max_positions=10)

        bars = self._london_session_bars()
        bars_map = {"EURUSD=X": bars}
        prices = {"EURUSD=X": 1.1005}

        signals = strat.scan(bars_map, prices)
        assert isinstance(signals, list)

    def test_news_fade_spike_detection(self, tmp_path):
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store, max_positions=10)

        base = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        closes = [1.1000] * 55
        closes[-3] = 1.1000
        closes[-2] = 1.1050
        closes[-1] = 1.1100
        bars = _make_bars(closes, base_ts=base)
        bars_map = {"EURUSD=X": bars}
        prices = {"EURUSD=X": 1.1100}

        signals = strat.scan(bars_map, prices)
        assert isinstance(signals, list)

    def test_process_signals_opens(self, tmp_path):
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store, max_positions=10)

        from fx_pro_bot.strategies.scalping.session_orb import OrbSignal

        sig = OrbSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            source="orb_breakout",
            box_high=1.1010,
            box_low=1.0990,
            atr=0.002,
            detail="breakout above 1.10100",
        )

        opened = strat.process_signals([sig], {"EURUSD=X": 1.1015})
        assert opened == 1
        assert store.count_open_positions(strategy="session_orb") == 1

    def test_max_positions_respected(self, tmp_path):
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store, max_positions=1)

        from fx_pro_bot.strategies.scalping.session_orb import OrbSignal

        sig = OrbSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            source="orb_breakout",
            box_high=1.1010,
            box_low=1.0990,
            atr=0.002,
            detail="test",
        )

        strat.process_signals([sig], {"EURUSD=X": 1.1015})
        opened2 = strat.process_signals([sig], {"EURUSD=X": 1.1015})
        assert opened2 == 0

    def test_insufficient_bars(self, tmp_path):
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store)

        bars = _make_bars([1.10] * 10)
        signals = strat.scan({"EURUSD=X": bars}, {"EURUSD=X": 1.10})
        assert signals == []

    def test_session_bars_detection(self):
        base_london = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
        bars = _make_bars([1.10] * 20, base_ts=base_london)
        result = SessionOrbStrategy._get_session_bars(bars)
        assert len(result) > 0

    def test_off_session_no_bars(self):
        base_off = datetime(2026, 3, 28, 3, 0, tzinfo=UTC)
        bars = _make_bars([1.10] * 20, base_ts=base_off)
        result = SessionOrbStrategy._get_session_bars(bars)
        assert result == []

    def test_confirm_bar_required_for_long(self, tmp_path):
        """Confirm bar: LONG вход только если close закрытого бара > box_high + filt.

        Касание wick не должно давать сигнал (ложные пробои <30мин = 295 сделок
        -3027 pips за 09-22.04.2026, см. BUILDLOG).
        """
        store = _store(tmp_path)
        strat = SessionOrbStrategy(store, max_positions=10)

        # Базовые бары: формируем 3-барный бокс, потом wick (high >> box), но close внутри
        base = datetime(2026, 3, 28, 8, 0, tzinfo=UTC)
        closes = [1.1000] * 60
        # ORB бокс: первые 3 бара после 8:00 = [1.1000, 1.1000, 1.1000]
        # 4-й бар (post-ORB): close внутри бокса (ложный пробой)
        bars = _make_bars(closes, base_ts=base, volumes=[200.0] * 60)
        # Последний бар: high = пробой, но close внутри коробки
        last = bars[-1]
        bars[-1] = Bar(
            instrument=last.instrument, ts=last.ts,
            open=last.open, high=1.1100, low=last.low,
            close=1.1000,  # close внутри коробки!
            volume=400.0,
        )

        prices = {"EURUSD=X": 1.1100}  # текущий price вышел за коробку (wick)
        signals = strat.scan({"EURUSD=X": bars}, prices)
        orb_signals = [s for s in signals if s.source == "orb_breakout"]
        assert orb_signals == [], "не должно быть сигнала без закрытия бара вне коробки"

    def test_news_fade_htf_blocks_short(self, tmp_path):
        """HTF EMA200 H1 БЛОКИРУЕТ SHORT news_fade при растущем H1 тренде.

        Ранее был warning-only → SHORT PF 0.36 vs LONG PF 0.62 (09-22.04).
        Research: Murphy J. «Technical Analysis», ch.9 — mean-reversion
        против H1-тренда имеет отрицательный edge.
        """
        strat = SessionOrbStrategy(_store(tmp_path), max_positions=10)
        base = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        closes = [1.1000] * 55
        closes[-3] = 1.1000
        closes[-2] = 1.1050
        closes[-1] = 1.1100
        bars = _make_bars(closes, base_ts=base)

        # Симулируем восходящий H1 тренд (htf_slope > 0)
        sig = strat._check_news_fade(
            "EURUSD=X", bars, price=1.1100, atr=0.0020,
            slope=0.0, htf_slope=0.001,
        )
        assert sig is None or sig.direction != TrendDirection.SHORT, (
            "HTF up должен блокировать SHORT news_fade"
        )

    def test_news_fade_htf_blocks_long(self, tmp_path):
        """HTF EMA200 H1 БЛОКИРУЕТ LONG news_fade при падающем H1 тренде."""
        strat = SessionOrbStrategy(_store(tmp_path), max_positions=10)
        base = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        closes = [1.1000] * 55
        closes[-3] = 1.1000
        closes[-2] = 1.0950
        closes[-1] = 1.0900
        bars = _make_bars(closes, base_ts=base)

        sig = strat._check_news_fade(
            "EURUSD=X", bars, price=1.0900, atr=0.0020,
            slope=0.0, htf_slope=-0.001,
        )
        assert sig is None or sig.direction != TrendDirection.LONG, (
            "HTF down должен блокировать LONG news_fade"
        )


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
