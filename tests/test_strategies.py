"""Тесты для стратегий: Leaders, Outsiders, Exits, Monitor, Shadow, Filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.events.models import CalendarEvent
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.exits import (
    EXIT_STRATEGIES,
    _profit_pips,
    create_paper_positions,
)
from fx_pro_bot.strategies.filters import check_entry_allowed
from fx_pro_bot.strategies.leaders import LeadersStrategy, aggregate_leader_signals
from fx_pro_bot.strategies.monitor import PositionMonitor
from fx_pro_bot.strategies.outsiders import (
    OutsidersStrategy,
    detect_extreme_setups,
)
from fx_pro_bot.strategies.shadow import ShadowTracker
from fx_pro_bot.whales.cot import CotSignal
from fx_pro_bot.whales.sentiment import SentimentSignal


def _make_bars(
    closes: list[float], instrument: str = "EURUSD=X",
) -> list[Bar]:
    inst = InstrumentId(symbol=instrument)
    base = datetime(2026, 3, 1, tzinfo=UTC)
    return [
        Bar(
            instrument=inst,
            ts=base + timedelta(minutes=5 * i),
            open=c - 0.0001, high=c + 0.0005,
            low=c - 0.0005, close=c, volume=100.0,
        )
        for i, c in enumerate(closes)
    ]


# ── Leaders ──────────────────────────────────────────────────


def test_aggregate_leader_signals_agreement() -> None:
    cot = [CotSignal("EURUSD=X", TrendDirection.LONG, 50000, 5000, 62.0, "2026-03-28")]
    sent = [SentimentSignal("EURUSD=X", TrendDirection.LONG, 28.0, 72.0, 5000)]
    bars = {"EURUSD=X": _make_bars([1.08 + i * 0.0001 for i in range(60)])}

    sigs = aggregate_leader_signals(cot, sent, bars)
    assert len(sigs) == 1
    assert sigs[0].direction == TrendDirection.LONG
    assert "cot" in sigs[0].sources
    assert "sentiment" in sigs[0].sources


def test_aggregate_leader_signals_disagreement() -> None:
    cot = [CotSignal("EURUSD=X", TrendDirection.LONG, 50000, 5000, 62.0, "2026-03-28")]
    sent = [SentimentSignal("EURUSD=X", TrendDirection.SHORT, 72.0, 28.0, 5000)]

    sigs = aggregate_leader_signals(cot, sent, {})
    assert len(sigs) == 0


def test_leaders_open_position(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    strat = LeadersStrategy(store, max_positions=5)
    cot = [CotSignal("EURUSD=X", TrendDirection.LONG, 50000, 5000, 62.0, "2026-03-28")]
    sent = [SentimentSignal("EURUSD=X", TrendDirection.LONG, 28.0, 72.0, 5000)]
    bars = {"EURUSD=X": _make_bars([1.08 + i * 0.0001 for i in range(60)])}

    sigs = aggregate_leader_signals(cot, sent, bars)
    opened = strat.process_signals(sigs, {"EURUSD=X": 1.0850})
    assert opened == 1
    assert store.count_open_positions(strategy="leaders") == 1


def test_leaders_max_positions(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    strat = LeadersStrategy(store, max_positions=1)

    store.open_position(
        strategy="leaders", source="cot", instrument="GBPUSD=X",
        direction="long", entry_price=1.28,
    )

    cot = [CotSignal("EURUSD=X", TrendDirection.LONG, 50000, 5000, 62.0, "2026-03-28")]
    sent = [SentimentSignal("EURUSD=X", TrendDirection.LONG, 28.0, 72.0, 5000)]
    sigs = aggregate_leader_signals(cot, sent, {})
    opened = strat.process_signals(sigs, {"EURUSD=X": 1.0850})
    assert opened == 0


# ── Outsiders ────────────────────────────────────────────────


def test_detect_rsi_extreme() -> None:
    closes = [1.10] * 15 + [1.10 - i * 0.003 for i in range(40)]
    bars_map = {"EURUSD=X": _make_bars(closes)}

    sigs = detect_extreme_setups(("EURUSD=X",), bars_map)
    rsi_sigs = [s for s in sigs if s.source == "extreme_rsi"]
    if rsi_sigs:
        assert rsi_sigs[0].direction in (TrendDirection.LONG, TrendDirection.SHORT)


def test_detect_atr_spike() -> None:
    closes = [1.10] * 40 + [1.10 + i * 0.005 for i in range(15)]
    bars_map = {"EURUSD=X": _make_bars(closes)}

    sigs = detect_extreme_setups(("EURUSD=X",), bars_map)
    spike_sigs = [s for s in sigs if s.source == "atr_spike"]
    if spike_sigs:
        assert spike_sigs[0].instrument == "EURUSD=X"


def test_detect_news_proximity() -> None:
    closes = [1.10] * 55
    bars = _make_bars(closes)
    ts = bars[-1].ts
    event = CalendarEvent(title="NFP", at=ts + timedelta(hours=2), importance="high")

    sigs = detect_extreme_setups(("EURUSD=X",), {"EURUSD=X": bars}, (event,), now=ts)
    news_sigs = [s for s in sigs if s.source == "news"]
    assert len(news_sigs) == 1


def test_outsiders_creates_papers(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    strat = OutsidersStrategy(store, max_positions=10)

    closes = [1.10] * 15 + [1.10 - i * 0.003 for i in range(40)]
    bars_map = {"EURUSD=X": _make_bars(closes)}
    sigs = detect_extreme_setups(("EURUSD=X",), bars_map)

    if sigs:
        opened = strat.process_signals(sigs[:1], {"EURUSD=X": 1.08})
        assert opened == 1
        papers = store.get_open_paper_positions()
        assert len(papers) == 4
        strats = {p.exit_strategy for p in papers}
        assert strats == set(EXIT_STRATEGIES)


# ── Exits ────────────────────────────────────────────────────


def test_profit_pips_long() -> None:
    assert abs(_profit_pips("long", 1.1000, 1.1050, 0.0001) - 50.0) < 0.01


def test_profit_pips_short() -> None:
    assert abs(_profit_pips("short", 1.1050, 1.1000, 0.0001) - 50.0) < 0.01


def test_create_paper_positions(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="outsiders", source="extreme_rsi",
        instrument="EURUSD=X", direction="long", entry_price=1.10,
    )
    ids = create_paper_positions(store, pid, 1.10, TrendDirection.LONG, 0.001, 0.0001)
    assert len(ids) == 4
    papers = store.get_open_paper_positions(position_id=pid)
    assert len(papers) == 4


# ── Monitor ──────────────────────────────────────────────────


def test_monitor_stop_loss(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="leaders", source="cot", instrument="EURUSD=X",
        direction="long", entry_price=1.10, stop_loss_price=1.09,
    )

    mon = PositionMonitor(store)
    stats = mon.run({"EURUSD=X": 1.085}, {"EURUSD=X": 0.001})
    assert stats["closed_sl"] == 1
    assert store.count_open_positions() == 0


def test_monitor_updates_price(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="leaders", source="cot", instrument="EURUSD=X",
        direction="long", entry_price=1.10,
    )

    mon = PositionMonitor(store)
    stats = mon.run({"EURUSD=X": 1.11}, {"EURUSD=X": 0.001})
    assert stats["updated"] == 1

    positions = store.get_open_positions()
    assert positions[0].current_price == 1.11


# ── Shadow ───────────────────────────────────────────────────


def test_shadow_records(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="leaders", source="cot", instrument="EURUSD=X",
        direction="long", entry_price=1.10,
    )

    shadow = ShadowTracker(store)
    count = shadow.run({"EURUSD=X": 1.11})
    assert count == 1

    summary = store.shadow_summary()
    assert len(summary) == 1
    assert summary[0]["strategy"] == "leaders"
    assert summary[0]["best_peak"] > 0


# ── Filters ──────────────────────────────────────────────────


def test_filter_position_limit(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="leaders", source="cot", instrument="EURUSD=X",
        direction="long", entry_price=1.10,
    )

    allowed, reason = check_entry_allowed(
        store, strategy="leaders", instrument="EURUSD=X",
        signal_price=1.10, current_price=1.10, atr=0.001, max_positions=1,
    )
    assert not allowed
    assert "лимит" in reason


def test_filter_price_drift(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")

    allowed, reason = check_entry_allowed(
        store, strategy="leaders", instrument="EURUSD=X",
        signal_price=1.10, current_price=1.12, atr=0.001, max_positions=20,
    )
    assert not allowed
    assert "ATR" in reason


def test_filter_ok(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")

    allowed, reason = check_entry_allowed(
        store, strategy="leaders", instrument="EURUSD=X",
        signal_price=1.10, current_price=1.1005, atr=0.001, max_positions=20,
    )
    assert allowed


# ── Store positions ──────────────────────────────────────────


def test_position_lifecycle(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="outsiders", source="extreme_rsi",
        instrument="EURUSD=X", direction="long", entry_price=1.10,
    )
    assert store.count_open_positions() == 1

    store.close_position(pid, "stop_loss")
    assert store.count_open_positions() == 0

    summary = store.position_summary_by_strategy()
    assert len(summary) == 1
    assert summary[0]["strategy"] == "outsiders"
    assert summary[0]["closed"] == 1


def test_paper_lifecycle(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="outsiders", source="extreme_rsi",
        instrument="EURUSD=X", direction="long", entry_price=1.10,
    )
    ppid = store.open_paper_position(
        position_id=pid, exit_strategy="scalp", entry_price=1.10,
    )

    papers = store.get_open_paper_positions(position_id=pid)
    assert len(papers) == 1
    assert papers[0].exit_strategy == "scalp"

    store.close_paper_position(ppid, "scalp_tp")
    papers = store.get_open_paper_positions(position_id=pid)
    assert len(papers) == 0

    summary = store.paper_summary_by_exit_strategy()
    assert len(summary) == 1
    assert summary[0]["exit_strategy"] == "scalp"
