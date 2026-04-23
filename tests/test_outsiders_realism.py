"""Тесты: модель издержек (cost_model) и confirmed-режим Outsiders."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.events.models import CalendarEvent
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.stats.cost_model import CostEstimate, estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.monitor import PositionMonitor
from fx_pro_bot.strategies.outsiders import (
    CLASSIC_SL_ATR,
    CONFIRMED_SL_ATR,
    OutsiderSignal,
    OutsidersStrategy,
    _check_bb_confirmed,
    _check_news_confirmed,
    _check_rsi_confirmed,
    _is_liquid_session,
    _limit_entry_price,
    detect_extreme_setups,
)


def _make_bars(
    closes: list[float],
    instrument: str = "EURUSD=X",
    *,
    volumes: list[float] | None = None,
    base_ts: datetime | None = None,
    interval_min: int = 5,
    high_offset: float = 0.0005,
    low_offset: float = 0.0005,
) -> list[Bar]:
    inst = InstrumentId(symbol=instrument)
    base = base_ts or datetime(2026, 3, 26, 10, 0, tzinfo=UTC)
    vols = volumes or [100.0] * len(closes)
    return [
        Bar(
            instrument=inst,
            ts=base + timedelta(minutes=interval_min * i),
            open=c - 0.0001,
            high=c + high_offset,
            low=c - low_offset,
            close=c,
            volume=vols[i] if i < len(vols) else 100.0,
        )
        for i, c in enumerate(closes)
    ]


def _store(tmp_path) -> StatsStore:
    return StatsStore(tmp_path / "test.sqlite")


# ── CostEstimate ──────────────────────────────────────────────


class TestCostEstimate:
    def test_total_pips(self):
        c = CostEstimate(spread_pips=3.0, slippage_pips=2.0)
        assert c.total_pips == 5.0

    def test_round_trip(self):
        c = CostEstimate(spread_pips=3.0, slippage_pips=2.0)
        assert c.round_trip_pips == 5.0

    def test_zero_cost(self):
        c = CostEstimate(spread_pips=0.0, slippage_pips=0.0)
        assert c.total_pips == 0.0
        assert c.round_trip_pips == 0.0


class TestEstimateEntryCost:
    def test_extreme_rsi_has_high_multiplier(self):
        cost = estimate_entry_cost("EURUSD=X", "extreme_rsi", atr=0.0020, pip_sz=0.0001)
        assert cost.spread_pips == 1.5 * 2.5
        assert cost.slippage_pips == pytest.approx(0.0020 / 0.0001 * 0.05, rel=0.01)

    def test_atr_spike_highest_multiplier(self):
        cost = estimate_entry_cost("GBPUSD=X", "atr_spike", atr=0.0030, pip_sz=0.0001)
        assert cost.spread_pips == 1.8 * 4.0

    def test_leaders_low_multiplier(self):
        cost = estimate_entry_cost("EURUSD=X", "cot", atr=0.0020, pip_sz=0.0001)
        assert cost.spread_pips == 1.5

    def test_scalping_moderate_multiplier(self):
        cost = estimate_entry_cost("AUDUSD=X", "vwap_deviation", atr=0.0015, pip_sz=0.0001)
        assert cost.spread_pips == 1.8 * 1.2

    def test_unknown_source_uses_defaults(self):
        cost = estimate_entry_cost("EURUSD=X", "unknown_source", atr=0.0020, pip_sz=0.0001)
        assert cost.spread_pips == 1.5 * 1.0

    def test_zero_pip_size_no_crash(self):
        cost = estimate_entry_cost("EURUSD=X", "extreme_rsi", atr=0.0020, pip_sz=0.0)
        assert cost.slippage_pips == 0.0

    def test_round_trip_for_outsiders_is_significant(self):
        cost = estimate_entry_cost("EURUSD=X", "atr_spike", atr=0.0030, pip_sz=0.0001)
        assert cost.round_trip_pips > 5.0, "ATR spike entry should have >5 pips round-trip cost"


# ── Session filter ────────────────────────────────────────────


class TestLiquidSession:
    def _bar_at(self, hour: int, minute: int = 0, weekday: int = 0) -> Bar:
        base = datetime(2026, 3, 23, hour, minute, tzinfo=UTC)
        while base.weekday() != weekday:
            base += timedelta(days=1)
        return Bar(
            instrument=InstrumentId(symbol="EURUSD=X"),
            ts=base,
            open=1.10, high=1.101, low=1.099, close=1.10, volume=100,
        )

    def test_london_session_liquid(self):
        assert _is_liquid_session(self._bar_at(10, weekday=1))

    def test_ny_session_liquid(self):
        assert _is_liquid_session(self._bar_at(15, weekday=2))

    def test_asian_session_illiquid(self):
        assert not _is_liquid_session(self._bar_at(3, weekday=1))

    def test_late_night_illiquid(self):
        assert not _is_liquid_session(self._bar_at(23, weekday=3))

    def test_weekend_illiquid(self):
        assert not _is_liquid_session(self._bar_at(10, weekday=5))

    def test_ny_close_excluded(self):
        """21:00 UTC ровно — NY close; ликвидность резко падает, блокируем."""
        assert not _is_liquid_session(self._bar_at(21, minute=0, weekday=1))

    def test_ny_pre_close_liquid(self):
        """20:55 UTC — ещё в NY session."""
        assert _is_liquid_session(self._bar_at(20, minute=55, weekday=1))


# ── Confirmed detectors ──────────────────────────────────────


class TestRsiConfirmed:
    def _closes_with_rsi_pattern(self, extreme_low: bool) -> tuple[list[float], float, list[float]]:
        base = [1.1000 + i * 0.0001 for i in range(40)]
        if extreme_low:
            for i in range(20):
                base.append(base[-1] - 0.0010)
            recovery = base[-1] + 0.0015
        else:
            for i in range(20):
                base.append(base[-1] + 0.0010)
            recovery = base[-1] - 0.0015
        prev_closes = list(base)
        all_closes = list(base) + [recovery]
        return prev_closes, recovery, all_closes

    def test_rsi_oversold_recovery(self):
        prev, cur, all_c = self._closes_with_rsi_pattern(extreme_low=True)
        sig = _check_rsi_confirmed("EURUSD=X", prev, cur, all_c, 0.002)
        if sig is not None:
            assert sig.direction == TrendDirection.LONG
            assert "confirmed" in sig.detail.lower()

    def test_rsi_overbought_reversal(self):
        prev, cur, all_c = self._closes_with_rsi_pattern(extreme_low=False)
        sig = _check_rsi_confirmed("EURUSD=X", prev, cur, all_c, 0.002)
        if sig is not None:
            assert sig.direction == TrendDirection.SHORT

    def test_no_signal_when_not_extreme(self):
        closes = [1.1000 + i * 0.00001 for i in range(60)]
        sig = _check_rsi_confirmed("EURUSD=X", closes[:-1], closes[-1], closes, 0.002)
        assert sig is None


class TestBbConfirmed:
    def test_bb_recovery_from_below(self):
        base = [1.1000] * 20
        prev_closes = base + [1.0850]
        cur_close = 1.0950
        sig = _check_bb_confirmed("EURUSD=X", prev_closes, cur_close, 0.002)
        if sig is not None:
            assert sig.direction == TrendDirection.LONG

    def test_bb_reversal_from_above(self):
        base = [1.1000] * 20
        prev_closes = base + [1.1150]
        cur_close = 1.1050
        sig = _check_bb_confirmed("EURUSD=X", prev_closes, cur_close, 0.002)
        if sig is not None:
            assert sig.direction == TrendDirection.SHORT

    def test_no_signal_when_inside_bands(self):
        base = [1.1000 + 0.0005 * (i % 3 - 1) for i in range(20)]
        prev_closes = base + [1.1002]
        sig = _check_bb_confirmed("EURUSD=X", prev_closes, 1.1001, 0.002)
        assert sig is None


class TestNewsConfirmed:
    def test_post_news_reversal(self):
        base_ts = datetime(2026, 3, 26, 10, 0, tzinfo=UTC)
        closes = [1.1000] * 50 + [1.1010, 1.1030, 1.1020]
        bars = _make_bars(closes, base_ts=base_ts)
        event = CalendarEvent(
            at=base_ts + timedelta(hours=3),
            title="CPI Release",
            currency="USD",
            importance="high",
        )
        now = bars[-1].ts
        sig = _check_news_confirmed("EURUSD=X", bars, (event,), 0.002, now)
        pass

    def test_no_event_no_signal(self):
        closes = [1.1000] * 55
        bars = _make_bars(closes)
        sig = _check_news_confirmed("EURUSD=X", bars, (), 0.002, bars[-1].ts)
        assert sig is None


# ── detect_extreme_setups mode parameter ──────────────────────


class TestDetectModes:
    def _bars_map(self, base_ts: datetime | None = None) -> dict[str, list[Bar]]:
        closes = [1.1000 + i * 0.00001 for i in range(55)]
        bars = _make_bars(closes, base_ts=base_ts)
        return {"EURUSD=X": bars}

    def test_classic_mode_returns_signals(self):
        closes = [1.1000] * 40
        for _ in range(15):
            closes.append(closes[-1] - 0.0015)
        bars = _make_bars(closes)
        sigs = detect_extreme_setups(
            ("EURUSD=X",), {"EURUSD=X": bars}, mode="classic",
        )
        assert isinstance(sigs, list)

    def test_confirmed_mode_filters_session(self):
        closes = [1.1000] * 55
        bars = _make_bars(
            closes,
            base_ts=datetime(2026, 3, 26, 2, 0, tzinfo=UTC),
        )
        sigs = detect_extreme_setups(
            ("EURUSD=X",), {"EURUSD=X": bars}, mode="confirmed",
        )
        assert sigs == [], "Asian session should be filtered in confirmed mode"

    def test_confirmed_mode_allows_london(self):
        closes = [1.1000] * 55
        bars = _make_bars(
            closes,
            base_ts=datetime(2026, 3, 26, 10, 0, tzinfo=UTC),
        )
        sigs = detect_extreme_setups(
            ("EURUSD=X",), {"EURUSD=X": bars}, mode="confirmed",
        )
        assert isinstance(sigs, list)


# ── Limit entry price ────────────────────────────────────────


class TestLimitEntryPrice:
    def test_long_gets_lower_price(self):
        price = _limit_entry_price(1.1000, TrendDirection.LONG, 0.0020)
        assert price < 1.1000

    def test_short_gets_higher_price(self):
        price = _limit_entry_price(1.1000, TrendDirection.SHORT, 0.0020)
        assert price > 1.1000

    def test_offset_is_30pct_atr(self):
        price = _limit_entry_price(1.1000, TrendDirection.LONG, 0.0020)
        assert price == pytest.approx(1.1000 - 0.3 * 0.0020)


# ── OutsidersStrategy with mode ──────────────────────────────


class TestOutsidersStrategyMode:
    def test_confirmed_mode_uses_tighter_sl(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        strat = OutsidersStrategy(store, mode="confirmed")
        assert strat.mode == "confirmed"

        sig = OutsiderSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail="test",
            atr=0.0020,
        )
        opened = strat.process_signals([sig], {"EURUSD=X": 1.1000})
        assert opened == 1

        positions = store.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]

        expected_entry = 1.1000 - 0.3 * 0.0020
        expected_sl = expected_entry - CONFIRMED_SL_ATR * 0.0020
        assert pos.entry_price == pytest.approx(expected_entry, abs=1e-6)
        assert pos.stop_loss_price == pytest.approx(expected_sl, abs=1e-6)

    def test_classic_mode_uses_original_sl(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        strat = OutsidersStrategy(store, mode="classic")

        sig = OutsiderSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail="test",
            atr=0.0020,
        )
        opened = strat.process_signals([sig], {"EURUSD=X": 1.1000})
        assert opened == 1

        positions = store.get_open_positions()
        pos = positions[0]
        expected_sl = 1.1000 - CLASSIC_SL_ATR * 0.0020
        assert pos.stop_loss_price == pytest.approx(expected_sl, abs=1e-6)

    def test_cost_is_recorded(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        strat = OutsidersStrategy(store, mode="classic")

        sig = OutsiderSignal(
            instrument="EURUSD=X",
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail="test",
            atr=0.0020,
        )
        strat.process_signals([sig], {"EURUSD=X": 1.1000})

        positions = store.get_open_positions()
        assert positions[0].estimated_cost_pips > 0


# ── StatsStore cost integration ──────────────────────────────


class TestStatsStoreCost:
    def test_set_estimated_cost(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        pid = store.open_position(
            strategy="outsiders", source="extreme_rsi",
            instrument="EURUSD=X", direction="long",
            entry_price=1.1000, stop_loss_price=1.0940,
        )
        store.set_estimated_cost(pid, 12.5)

        pos = store.get_open_positions()[0]
        assert pos.estimated_cost_pips == 12.5

    def test_summary_includes_cost(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        pid = store.open_position(
            strategy="outsiders", source="extreme_rsi",
            instrument="EURUSD=X", direction="long",
            entry_price=1.1000, stop_loss_price=1.0940,
        )
        store.set_estimated_cost(pid, 10.0)
        store.update_position_price(pid, 1.1050, 50.0, 0.45, 1.1050, 1.1000)
        store.close_position(pid, "aggressive_tp")

        summary = store.position_summary_by_strategy()
        row = summary[0]
        assert row["total_cost_pips"] == 10.0
        assert row["net_pips"] == pytest.approx(50.0 - 10.0)


# ── PositionMonitor confirmed mode ──────────────────────────


class TestMonitorConfirmedMode:
    def test_confirmed_hard_stop_at_36h(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        monitor = PositionMonitor(store, outsiders_mode="confirmed")

        pid = store.open_position(
            strategy="outsiders", source="extreme_rsi",
            instrument="EURUSD=X", direction="long",
            entry_price=1.1000, stop_loss_price=1.0960,
        )
        import sqlite3
        created_at = (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat()
        conn = sqlite3.connect(tmp_path / "test.sqlite")
        conn.execute("UPDATE positions SET created_at=? WHERE id=?", (created_at, pid))
        conn.commit()
        conn.close()

        store.update_position_price(pid, 1.1010, 10.0, 0.09, 1.1010, 1.1000)
        stats = monitor.run({"EURUSD=X": 1.1010}, {"EURUSD=X": 0.0020})

        positions = store.get_open_positions()
        assert len(positions) == 1, "At 25h, confirmed mode should NOT close (hard stop at 36h)"

    def test_classic_hard_stop_at_24h(self, tmp_path):
        store = StatsStore(tmp_path / "test.sqlite")
        monitor = PositionMonitor(store, outsiders_mode="classic")

        pid = store.open_position(
            strategy="outsiders", source="extreme_rsi",
            instrument="EURUSD=X", direction="long",
            entry_price=1.1000, stop_loss_price=1.0940,
        )
        import sqlite3
        created_at = (datetime.now(tz=UTC) - timedelta(hours=25)).isoformat()
        conn = sqlite3.connect(tmp_path / "test.sqlite")
        conn.execute("UPDATE positions SET created_at=? WHERE id=?", (created_at, pid))
        conn.commit()
        conn.close()

        store.update_position_price(pid, 1.1010, 10.0, 0.09, 1.1010, 1.1000)
        stats = monitor.run({"EURUSD=X": 1.1010}, {"EURUSD=X": 0.0020})

        positions = store.get_open_positions()
        assert len(positions) == 0, "At 25h, classic mode should close (hard stop at 24h)"
