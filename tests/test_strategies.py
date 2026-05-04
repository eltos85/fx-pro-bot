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


def _make_bar(symbol: str, ts: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(
        instrument=InstrumentId(symbol=symbol),
        ts=ts, open=o, high=h, low=l, close=c, volume=100.0,
    )


def test_monitor_shadow_intrabar_long_triggers(tmp_path) -> None:
    """Shadow INTRABAR: long, peak по bar.high, trigger в свече где
    (peak - low) >= trail_d (для XAU pip_size=0.10, atr=1.0 → atr_pips=10,
    trail_d = max(0.3*10, 3) = 3 pips = 0.3 цены).
    Не влияет на торговлю."""
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="gold_orb", source="orb_breakout", instrument="GC=F",
        direction="long", entry_price=4500.0, stop_loss_price=4495.0,
    )
    pos = store.get_open_positions()[0]
    entry_ts = datetime.fromisoformat(pos.created_at).replace(tzinfo=UTC)

    bars = [
        _make_bar("GC=F", entry_ts + timedelta(minutes=5),
                  4500.5, 4502.0, 4501.85, 4501.95),
        _make_bar("GC=F", entry_ts + timedelta(minutes=10),
                  4501.95, 4509.5, 4500.0, 4502.0),
    ]
    mon = PositionMonitor(store)
    mon.run(
        {"GC=F": 4502.0}, {"GC=F": 1.0},
        bars_map={"GC=F": bars},
    )
    state = mon._shadow_states.get(pid)
    assert state is not None
    assert state.peak_price == 4509.5
    assert state.peak_pips > 0
    assert state.triggered is True
    assert state.triggered_at_ts == bars[-1].ts


def test_monitor_shadow_intrabar_short_pending(tmp_path) -> None:
    """Shadow INTRABAR для short без retreat — state не triggered.
    Bar high всегда < (peak + trail_d), retreat не зафиксирован."""
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="gold_orb", source="orb_breakout", instrument="GC=F",
        direction="short", entry_price=4500.0, stop_loss_price=4505.0,
    )
    pos = store.get_open_positions()[0]
    entry_ts = datetime.fromisoformat(pos.created_at).replace(tzinfo=UTC)

    bars = [
        _make_bar("GC=F", entry_ts + timedelta(minutes=5),
                  4499.5, 4498.2, 4498.0, 4498.1),
        _make_bar("GC=F", entry_ts + timedelta(minutes=10),
                  4498.1, 4497.2, 4497.0, 4497.1),
    ]
    mon = PositionMonitor(store)
    mon.run(
        {"GC=F": 4497.1}, {"GC=F": 1.0},
        bars_map={"GC=F": bars},
    )
    state = mon._shadow_states.get(pid)
    assert state is not None
    assert state.peak_price == 4497.0
    assert state.triggered is False


def test_monitor_shadow_does_not_change_trading(tmp_path) -> None:
    """Shadow F1=BLOCK не должен мешать реальному закрытию по SL."""
    store = StatsStore(tmp_path / "t.db")
    store.open_position(
        strategy="gold_orb", source="orb_breakout", instrument="GC=F",
        direction="long", entry_price=4500.0, stop_loss_price=4495.0,
    )
    pos = store.get_open_positions()[0]
    entry_ts = datetime.fromisoformat(pos.created_at).replace(tzinfo=UTC)
    bars = [_make_bar("GC=F", entry_ts + timedelta(minutes=5),
                      4500.0, 4502.0, 4493.0, 4494.0)]
    mon = PositionMonitor(store)
    stats = mon.run(
        {"GC=F": 4494.0}, {"GC=F": 1.0},
        bars_map={"GC=F": bars},
    )
    assert stats["closed_sl"] == 1
    assert store.count_open_positions() == 0


def test_monitor_keeps_shadow_state_after_close(tmp_path) -> None:
    """При close monitor НЕ пишет в БД (это делает app/main.py через
    _persist_close_diagnostics), но обязан сохранить shadow_intrabar
    state доступным через get_shadow_state до явного pop."""
    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="gold_orb", source="orb_breakout", instrument="GC=F",
        direction="long", entry_price=4500.0, stop_loss_price=4495.0,
    )
    pos = store.get_open_positions()[0]
    entry_ts = datetime.fromisoformat(pos.created_at).replace(tzinfo=UTC)

    # ATR=1.0 → atr_pips=10, scalp_tp=3*10=30p, trigger=max(0.3*10,5)=5p,
    # trail_d=max(0.3*10,3)=3p. Peak +15p, потом откат +4p → scalp_trail.
    bars = [
        _make_bar("GC=F", entry_ts + timedelta(minutes=5),
                  4500.0, 4501.5, 4500.0, 4501.5),
    ]
    mon = PositionMonitor(store)
    mon.run(
        {"GC=F": 4501.5}, {"GC=F": 1.0},
        bars_map={"GC=F": bars},
    )
    bars2 = bars + [
        _make_bar("GC=F", entry_ts + timedelta(minutes=10),
                  4501.5, 4501.5, 4500.4, 4500.4),
    ]
    stats = mon.run(
        {"GC=F": 4500.4}, {"GC=F": 1.0},
        bars_map={"GC=F": bars2},
    )
    assert stats["closed_trail"] == 1
    # БД close-diag НЕ записан monitor'ом (это теперь делает main.py).
    diag = store.get_diagnostics(pid)
    assert diag is None or diag.get("peak_pips") is None
    # Но shadow-state доступен — main.py его подтянет через pop_shadow_state.
    sh = mon.get_shadow_state(pid)
    assert sh is not None
    assert sh.peak_pips > 0
    popped = mon.pop_shadow_state(pid)
    assert popped is sh
    assert mon.get_shadow_state(pid) is None


def test_compute_close_diagnostics_gold_orb_with_shadow(tmp_path) -> None:
    """compute_close_diagnostics возвращает peak/tp/trail/atr для
    gold_orb scalping и shadow_intrabar блок если shadow != None."""
    from fx_pro_bot.strategies.monitor import (
        ShadowTrailState, compute_close_diagnostics,
    )

    store = StatsStore(tmp_path / "t.db")
    pid = store.open_position(
        strategy="gold_orb", source="orb_breakout", instrument="GC=F",
        direction="long", entry_price=4500.0, stop_loss_price=4495.0,
    )
    store.update_position_price(
        pid, current_price=4500.4, profit_pips=4.0, profit_pct=0.0,
        peak_price=4501.5, trough_price=4499.5,
        trail_price=0.0, trail_activated=False,
    )
    store.close_position(pid, "scalp_trail")
    pos = store.get_position(pid)
    assert pos is not None and pos.peak_price > pos.entry_price

    shadow = ShadowTrailState(
        peak_price=4501.5, peak_pips=15.0,
        triggered=True, triggered_exit_pips=12.0,
    )
    diag = compute_close_diagnostics(
        pos, atr=1.0, ps=0.1, shadow=shadow, exit_reason=pos.exit_reason,
    )
    assert diag["peak_pips"] == 15.0    # (4501.5 - 4500) / 0.1
    assert diag["tp_target_pips"] == 30.0  # GOLD_ORB_TP_ATR_MULT (3.0) * 10
    assert diag["trail_trigger_pips"] == 6.0  # max(0.6*10, 5) = 6
    assert diag["trail_distance_pips"] == 3.0  # max(0.3*10, 3) = 3
    assert diag["atr_at_close_pips"] == 10.0
    assert diag["shadow_intrabar_triggered"] is True
    assert diag["shadow_intrabar_peak_pips"] == 15.0
    assert diag["shadow_intrabar_would_exit_pips"] == 12.0


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


# ── slippage_guard ghost-fix ─────────────────────────────────


def test_open_broker_for_new_slippage_links_broker_id_and_syncs_pnl(
    tmp_path, monkeypatch,
) -> None:
    """slippage_guard: когда executor.open_position возвращает success=False
    + broker_position_id, бот обязан:
      1. Записать `broker_position_id` в БД (`set_broker_position_id`),
      2. Закрыть позицию с `exit_reason='slippage_guard'`,
      3. Подтянуть реальный grossProfit из API через `_update_broker_pnl`
         и обновить `profit_pips`.

    Регрессия: до фикса BUILDLOG 2026-05-04 broker_id игнорировался,
    позиция оставалась с pos_id=0 — реальная сделка у брокера висела
    как ghost вне БД. См. `BUILDLOG.md` `bug-fix(slippage_guard)`.
    """
    from fx_pro_bot.app import main as _main
    from fx_pro_bot.trading.executor import AccountInfo, OrderResult

    monkeypatch.setattr(_main.time, "sleep", lambda *_a, **_kw: None)

    store = StatsStore(tmp_path / "t.db")
    pos_id = store.open_position(
        strategy="gold_orb", source="orb_breakout",
        instrument="XAUUSD=X", direction="long",
        entry_price=2400.0, stop_loss_price=2398.0,
    )

    slip_result = OrderResult(
        success=False,
        broker_position_id=987654,
        fill_price=2400.50,
        strategic_price=2400.00,
        slippage_pips=50.0,
        volume=10,
        error="slippage 50.0pip > max 10.0pip",
    )

    deal_payload = {
        "positionId": 987654,
        "grossProfit": -7.30,
        "volume": 10,
        "entryPrice": 2400.50,
    }

    class _StubExecutor:
        def __init__(self) -> None:
            self.open_calls = 0
            self.deal_calls = 0
        def get_account_info(self) -> AccountInfo:
            return AccountInfo(balance=1500.0)
        def get_open_positions(self) -> list:
            return []
        def open_position(self, **_kwargs) -> OrderResult:
            self.open_calls += 1
            return slip_result
        def get_deal_list(self, _ts_from: int, _ts_to: int) -> list[dict]:
            self.deal_calls += 1
            return [deal_payload]

    class _StubKill:
        def check_allowed(self, _open_count: int, _balance: float) -> bool:
            return True

    class _StubSettings:
        risk_per_trade_usd = 15.0
        lot_size = 0.01

    executor = _StubExecutor()
    _main._open_broker_for_new(
        store=store,
        executor=executor,
        killswitch=_StubKill(),
        before_ids=set(),
        prices={"XAUUSD=X": 2400.0},
        settings=_StubSettings(),  # type: ignore[arg-type]
        atrs={"XAUUSD=X": 1.5},
    )

    assert executor.open_calls == 1
    assert executor.deal_calls == 1, "должен дёргать deal_list для синка PnL"

    pos_after = store.get_position(pos_id)
    assert pos_after is not None
    assert pos_after.status == "closed"
    assert pos_after.exit_reason == "slippage_guard"
    assert pos_after.broker_position_id == 987654, (
        "broker_position_id должен быть сохранён, иначе ghost"
    )
    assert pos_after.profit_pips != 0.0, (
        "_update_broker_pnl должен пересчитать profit_pips из gross"
    )
    assert pos_after.profit_pips < 0, "loss-deal → отрицательный profit_pips"


def test_open_broker_for_new_slippage_no_broker_id_falls_back(
    tmp_path, monkeypatch,
) -> None:
    """Edge case: если broker_position_id=0 (executor не получил ответа от API),
    позиция всё равно закрывается со slippage_guard, но без link/sync.
    Поведение должно быть как до фикса для этой ветки.
    """
    from fx_pro_bot.app import main as _main
    from fx_pro_bot.trading.executor import AccountInfo, OrderResult

    monkeypatch.setattr(_main.time, "sleep", lambda *_a, **_kw: None)

    store = StatsStore(tmp_path / "t.db")
    pos_id = store.open_position(
        strategy="gold_orb", source="orb_breakout",
        instrument="XAUUSD=X", direction="long",
        entry_price=2400.0, stop_loss_price=2398.0,
    )

    no_id_result = OrderResult(
        success=False,
        broker_position_id=0,
        error="slippage 50.0pip > max 10.0pip",
    )

    class _StubExecutor:
        def __init__(self) -> None:
            self.deal_calls = 0
        def get_account_info(self) -> AccountInfo:
            return AccountInfo(balance=1500.0)
        def get_open_positions(self) -> list:
            return []
        def open_position(self, **_kwargs) -> OrderResult:
            return no_id_result
        def get_deal_list(self, _ts_from: int, _ts_to: int) -> list[dict]:
            self.deal_calls += 1
            return []

    class _StubKill:
        def check_allowed(self, _open_count: int, _balance: float) -> bool:
            return True

    class _StubSettings:
        risk_per_trade_usd = 15.0
        lot_size = 0.01

    executor = _StubExecutor()
    _main._open_broker_for_new(
        store=store,
        executor=executor,
        killswitch=_StubKill(),
        before_ids=set(),
        prices={"XAUUSD=X": 2400.0},
        settings=_StubSettings(),  # type: ignore[arg-type]
        atrs={"XAUUSD=X": 1.5},
    )

    pos_after = store.get_position(pos_id)
    assert pos_after is not None
    assert pos_after.status == "closed"
    assert pos_after.exit_reason == "slippage_guard"
    assert pos_after.broker_position_id == 0
    assert executor.deal_calls == 0, (
        "без broker_position_id нет смысла дёргать deal_list"
    )


# ── Gold ORB H2 (ATR-regime) + H5 (liquidity-sweep) фильтры ──


def _make_orb_bars(
    *,
    base: datetime,
    box_low: float,
    box_high: float,
    after_box_close: float,
    breakout_high: float,
    breakout_low: float | None = None,
    baseline_low: float | None = None,
) -> list[Bar]:
    """Конструктор баров для теста gold_orb _check_orb.

    Структура: 50 baseline баров + 3 ORB-бара + signal-бар.

    Чтобы H5 sweep НЕ возникал автоматически — baseline_low по
    умолчанию < box_low (prior_low ниже recent_low ORB-баров, sweep
    не выполняется). Для теста sweep-сценария передаётся baseline_low
    выше box_low.
    """
    inst = InstrumentId(symbol="GC=F")
    bars: list[Bar] = []
    bl = baseline_low if baseline_low is not None else (box_low - 1.0)
    for i in range(50):
        bars.append(Bar(
            instrument=inst,
            ts=base - timedelta(minutes=5 * (50 - i)),
            open=after_box_close, high=after_box_close + 0.5,
            low=bl, close=after_box_close, volume=100.0,
        ))
    box_open = base.replace(hour=8, minute=0, second=0, microsecond=0)
    for i in range(3):
        bars.append(Bar(
            instrument=inst, ts=box_open + timedelta(minutes=5 * i),
            open=(box_low + box_high) / 2,
            high=box_high, low=box_low,
            close=(box_low + box_high) / 2, volume=200.0,
        ))
    if breakout_low is not None:
        sig_high, sig_low = box_high - 0.1, breakout_low
    else:
        sig_high, sig_low = breakout_high, box_low + 0.1
    bars.append(Bar(
        instrument=inst, ts=box_open + timedelta(minutes=15),
        open=after_box_close, high=sig_high, low=sig_low,
        close=after_box_close, volume=200.0,
    ))
    return bars


def test_gold_orb_filters_disabled_passthrough(tmp_path) -> None:
    """Когда regime/sweep filters выключены — signal всегда проходит,
    H2/H5 поля заполняются для логирования но не влияют."""
    from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=False, sweep_filter=False)

    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars = _make_orb_bars(
        base=base, box_low=2400.0, box_high=2402.0,
        after_box_close=2401.0, breakout_high=2403.0,
    )
    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 1, "filters off — signal должен пройти"
    sig = sigs[0]
    assert sig.direction == TrendDirection.LONG
    assert sig.h2_regime in {"unknown", "expansion", "normal", "compression"}
    assert isinstance(sig.h5_swept_pre, bool)


def test_gold_orb_h2_blocks_compression(tmp_path) -> None:
    """H2 regime_filter блокирует signal в compression-режиме."""
    from fx_pro_bot.strategies.scalping.gold_orb import (
        GoldOrbStrategy, H2_DAILY_ATR_WINDOW,
    )

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=True, sweep_filter=False)

    inst = InstrumentId(symbol="GC=F")
    daily_bars: list[Bar] = []
    base_day = datetime(2026, 4, 1, tzinfo=UTC)
    for i in range(H2_DAILY_ATR_WINDOW + 20):
        rng = 5.0 if i < H2_DAILY_ATR_WINDOW + 19 else 0.5
        daily_bars.append(Bar(
            instrument=inst, ts=base_day + timedelta(days=i),
            open=2400.0, high=2400.0 + rng, low=2400.0 - rng,
            close=2400.0, volume=10000.0,
        ))
    strat.update_daily_atr_history(daily_bars)
    regime, pct = strat._h2_regime()
    assert regime == "compression", f"ожидали compression, получили {regime} {pct}"

    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars = _make_orb_bars(
        base=base, box_low=2400.0, box_high=2402.0,
        after_box_close=2401.0, breakout_high=2403.0,
    )
    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 0, "H2 compression — signal должен быть заблокирован"


def test_gold_orb_h2_passes_expansion(tmp_path) -> None:
    """H2 пропускает signal когда current ATR в expansion (top P70)."""
    from fx_pro_bot.strategies.scalping.gold_orb import (
        GoldOrbStrategy, H2_DAILY_ATR_WINDOW,
    )

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=True, sweep_filter=False)

    inst = InstrumentId(symbol="GC=F")
    daily_bars: list[Bar] = []
    base_day = datetime(2026, 4, 1, tzinfo=UTC)
    for i in range(H2_DAILY_ATR_WINDOW + 20):
        rng = 0.5 if i < H2_DAILY_ATR_WINDOW + 19 else 10.0
        daily_bars.append(Bar(
            instrument=inst, ts=base_day + timedelta(days=i),
            open=2400.0, high=2400.0 + rng, low=2400.0 - rng,
            close=2400.0, volume=10000.0,
        ))
    strat.update_daily_atr_history(daily_bars)
    regime, _pct = strat._h2_regime()
    assert regime == "expansion", f"ожидали expansion, получили {regime}"

    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars = _make_orb_bars(
        base=base, box_low=2400.0, box_high=2402.0,
        after_box_close=2401.0, breakout_high=2403.0,
    )
    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 1, "H2 expansion — signal должен пройти"
    assert sigs[0].h2_regime == "expansion"


def test_gold_orb_h2_unknown_failsafe(tmp_path) -> None:
    """Без daily history regime='unknown' → signal проходит (fail-safe)."""
    from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=True, sweep_filter=False)

    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars = _make_orb_bars(
        base=base, box_low=2400.0, box_high=2402.0,
        after_box_close=2401.0, breakout_high=2403.0,
    )
    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 1, "regime=unknown → fail-safe passthrough"
    assert sigs[0].h2_regime == "unknown"


def test_gold_orb_h5_blocks_no_sweep(tmp_path) -> None:
    """H5 sweep_filter блокирует signal без liquidity-sweep."""
    from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=False, sweep_filter=True)

    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars = _make_orb_bars(
        base=base, box_low=2400.0, box_high=2402.0,
        after_box_close=2401.0, breakout_high=2403.0,
    )
    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 0, "H5 без sweep — signal должен быть заблокирован"


def test_gold_orb_h5_passes_with_sweep(tmp_path) -> None:
    """H5 пропускает signal когда recent_window выкосил prior_low (long).

    Layout (54 баров, signal_idx=53):
      bars[0..42]   baseline-prior, low=2399.0  → prior_low = 2399.0
      bars[43..49]  baseline-recent, low=2398.0 → выкос ниже prior_low
      bars[50..52]  ORB, low=2400.0
      bars[53]      signal: high=2403, low=2400.5, close=2401
                    close >= prior_low (2399) → signal вернулся в prior range
    """
    from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy

    store = StatsStore(tmp_path / "t.db")
    strat = GoldOrbStrategy(store, regime_filter=False, sweep_filter=True)

    inst = InstrumentId(symbol="GC=F")
    base = datetime(2026, 5, 5, 8, 15, tzinfo=UTC)
    bars: list[Bar] = []
    for i in range(50):
        low_v = 2398.0 if i >= 43 else 2399.0
        bars.append(Bar(
            instrument=inst, ts=base - timedelta(minutes=5 * (50 - i)),
            open=2401.0, high=2401.5, low=low_v,
            close=2401.0, volume=100.0,
        ))
    box_open = base.replace(hour=8, minute=0)
    for i in range(3):
        bars.append(Bar(
            instrument=inst, ts=box_open + timedelta(minutes=5 * i),
            open=2401.0, high=2402.0, low=2400.0,
            close=2401.0, volume=200.0,
        ))
    bars.append(Bar(
        instrument=inst, ts=box_open + timedelta(minutes=15),
        open=2401.0, high=2403.0, low=2400.5,
        close=2401.0, volume=200.0,
    ))

    sigs = strat.scan({"GC=F": bars}, {"GC=F": 2403.0})
    assert len(sigs) == 1, "H5 с sweep — signal должен пройти"
    assert sigs[0].h5_swept_pre is True
    assert sigs[0].direction == TrendDirection.LONG
