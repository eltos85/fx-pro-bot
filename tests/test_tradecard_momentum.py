"""Юнит-тесты tradecard_momentum (advisory-ревьюер fx_momentum_bot).

Цели — чистая детерминированная логика (без сети/брокера). Фикстуры честные:
поля MomentumTrade задаются явно для проверки ЛОГИКИ детекторов/грейдинга, без
рисовки «под результат» (no-data-fitting.mdc — запрет про входные сигналы страт,
не про агрегаты ретро-детекторов).
"""
from __future__ import annotations

import sqlite3

import pytest

from tradecard_momentum.analysis.detectors import (
    detect_loss_cluster, detect_overtrading, detect_signal_not_predictive,
    detect_swap_drag, detect_symbol_session_leak)
from tradecard_momentum.analysis.engine import run_detection
from tradecard_momentum.analysis.grading import grade_curve
from tradecard_momentum.analysis.pnl import summarize, summarize_by_symbol
from tradecard_momentum.analysis.small_wins import evaluate_small_win
from tradecard_momentum.analysis.stats import (spearman_rho,
                                               two_proportion_test)
from tradecard_momentum.analysis.trade import MomentumTrade, expectancy_r
from tradecard_momentum.config.settings import TradecardMomentumSettings
from tradecard_momentum.data.broker import _match_decision, _scale_price
from tradecard_momentum.data.momentum_db import EntryDecision, _parse_dt
from tradecard_momentum.llm.five_why import build_prompt, parse_response
from tradecard_momentum.state.db import TradecardDB

_BASE_TS = 1_700_000_000.0  # фикс. UTC epoch (детерминизм сессии/недели)


def mk(pid: int, *, net: float, symbol: str = "EURUSD", side: str = "long",
       ts_open: float = _BASE_TS, entry: float = 1.10, risk: float | None = 0.001,
       signal_mom: float | None = 0.005, swap: float = 0.0,
       commission: float = 0.0, exit_px: float | None = None) -> MomentumTrade:
    """Сделка: net разносим в gross, чтобы net_usd==net (swap/comm по умолчанию 0).

    Если exit_px не задан — синтезируем так, чтобы знак хода соответствовал R от
    net (для тестов R берём из risk_price напрямую). Для детекторов важны
    net/win/loss; R задаём через risk + exit при необходимости.
    """
    gross = net - swap - commission
    if exit_px is None:
        # ход в сторону сделки, величина не критична для большинства тестов
        move = 0.002 if net > 0 else -0.002
        exit_px = entry + move if side == "long" else entry - move
    return MomentumTrade(
        position_id=pid, symbol=symbol, side=side, ts_open=ts_open,
        ts_close=ts_open + 3600, entry=entry, exit=exit_px, volume_units=10000,
        gross_usd=gross, swap_usd=swap, commission_usd=commission,
        signal_momentum=signal_mom, signal_atr=(risk / 2.5 if risk else None),
        risk_price=risk)


# ─── stats ─────────────────────────────────────────────────────────────────

def test_two_proportion_significant_drop():
    t = two_proportion_test(40, 100, 10, 100)
    assert t is not None and t.diff < 0 and t.p_value < 0.05


def test_spearman_monotone():
    assert spearman_rho([0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


# ─── trade derived ──────────────────────────────────────────────────────────

def test_net_and_win():
    t = mk(1, net=12.0, swap=-1.0, commission=-0.5)
    assert t.net_usd == pytest.approx(12.0)
    assert t.gross_usd == pytest.approx(13.5)
    assert t.is_win and t.is_decided and not t.is_loss


def test_r_multiple_price_based():
    # long, entry 1.10, exit 1.103, risk_price 0.001 → R = 0.003/0.001 = 3.0
    t = mk(1, net=5.0, entry=1.10, exit_px=1.103, risk=0.001)
    assert t.r_multiple == pytest.approx(3.0)
    # short зеркально
    s = mk(2, net=5.0, side="short", entry=1.10, exit_px=1.097, risk=0.001)
    assert s.r_multiple == pytest.approx(3.0)


def test_r_multiple_none_without_risk():
    assert mk(1, net=1.0, risk=None).r_multiple is None


def test_session_buckets():
    # ts_open % 86400 → час UTC. _BASE_TS соответствует ~01:33 UTC (asia)
    assert mk(1, net=1.0).session in {"asia", "london", "ny", "late"}
    # 10:00 UTC → london
    t = mk(1, net=1.0, ts_open=10 * 3600)
    assert t.session == "london"


# ─── pnl ────────────────────────────────────────────────────────────────────

def test_summarize_net_and_breakdown():
    trades = [mk(1, net=10.0, swap=-1.0), mk(2, net=-4.0, swap=-2.0)]
    p = summarize(trades)
    assert p.n_decided == 2 and p.wins == 1
    assert p.net == pytest.approx(6.0)
    assert p.swap == pytest.approx(-3.0)


def test_summarize_by_symbol_sorted_worst_first():
    trades = [mk(1, net=10.0, symbol="EURUSD"), mk(2, net=-8.0, symbol="GBPUSD")]
    rows = summarize_by_symbol(trades)
    assert rows[0].symbol == "GBPUSD"  # худший сверху


# ─── grading ────────────────────────────────────────────────────────────────

def test_grade_curve_monotone_predictive():
    # сильнее сигнал → выше R: монотонная предиктивная кривая
    trades = []
    pid = 1
    for mom, r in [(0.001, -1.0), (0.003, 0.0), (0.006, 1.0), (0.01, 2.0)]:
        for _ in range(5):
            # net знак = знак r; risk фикс 0.001, exit под нужный R
            entry = 1.10
            exit_px = entry + r * 0.001
            trades.append(MomentumTrade(
                position_id=pid, symbol="EURUSD", side="long", ts_open=_BASE_TS,
                ts_close=_BASE_TS + 60, entry=entry, exit=exit_px,
                volume_units=10000, gross_usd=r, swap_usd=0.0, commission_usd=0.0,
                signal_momentum=mom, signal_atr=0.0004, risk_price=0.001))
            pid += 1
    curve = grade_curve(trades, buckets=4, min_rho=0.5)
    assert curve is not None and curve.monotonic and curve.rho > 0.5


# ─── detectors ──────────────────────────────────────────────────────────────

def test_signal_not_predictive_flags_noise():
    # сила сигнала не связана с исходом (рандомный знак R по бакетам) → не монотонна
    trades = []
    pid = 1
    for mom, r in [(0.001, 2.0), (0.003, -1.0), (0.006, 1.5), (0.01, -2.0)]:
        for _ in range(8):
            entry = 1.10
            exit_px = entry + r * 0.001
            trades.append(MomentumTrade(
                position_id=pid, symbol="EURUSD", side="long", ts_open=_BASE_TS,
                ts_close=_BASE_TS + 60, entry=entry, exit=exit_px,
                volume_units=10000, gross_usd=r, swap_usd=0.0, commission_usd=0.0,
                signal_momentum=mom, signal_atr=0.0004, risk_price=0.001))
            pid += 1
    out = detect_signal_not_predictive(trades, bot="momentum", mode="live",
                                       buckets=4, min_rho=0.5, min_trades=10)
    assert out and out[0].code == "signal_not_predictive"


def test_symbol_session_leak_requires_overall_positive():
    # общий EXP положителен (EURUSD сильно в плюс), GBPUSD-срез системно в минус
    trades = [mk(i, net=20.0, symbol="EURUSD", entry=1.10, exit_px=1.104,
                 risk=0.001) for i in range(1, 26)]
    trades += [mk(100 + i, net=-10.0, symbol="GBPUSD", entry=1.30, exit_px=1.298,
                  risk=0.001) for i in range(1, 26)]
    out = detect_symbol_session_leak(trades, bot="momentum", mode="live",
                                     min_trades=20)
    codes = {(f.code, f.scope.get("symbol")) for f in out}
    assert ("symbol_session_leak", "GBPUSD") in codes


def test_loss_cluster_relative():
    # базовая доля убытков ~50%; на (GBPUSD, short) — 100% (≥1.5× базы)
    trades = [mk(i, net=5.0, symbol="EURUSD") for i in range(1, 21)]
    trades += [mk(100 + i, net=-5.0, symbol="EURUSD") for i in range(1, 21)]
    trades += [mk(200 + i, net=-5.0, symbol="GBPUSD", side="short")
               for i in range(1, 21)]
    out = detect_loss_cluster(trades, bot="momentum", mode="live", factor=1.5,
                              min_trades=20)
    assert any(f.scope == {"symbol": "GBPUSD", "side": "short"} for f in out)


def test_swap_drag_detects_financing_bleed():
    # gross+, но swap съедает >20% валовой прибыли
    trades = [mk(i, net=10.0, swap=-3.0, symbol="AUDUSD") for i in range(1, 21)]
    out = detect_swap_drag(trades, bot="momentum", mode="live", min_frac=0.2,
                           min_trades=20)
    assert out and out[0].code == "swap_drag" and out[0].scope == {"symbol": "AUDUSD"}


def test_swap_drag_quiet_when_swap_small():
    trades = [mk(i, net=10.0, swap=-0.5, symbol="AUDUSD") for i in range(1, 21)]
    out = detect_swap_drag(trades, bot="momentum", mode="live", min_frac=0.2,
                           min_trades=20)
    assert out == []


def test_overtrading_hot_hours_worse():
    cfg_min = 10
    trades = []
    pid = 1
    # спокойные часы: по 1 сделке, R=+2
    for h in range(10):
        entry = 1.10
        trades.append(MomentumTrade(
            position_id=pid, symbol="EURUSD", side="long",
            ts_open=h * 3600.0, ts_close=h * 3600.0 + 60, entry=entry,
            exit=entry + 0.002, volume_units=10000, gross_usd=2.0, swap_usd=0.0,
            commission_usd=0.0, signal_momentum=0.005, signal_atr=0.0004,
            risk_price=0.001))
        pid += 1
    # один горячий час: 15 сделок, R=-1
    for _ in range(15):
        entry = 1.10
        trades.append(MomentumTrade(
            position_id=pid, symbol="EURUSD", side="long",
            ts_open=20 * 3600.0, ts_close=20 * 3600.0 + 60, entry=entry,
            exit=entry - 0.001, volume_units=10000, gross_usd=-1.0, swap_usd=0.0,
            commission_usd=0.0, signal_momentum=0.005, signal_atr=0.0004,
            risk_price=0.001))
        pid += 1
    out = detect_overtrading(trades, bot="momentum", mode="live",
                             spike_factor=2.0, min_trades=cfg_min)
    assert out and out[0].code == "overtrading"


# ─── engine: тема №1 + sample-гейт ───────────────────────────────────────────

def test_engine_top_theme_and_sample_gate():
    cfg = TradecardMomentumSettings(regime_leak_min_trades=20,
                                    min_trades_for_theme=100)
    trades = [mk(i, net=20.0, symbol="EURUSD", entry=1.10, exit_px=1.104,
                 risk=0.001) for i in range(1, 26)]
    trades += [mk(100 + i, net=-10.0, symbol="GBPUSD", entry=1.30,
                  exit_px=1.298, risk=0.001) for i in range(1, 26)]
    res = run_detection(trades, cfg=cfg)
    assert res.top_theme is not None
    # n=25 < 100 → ниже порога темы → НАБЛЮДЕНИЕ
    assert res.sample_ok is False


# ─── five why prompt/parse ───────────────────────────────────────────────────

def test_five_why_prompt_and_parse():
    samples = [mk(1, net=-5.0, symbol="GBPUSD", side="short")]
    prompt = build_prompt(code="loss_cluster", scope={"symbol": "GBPUSD"}, n=20,
                          wr=0.3, exp_r=-0.4, net=-100.0, samples=samples)
    assert "loss_cluster" in prompt and "GBPUSD" in prompt
    chain, hyp = parse_response(
        "WHY1: a\nWHY2: b\nWHY3: c\nWHY4: d\nWHY5: e\nГИПОТЕЗА: добавить ADX-гейт")
    assert len(chain) == 5 and "ADX" in hyp


# ─── broker helpers ──────────────────────────────────────────────────────────

def test_scale_price_heuristic():
    assert _scale_price(1.10532) == pytest.approx(1.10532)
    assert _scale_price(110532) == pytest.approx(110532)  # FX price scale, не делим
    assert _scale_price(110532000) == pytest.approx(1105.32)  # явно scaled


def test_match_decision_nearest_in_window():
    decs = [
        EntryDecision(ts=1000.0, symbol_yf="EURUSD=X", direction="long",
                      momentum_value=0.004, atr=0.0004, note="ok"),
        EntryDecision(ts=1500.0, symbol_yf="EURUSD=X", direction="long",
                      momentum_value=0.006, atr=0.0005, note="ok"),
    ]
    m = _match_decision(decs, symbol_yf="EURUSD=X", side="long", ts_open=1490.0,
                        window_sec=900.0)
    assert m is not None and m.momentum_value == pytest.approx(0.006)
    # вне окна
    assert _match_decision(decs, symbol_yf="EURUSD=X", side="long",
                           ts_open=5000.0, window_sec=900.0) is None
    # неверная сторона
    assert _match_decision(decs, symbol_yf="EURUSD=X", side="short",
                           ts_open=1490.0, window_sec=900.0) is None


def test_parse_dt():
    assert _parse_dt("2026-06-23 09:26:46") is not None
    assert _parse_dt("") is None


# ─── momentum_db read-only инвариант ─────────────────────────────────────────

def test_momentum_db_readonly(tmp_path):
    from tradecard_momentum.data.momentum_db import MomentumDBReadOnly
    db_path = tmp_path / "momentum_bot.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE momentum_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT, symbol TEXT, direction TEXT, momentum_value REAL, "
        "atr REAL, close_price REAL, executed INTEGER, note TEXT)")
    conn.execute(
        "INSERT INTO momentum_decisions(created_at,symbol,direction,"
        "momentum_value,atr,close_price,executed,note) VALUES "
        "('2026-06-23 09:26:46','EURUSD=X','long',0.005,0.0004,1.1,1,'live_open:ok')")
    conn.commit()
    conn.close()

    with MomentumDBReadOnly(str(db_path)) as ro:
        decs = ro.executed_decisions()
        assert len(decs) == 1 and decs[0].symbol_yf == "EURUSD=X"
        # запись физически невозможна (mode=ro)
        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("INSERT INTO momentum_decisions(symbol) VALUES ('X')")


# ─── tradecard DB (своё хранилище) ───────────────────────────────────────────

def test_tradecard_db_theme_and_freq(tmp_path):
    db = TradecardDB(str(tmp_path / "tc.sqlite"))
    try:
        tid = db.upsert_theme(bot="momentum", mode="live", code="loss_cluster",
                              scope={"symbol": "GBPUSD"}, week="2026-25")
        # идемпотентность
        tid2 = db.upsert_theme(bot="momentum", mode="live", code="loss_cluster",
                               scope={"symbol": "GBPUSD"}, week="2026-26")
        assert tid == tid2
        db.record_freq(theme_id=tid, bot="momentum", mode="live", week="2026-25",
                       n_pattern=10, n_trades=100)
        rows = db.freq_history(tid, "live")
        assert rows[0]["freq_per_100"] == pytest.approx(10.0)
    finally:
        db.close()


def test_small_win_observation_below_threshold(tmp_path):
    db = TradecardDB(str(tmp_path / "tc.sqlite"))
    try:
        tid = db.upsert_theme(bot="momentum", mode="live", code="loss_cluster",
                              scope={}, week="2026-20")
        hid = db.add_hypothesis(theme_id=tid, bot="momentum", text="ADX-гейт")
        db.record_freq(theme_id=tid, bot="momentum", mode="live", week="2026-20",
                       n_pattern=30, n_trades=100)  # baseline
        db.record_freq(theme_id=tid, bot="momentum", mode="live", week="2026-22",
                       n_pattern=2, n_trades=30)     # OOS мал
        chk = evaluate_small_win(db, hypothesis_id=hid, theme_id=tid, mode="live",
                                 implemented_week="2026-21", min_trades=100,
                                 min_weeks=2, significance_p=0.05)
        assert chk.status == "observation"  # OOS ниже порога → НЕ победа
    finally:
        db.close()
