"""Юнит-тесты tradecard_bybit (advisory-ревьюер scalp_bot / flowzone_bot).

Все цели — чистая детерминированная логика (без сети/биржи). Фикстуры честные:
поля trades задаются явно для проверки ЛОГИКИ детекторов/грейдинга, без рисовки
OHLC «под сигнал» (no-data-fitting.mdc — этот запрет про входные сигналы страт,
не про агрегаты ретро-детекторов).
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from tradecard_bybit.analysis.detectors import (
    detect_big_game_hunting, detect_exit_left_money, detect_factor_noise,
    detect_grade_not_predictive, detect_overtrading,
    detect_paper_live_divergence, detect_sl_cluster,
    detect_strategy_regime_leak)
from tradecard_bybit.analysis.engine import run_detection
from tradecard_bybit.analysis.grading import grade_curve
from tradecard_bybit.analysis.pnl import bybit_net, summarize_mode
from tradecard_bybit.analysis.small_wins import evaluate_small_win
from tradecard_bybit.analysis.stats import spearman_rho, two_proportion_test
from tradecard_bybit.analysis.trade import Trade
from tradecard_bybit.config.settings import TradecardBybitSettings
from tradecard_bybit.data.bot_db import BotDBReadOnly
from tradecard_bybit.data.reasons import factor_tokens, parse_reasons
from tradecard_bybit.llm.five_why import build_prompt, parse_response, run_five_why
from tradecard_bybit.state.db import TradecardDB


# ─── helpers ─────────────────────────────────────────────────────────────

_BASE_TS = 1_700_000_000.0  # фикс. UTC epoch (детерминизм сессии/недели)


def mk(tid: int, *, score: int, pnl: float, strategy: str = "sweep_fade",
       symbol: str = "BTCUSDT", side: str = "long", mode: str = "live",
       close_reason: str = "tp_hit", reasons: str = "sweep,cvd_div,reclaim,mom",
       ts_open: float = _BASE_TS, entry: float = 100.0, sl: float = 99.0,
       qty: float = 1.0, exit_px: float | None = None,
       verified: int = 0, provisional: int = 0) -> Trade:
    """Сделка с risk=qty*|entry-sl|=1 → r_multiple == pnl (удобно для EXP)."""
    src = "verified" if verified else ("provisional" if provisional else "db")
    return Trade(
        id=tid, bot="scalp", ts_open=ts_open, symbol=symbol, side=side, qty=qty,
        entry=entry, sl=sl, tp=103.0, score=score, reasons_raw=reasons,
        mode=mode, strategy=strategy, status="closed", ts_close=ts_open + 60,
        exit=exit_px if exit_px is not None else 101.0, pnl_usd=pnl,
        fees_usd=0.0, close_reason=close_reason, pnl_provisional=provisional,
        pnl_verified=verified, pnl_source=src)


# ─── reasons parsing ───────────────────────────────────────────────────────

def test_parse_reasons_scalp_flat():
    assert parse_reasons("sweep,cvd_div,reclaim,mom,ob_imb") == [
        "sweep", "cvd_div", "reclaim", "mom", "ob_imb"]
    assert parse_reasons("") == []
    assert parse_reasons(None) == []


def test_factor_tokens_flowzone_structural():
    # flowzone: structural ctx=/zone=/tp= раскладываются на атомы
    toks = factor_tokens("ctx=down,zone=val+poc,tp=swing,absorb_seller")
    assert "ctx:down" in toks
    assert "zone:val" in toks and "zone:poc" in toks
    assert "tp:swing" in toks
    assert "absorb_seller" in toks


def test_factor_tokens_dedup():
    assert factor_tokens("sweep,sweep,mom") == ["sweep", "mom"]


# ─── Trade derived metrics ─────────────────────────────────────────────────

def test_trade_r_multiple_and_decided():
    t = mk(1, score=4, pnl=2.5)  # risk=1 → R=2.5
    assert t.is_decided and t.is_win
    assert t.r_multiple == pytest.approx(2.5)


def test_non_trade_excluded_from_decided():
    t = mk(1, score=4, pnl=0.0, close_reason="restart_flat")
    assert t.is_non_trade
    assert not t.is_decided


def test_session_classification():
    # 00:00 UTC = asia, 09:00 = london, 14:00 = ny, 22:00 = late
    day = 1_700_000_000.0 - (1_700_000_000.0 % 86400.0)
    assert mk(1, score=1, pnl=1, ts_open=day + 1 * 3600).session == "asia"
    assert mk(2, score=1, pnl=1, ts_open=day + 9 * 3600).session == "london"
    assert mk(3, score=1, pnl=1, ts_open=day + 14 * 3600).session == "ny"
    assert mk(4, score=1, pnl=1, ts_open=day + 22 * 3600).session == "late"


# ─── stats helpers ─────────────────────────────────────────────────────────

def test_spearman_monotonic():
    assert spearman_rho([0, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert spearman_rho([0, 1, 2, 3], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_two_proportion_significance():
    # сильное снижение частоты на большой выборке → значимо
    t = two_proportion_test(80, 100, 20, 100)
    assert t is not None and t.diff < 0 and t.significant
    # одинаковые доли → не значимо
    t2 = two_proportion_test(50, 100, 50, 100)
    assert t2 is not None and not t2.significant


# ─── grading §5 ─────────────────────────────────────────────────────────────

def test_grade_curve_predictive_monotonic():
    # высокий score (5) — винеры, низкий (1) — лузеры → монотонна
    trades = ([mk(i, score=1, pnl=-1.0, close_reason="sl_hit") for i in range(10)]
              + [mk(100 + i, score=5, pnl=2.0) for i in range(10)])
    curve = grade_curve(trades, buckets=2, min_rho=0.5)
    assert curve is not None and curve.predictive
    assert curve.rho is not None and curve.rho > 0


def test_grade_curve_not_predictive():
    # инверсия: высокий score — лузеры → НЕ монотонна
    trades = ([mk(i, score=1, pnl=2.0) for i in range(10)]
              + [mk(100 + i, score=5, pnl=-1.0, close_reason="sl_hit")
                 for i in range(10)])
    curve = grade_curve(trades, buckets=2, min_rho=0.5)
    assert curve is not None and not curve.predictive


def test_detect_grade_not_predictive():
    trades = ([mk(i, score=1, pnl=2.0) for i in range(10)]
              + [mk(100 + i, score=5, pnl=-1.0, close_reason="sl_hit")
                 for i in range(10)])
    found = detect_grade_not_predictive(
        trades, bot="scalp", mode="live", buckets=2, min_rho=0.5, min_trades=4)
    assert len(found) == 1
    assert found[0].code == "grade_not_predictive"


# ─── detectors §4 ───────────────────────────────────────────────────────────

def test_strategy_regime_leak():
    # страта в общем плюс, но на ETHUSDT системно минус
    winners = [mk(i, score=4, pnl=1.0, symbol="BTCUSDT") for i in range(30)]
    leak = [mk(100 + i, score=4, pnl=-1.0, symbol="ETHUSDT",
               close_reason="sl_hit") for i in range(20)]
    found = detect_strategy_regime_leak(
        winners + leak, bot="scalp", mode="live", min_trades=20)
    codes = [(f.scope.get("symbol")) for f in found]
    assert "ETHUSDT" in codes


def test_sl_cluster():
    # базовая SL-доля низкая, но XRPUSDT short — сплошные стопы
    base = [mk(i, score=4, pnl=1.0, symbol="BTCUSDT") for i in range(40)]
    cluster = [mk(200 + i, score=4, pnl=-1.0, symbol="XRPUSDT", side="short",
                  close_reason="sl_hit") for i in range(25)]
    found = detect_sl_cluster(base + cluster, bot="scalp", mode="live",
                              factor=1.5, min_trades=20)
    assert any(f.scope.get("symbol") == "XRPUSDT" for f in found)


def test_factor_noise():
    # фактор 'ob_imb' присутствует/отсутствует, но EXP одинаков → noise
    withf = [mk(i, score=5, pnl=1.0, reasons="sweep,cvd_div,reclaim,mom,ob_imb")
             for i in range(30)]
    without = [mk(100 + i, score=4, pnl=1.0, reasons="sweep,cvd_div,reclaim,mom")
               for i in range(30)]
    found = detect_factor_noise(withf + without, bot="scalp", mode="live",
                                max_exp_frac=0.1, min_trades=20)
    assert any(f.scope.get("factor") == "ob_imb" for f in found)


def test_overtrading():
    day = _BASE_TS - (_BASE_TS % 86400.0)
    # спокойные часы: по 2 сделки, прибыльные
    calm = []
    for h in range(10):
        for j in range(2):
            calm.append(mk(h * 10 + j, score=4, pnl=1.0,
                           ts_open=day + h * 3600 + j))
    # один перегретый час: 30 сделок, убыточные
    hot = [mk(1000 + i, score=4, pnl=-0.5, close_reason="sl_hit",
              ts_open=day + 20 * 3600 + i) for i in range(30)]
    found = detect_overtrading(calm + hot, bot="scalp", mode="live",
                               spike_factor=2.0, min_trades=20)
    assert len(found) == 1 and found[0].code == "overtrading"


def test_paper_live_divergence():
    paper = [mk(i, score=4, pnl=1.0, mode="paper", symbol="SOLUSDT")
             for i in range(25)]
    live = [mk(100 + i, score=4, pnl=-1.0, mode="live", symbol="SOLUSDT",
               close_reason="sl_hit") for i in range(25)]
    found = detect_paper_live_divergence(paper + live, bot="scalp",
                                         min_trades=20)
    assert len(found) == 1 and found[0].code == "paper_live_divergence"


def test_big_game_hunting():
    # baseline (score 1-2) прибылен, редкий A+ (score 9) не бьёт baseline
    baseline = [mk(i, score=1, pnl=1.0) for i in range(40)]
    baseline += [mk(500 + i, score=2, pnl=1.0) for i in range(40)]
    aplus = [mk(1000 + i, score=9, pnl=0.5) for i in range(5)]
    found = detect_big_game_hunting(
        baseline + aplus, bot="scalp", mode="live", max_top_share=0.15,
        min_trades=30, buckets=3)
    assert any(f.code == "big_game_hunting" for f in found)


def test_exit_left_money_with_mfe():
    # выход на +1 (exit=101, entry=100), но потом цена прошла ещё +5 → MFE≫
    trades = [mk(i, score=4, pnl=1.0, close_reason="tp_hit", exit_px=101.0)
              for i in range(25)]

    def mfe_fn(t: Trade) -> float:
        return 5.0  # post-exit favorable excursion (в цене)

    found = detect_exit_left_money(
        trades, bot="scalp", mode="live", factor=2.0, min_trades=20,
        mfe_fn=mfe_fn)
    assert len(found) == 1 and found[0].code == "exit_left_money"


def test_exit_left_money_no_provider_silent():
    trades = [mk(i, score=4, pnl=1.0, close_reason="tp_hit") for i in range(25)]
    assert detect_exit_left_money(trades, bot="scalp", mode="live", factor=2.0,
                                  min_trades=20, mfe_fn=None) == []


# ─── engine: per-mode isolation + theme ranking ────────────────────────────

def test_engine_separates_modes_and_picks_theme():
    cfg = TradecardBybitSettings(regime_leak_min_trades=20,
                                 min_trades_for_theme=100)
    winners = [mk(i, score=4, pnl=1.0, symbol="BTCUSDT") for i in range(30)]
    leak = [mk(100 + i, score=4, pnl=-2.0, symbol="ETHUSDT",
               close_reason="sl_hit") for i in range(25)]
    res = run_detection(winners + leak, bot="scalp", cfg=cfg)
    assert res.top_theme is not None
    # тема №1 — самый дорогой убыток (ETHUSDT leak), но n<100 → НЕ sample_ok
    assert res.top_theme.net < 0
    assert not res.sample_ok


# ─── closedPnl ground truth §3.2 ────────────────────────────────────────────

class _FakeSession:
    """Мок pybit HTTP с пагинацией get_closed_pnl."""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self._calls = 0

    def get_closed_pnl(self, **kwargs):
        idx = self._calls
        self._calls += 1
        if idx >= len(self._pages):
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        cursor = "next" if idx < len(self._pages) - 1 else ""
        return {"retCode": 0,
                "result": {"list": self._pages[idx], "nextPageCursor": cursor}}


def test_bybit_full_pagination():
    from tradecard_bybit.data.bybit_client import TradecardBybitReadOnly
    client = TradecardBybitReadOnly.__new__(TradecardBybitReadOnly)
    client._session = _FakeSession([
        [{"closedPnl": "1.0"}, {"closedPnl": "2.0"}],
        [{"closedPnl": "-0.5"}],
    ])
    client._category = "linear"
    rows = client.fetch_closed_pnl(start_ms=0, end_ms=1000)
    assert len(rows) == 3
    net, n = bybit_net(rows)
    assert n == 3 and net == pytest.approx(2.5)


def test_pnl_prefers_verified_source():
    trades = [mk(1, score=4, pnl=1.0, verified=1),
              mk(2, score=4, pnl=2.0, provisional=1),
              mk(3, score=4, pnl=3.0)]
    summ = summarize_mode(trades, "live")
    assert summ.n_verified == 1 and summ.n_provisional == 1 and summ.n_db_only == 1
    assert summ.net_db == pytest.approx(6.0)


# ─── 5 Why §6 ───────────────────────────────────────────────────────────────

def test_five_why_prompt_includes_canon():
    samples = [mk(1, score=4, pnl=-1.0, close_reason="sl_hit")]
    prompt = build_prompt(code="strategy_regime_leak", strategy="sweep_fade",
                          scope={"symbol": "ETHUSDT"}, n=50, wr=0.3,
                          exp_r=-0.4, net=-20.0, samples=samples)
    assert "CAP order-flow" in prompt   # канон страты в промпте
    assert "ГИПОТЕЗА" in prompt


def test_five_why_parse_response():
    text = ("WHY1: рынок трендовый\nWHY2: фейд против тренда\n"
            "WHY3: нет фильтра режима\nWHY4: ADX не учитывается на ETH\n"
            "WHY5: сетап валиден только в рейндже\n"
            "ГИПОТЕЗА: добавить session/режим-гейт для ETHUSDT в рейндже")
    chain, hyp = parse_response(text)
    assert len(chain) == 5
    assert hyp.startswith("добавить session")
    assert len(hyp) <= 200


def test_run_five_why_with_mock_client():
    class _MockClient:
        def ask(self, system, user):
            from tradecard_bybit.llm.client import LlmResponse
            return LlmResponse(
                text=("WHY1: a\nWHY2: b\nWHY3: c\nWHY4: d\nWHY5: e\n"
                      "ГИПОТЕЗА: добавить фильтр режима"),
                tokens_input=1, tokens_output=1, cost_usd=0.0)

    res = run_five_why(_MockClient(), code="strategy_regime_leak",
                       strategy="sweep_fade", scope={}, n=120, wr=0.3,
                       exp_r=-0.4, net=-30.0, samples=[])
    assert res.error is None
    assert len(res.chain) == 5 and "фильтр режима" in res.hypothesis


# ─── small wins §7 (OOS-гейт) ──────────────────────────────────────────────

def test_small_win_not_counted_in_sample(tmp_path):
    db = TradecardDB(str(tmp_path / "tc.sqlite"))
    tid = db.upsert_theme(bot="scalp", mode="live", code="sl_cluster",
                          scope={"symbol": "X"}, week="2026-10")
    hyp = db.add_hypothesis(theme_id=tid, bot="scalp", text="гипотеза")
    # только baseline-недели (до внедрения), нет OOS-наблюдений
    db.record_freq(theme_id=tid, bot="scalp", mode="live", week="2026-10",
                   n_pattern=30, n_trades=100)
    db.set_hypothesis_status(hyp, "implemented", implemented_week="2026-11")
    chk = evaluate_small_win(db, hypothesis_id=hyp, theme_id=tid, mode="live",
                             implemented_week="2026-11", min_trades=100,
                             min_weeks=2, significance_p=0.05)
    # нет OOS-выборки → НЕ победа (observation / no_change), статус не small_win
    assert chk.status != "small_win"
    db.close()


def test_small_win_counted_only_oos(tmp_path):
    db = TradecardDB(str(tmp_path / "tc.sqlite"))
    tid = db.upsert_theme(bot="scalp", mode="live", code="sl_cluster",
                          scope={"symbol": "X"}, week="2026-10")
    hyp = db.add_hypothesis(theme_id=tid, bot="scalp", text="гипотеза")
    # baseline: высокая частота паттерна
    db.record_freq(theme_id=tid, bot="scalp", mode="live", week="2026-09",
                   n_pattern=80, n_trades=100)
    db.record_freq(theme_id=tid, bot="scalp", mode="live", week="2026-10",
                   n_pattern=80, n_trades=100)
    db.set_hypothesis_status(hyp, "implemented", implemented_week="2026-11")
    # OOS (≥2 недели, ≥100 сделок) с резким снижением частоты
    db.record_freq(theme_id=tid, bot="scalp", mode="live", week="2026-11",
                   n_pattern=10, n_trades=100)
    db.record_freq(theme_id=tid, bot="scalp", mode="live", week="2026-12",
                   n_pattern=10, n_trades=100)
    chk = evaluate_small_win(db, hypothesis_id=hyp, theme_id=tid, mode="live",
                             implemented_week="2026-11", min_trades=100,
                             min_weeks=2, significance_p=0.05)
    assert chk.status == "small_win"
    assert chk.p_value is not None and chk.p_value < 0.05
    db.close()


# ─── read-only инвариант §11 ────────────────────────────────────────────────

_SCALP_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, entry REAL NOT NULL, sl REAL NOT NULL, tp REAL NOT NULL,
    score INTEGER NOT NULL, reasons TEXT NOT NULL, mode TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'sweep_fade', status TEXT NOT NULL DEFAULT 'open',
    entry_order_id TEXT, ts_close REAL, exit REAL, pnl_usd REAL, fees_usd REAL,
    close_reason TEXT, pnl_provisional INTEGER NOT NULL DEFAULT 0,
    pnl_verified INTEGER NOT NULL DEFAULT 0
);
"""


def _make_scalp_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCALP_SCHEMA)
    conn.execute(
        "INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,reasons,"
        "mode,strategy,status,ts_close,exit,pnl_usd,fees_usd,close_reason,"
        "pnl_provisional,pnl_verified) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_BASE_TS, "BTCUSDT", "long", 1.0, 100.0, 99.0, 103.0, 4,
         "sweep,cvd_div,reclaim,mom", "live", "sweep_fade", "closed",
         _BASE_TS + 60, 101.0, 1.5, 0.0, "tp_hit", 0, 1))
    conn.commit()
    conn.close()


def test_bot_db_loads_trades(tmp_path):
    path = str(tmp_path / "scalp_bot.sqlite")
    _make_scalp_db(path)
    with BotDBReadOnly(path, "scalp") as db:
        trades = db.closed_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t.bot == "scalp" and t.symbol == "BTCUSDT"
    assert t.pnl_source == "verified"  # pnl_verified=1 → ground truth
    assert t.r_multiple == pytest.approx(1.5)
    assert t.reasons == ["sweep", "cvd_div", "reclaim", "mom"]


def test_bot_db_is_read_only(tmp_path):
    path = str(tmp_path / "scalp_bot.sqlite")
    _make_scalp_db(path)
    db = BotDBReadOnly(path, "scalp")
    # запись в БД бота физически невозможна (mode=ro)
    with pytest.raises(sqlite3.OperationalError):
        db._conn.execute(
            "UPDATE trades SET pnl_usd=999 WHERE id=1")
    db.close()


def test_bot_db_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        BotDBReadOnly(str(tmp_path / "nope.sqlite"), "scalp")
