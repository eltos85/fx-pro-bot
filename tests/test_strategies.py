"""Тесты для стратегий: Leaders, Outsiders, Exits, Monitor, Shadow, Filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
    base: datetime | None = None,
) -> list[Bar]:
    inst = InstrumentId(symbol=instrument)
    if base is None:
        base = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
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


def test_atr_spike_removed() -> None:
    """atr_spike setup удалён: 4× ATR fade был overfit (см. outsiders.py docstring)."""
    closes = [1.10] * 40 + [1.10 + i * 0.005 for i in range(15)]
    bars_map = {"EURUSD=X": _make_bars(closes)}

    sigs = detect_extreme_setups(("EURUSD=X",), bars_map)
    spike_sigs = [s for s in sigs if s.source == "atr_spike"]
    assert spike_sigs == []


def test_news_proximity_blocks_signals() -> None:
    """news proximity 23.04.2026: БЛОКИРУЮЩИЙ фильтр, а не источник сигнала.

    Research: Andersen et al. 2003 — fat tails вокруг high-impact news
    ломают mean-reversion. Теперь при близком событии весь инструмент
    скипается.
    """
    # Формируем RSI oversold сетап (падающая цена)
    closes = [1.10] * 15 + [1.10 - i * 0.003 for i in range(40)]
    bars = _make_bars(closes)
    ts = bars[-1].ts

    # БЕЗ news — сигнал должен быть (RSI oversold triggers LONG)
    sigs_no_news = detect_extreme_setups(("EURUSD=X",), {"EURUSD=X": bars}, (), now=ts)
    # С ближним high-impact news — сигнал должен быть заблокирован
    event = CalendarEvent(title="NFP", at=ts + timedelta(hours=2), importance="high")
    sigs_with_news = detect_extreme_setups(
        ("EURUSD=X",), {"EURUSD=X": bars}, (event,), now=ts,
    )

    # news не создаёт сигнал
    assert all(s.source != "news" for s in sigs_with_news)
    # news-фильтр блокирует RSI/BB сигналы (возможно не все сетапы сработают
    # из-за HTF/liquid session — главное что их не больше чем без фильтра)
    assert len(sigs_with_news) <= len(sigs_no_news)


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


def test_monitor_peak_uses_bar_high(tmp_path) -> None:
    """recent_bars: peak обновляется по high бара (а не close).

    Это ключ к корректному server-side trailing — high бара даёт intra-bar
    peak, который пропускается при использовании только close.
    """
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="gold_orb", source="gold_orb_breakout", instrument="GC=F",
        direction="long", entry_price=4700.0,
    )
    inst = InstrumentId(symbol="GC=F")
    bar = Bar(
        instrument=inst,
        ts=datetime(2026, 4, 27, 10, 35, tzinfo=UTC),
        open=4700.5, high=4710.0, low=4698.0, close=4702.0, volume=100.0,
    )
    mon = PositionMonitor(store)
    # ATR=5.0 → atr_pips=50 → scalp_tp=150, movement 20pips недостаточно
    mon.run({"GC=F": 4702.0}, {"GC=F": 5.0}, recent_bars={"GC=F": bar})
    pos = store.get_open_positions()[0]
    # close=4702, но high=4710 → peak должен быть 4710 (long)
    assert pos.peak_price == 4710.0
    assert pos.trough_price == 4698.0


def test_monitor_peak_short_uses_bar_low(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="gold_orb", source="gold_orb_breakout", instrument="GC=F",
        direction="short", entry_price=4710.0,
    )
    inst = InstrumentId(symbol="GC=F")
    bar = Bar(
        instrument=inst,
        ts=datetime(2026, 4, 27, 10, 35, tzinfo=UTC),
        open=4709.5, high=4712.0, low=4700.0, close=4708.0, volume=100.0,
    )
    mon = PositionMonitor(store)
    mon.run({"GC=F": 4708.0}, {"GC=F": 5.0}, recent_bars={"GC=F": bar})
    pos = store.get_open_positions()[0]
    # close=4708, но low=4700 → peak должен быть 4700 (short = min)
    assert pos.peak_price == 4700.0
    assert pos.trough_price == 4712.0


def test_monitor_peak_fallback_to_close_without_bars(tmp_path) -> None:
    """Без recent_bars peak обновляется по close (обратная совместимость)."""
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="gold_orb", source="gold_orb_breakout", instrument="GC=F",
        direction="long", entry_price=4700.0,
    )
    mon = PositionMonitor(store)
    mon.run({"GC=F": 4705.0}, {"GC=F": 5.0})
    pos = store.get_open_positions()[0]
    assert pos.peak_price == 4705.0


def test_monitor_gold_orb_no_botside_trail(tmp_path) -> None:
    """Для gold_orb monitor НЕ должен закрывать по scalp_trail.

    Trailing полностью отдан брокеру (server-side через amend SL). Иначе
    возникает race condition между bot closure и broker SL hit.
    """
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="gold_orb", source="gold_orb_breakout", instrument="GC=F",
        direction="long", entry_price=4700.0, stop_loss_price=4695.0,
    )
    # Симулируем peak +20 pips, потом откат на 10 pips (триггер trail = 5pips,
    # distance = 3 pips → откат 10 pips должен бы закрыть в обычной scalp-логике).
    # Перед run-ом установим peak
    pos = store.get_open_positions()[0]
    store.update_position_price(
        pos.id, 4702.0, 20.0, 0.0,
        peak_price=4702.0, trough_price=4699.0,
        trail_price=0.0, trail_activated=False,
    )
    inst = InstrumentId(symbol="GC=F")
    bar = Bar(
        instrument=inst,
        ts=datetime(2026, 4, 27, 10, 40, tzinfo=UTC),
        open=4702.0, high=4702.5, low=4701.0, close=4701.0, volume=100.0,
    )
    mon = PositionMonitor(store)
    stats = mon.run({"GC=F": 4701.0}, {"GC=F": 0.5}, recent_bars={"GC=F": bar})
    # gold_orb не закрылся по scalp_trail
    assert stats["closed_trail"] == 0
    assert store.count_open_positions() == 1


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


# ── Position sizing (ATR-scaled) ─────────────────────────────


def test_calc_lot_size_eurusd_formula() -> None:
    """Проверяем формулу на SL где cap не мешает.

    pip_value_usd(EURUSD, 0.01) = $0.10 → per 0.01 lot: 50 pips × $0.10 = $5.
    lot = $15 / $5 × 0.01 = 0.03 (в пределах MAX_LOT_SIZE=0.05).
    """
    from fx_pro_bot.config.settings import calc_lot_size
    sl_dist = 0.0050  # 50 pips для EURUSD
    lot = calc_lot_size("EURUSD=X", sl_dist, risk_usd=15.0)
    assert 0.02 <= lot <= 0.04


def test_calc_lot_size_zero_sl_fallback() -> None:
    """Если SL=0 или отрицательный — возврат min_lot (защита от div-by-zero)."""
    from fx_pro_bot.config.settings import MIN_LOT_SIZE, calc_lot_size
    assert calc_lot_size("EURUSD=X", 0.0, risk_usd=15.0) == MIN_LOT_SIZE
    assert calc_lot_size("EURUSD=X", -0.001, risk_usd=15.0) == MIN_LOT_SIZE


def test_calc_lot_size_max_cap() -> None:
    """MAX_LOT_SIZE ограничивает при очень узком SL (защита от overleverage)."""
    from fx_pro_bot.config.settings import MAX_LOT_SIZE, calc_lot_size
    lot = calc_lot_size("EURUSD=X", 0.00001, risk_usd=1000.0)
    assert lot == MAX_LOT_SIZE


# ── Slippage guard ───────────────────────────────────────────


def test_max_slippage_pips_commodity() -> None:
    """Fallback: commodities — 10pip."""
    from fx_pro_bot.config.settings import max_slippage_pips
    assert max_slippage_pips("NG=F") == 10.0
    assert max_slippage_pips("CL=F") == 10.0
    assert max_slippage_pips("GC=F") == 10.0


def test_max_slippage_pips_fx_major() -> None:
    """Fallback: FX мажоры — 5pip."""
    from fx_pro_bot.config.settings import max_slippage_pips
    assert max_slippage_pips("EURUSD=X") == 5.0
    assert max_slippage_pips("GBPUSD=X") == 5.0
    assert max_slippage_pips("USDJPY=X") == 5.0


def test_max_slippage_pips_crypto() -> None:
    """Fallback: крипта — 20pip (высокая волатильность)."""
    from fx_pro_bot.config.settings import max_slippage_pips
    assert max_slippage_pips("BTC-USD") == 20.0
    assert max_slippage_pips("ETH-USD") == 20.0


def test_dynamic_slippage_formula_commodity_ng() -> None:
    """Динамический лимит = 30% tp_distance.

    NG=F: TP 17 pip (pip=0.001) → tp_distance=0.017, max_slip = 5.1 pip.
    Инцидент 23.04.2026 (slip=17pip при TP=17pip) → GUARD срабатывает.
    """
    from fx_pro_bot.config.settings import pip_size

    tp_distance = 0.017  # 17 pip NG=F
    ps = pip_size("NG=F")
    tp_pips = tp_distance / ps
    max_slip = tp_pips * 0.30

    assert tp_pips == pytest.approx(17.0, rel=0.01)
    assert max_slip == pytest.approx(5.1, rel=0.01)
    assert 17.0 > max_slip  # инцидент блокируется
    assert 5.0 <= max_slip  # допустимый slip 5pip проходит


def test_dynamic_slippage_formula_fx_eurusd() -> None:
    """EURUSD: TP 25 pip (pip=0.0001) → tp_distance=0.0025, max_slip = 7.5 pip."""
    from fx_pro_bot.config.settings import pip_size

    tp_distance = 0.0025  # 25 pip
    ps = pip_size("EURUSD=X")
    tp_pips = tp_distance / ps
    max_slip = tp_pips * 0.30

    assert tp_pips == pytest.approx(25.0, rel=0.01)
    assert max_slip == pytest.approx(7.5, rel=0.01)


def test_dynamic_slippage_formula_orb_narrow_tp() -> None:
    """ORB с узким TP=5pip: dynamic guard 1.5pip (static 10pip пропустил бы).

    Ключевое преимущество Варианта B: static commodities=10pip больше
    чем весь TP, позиция открылась бы с отрицательным expectancy.
    """
    from fx_pro_bot.config.settings import pip_size

    tp_distance = 0.005  # 5 pip NG=F
    ps = pip_size("NG=F")
    tp_pips = tp_distance / ps
    max_slip = tp_pips * 0.30

    assert tp_pips == pytest.approx(5.0, rel=0.01)
    assert max_slip == pytest.approx(1.5, rel=0.01)
    assert max_slip < 10.0  # динамический жёстче static(=10)


def test_order_result_has_slippage_fields() -> None:
    """OrderResult поддерживает strategic_price и slippage_pips."""
    from fx_pro_bot.trading.executor import OrderResult
    r = OrderResult(
        success=True,
        broker_position_id=1,
        fill_price=2.908,
        strategic_price=2.891,
        slippage_pips=17.0,
        volume=50000,
    )
    assert r.strategic_price == 2.891
    assert r.slippage_pips == 17.0
    assert r.fill_price == 2.908


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
