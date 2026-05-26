"""Тесты v1.X self-reflection: per-symbol performance + recent trades.

Покрытие:
- ``AiFxTraderStore.get_pnl_by_symbol`` — фильтр ``is_paper=0``, n=0 fallback
- ``AiFxTraderStore.get_recent_closed_trades`` — limit, порядок,
  duration_minutes, clamp ``llm_reason``/``close_reason``
- ``format_performance_by_symbol`` — пустой / непустой
- ``format_recent_trades`` — пустой / непустой
- ``build_user_prompt`` — backward compat (default None) + с блоками
- ``build_user_prompt_review`` — backward compat + только performance

См. правила:
- ``.cursor/rules/sample-size.mdc`` — никаких hard-cap'ов, только
  информативный feedback (без него LLM не знает истории).
- ``BUILDLOG_AI_FX_TRADER.md`` запись v1.X.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fx_ai_trader.llm.prompts import (
    build_user_prompt,
    build_user_prompt_review,
    format_performance_by_symbol,
    format_recent_trades,
)
from fx_ai_trader.state.db import AiFxTraderStore


# ─── DB: get_pnl_by_symbol / get_recent_closed_trades ────────────────────


@pytest.fixture
def store(tmp_path: Path) -> AiFxTraderStore:
    return AiFxTraderStore(tmp_path / "fx_ai_trader.sqlite")


def _open_and_close(
    store: AiFxTraderStore,
    *,
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    pnl: float,
    is_paper: bool,
    llm_reason: str = "test setup",
    close_reason: str = "test close",
    volume_lots: float = 0.01,
    broker_position_id: int | None = None,
) -> int:
    pid = store.open_position(
        symbol=symbol,
        side=side,
        volume_lots=volume_lots,
        entry_price=entry,
        sl_price=entry - 1.0,
        tp_price=entry + 2.0,
        broker_position_id=broker_position_id,
        broker_order_label="ai-fx-trader",
        llm_reason=llm_reason,
        is_paper=is_paper,
    )
    store.close_position(
        pid,
        exit_price=exit_price,
        realized_pnl_usd=pnl,
        close_reason=close_reason,
    )
    return pid


class TestGetPnlBySymbol:
    def test_empty_store_returns_n_zero_for_each_symbol(self, store: AiFxTraderStore):
        rows = store.get_pnl_by_symbol(["XAUUSD", "BZ=F", "NG=F"])
        assert [r["symbol"] for r in rows] == ["XAUUSD", "BZ=F", "NG=F"]
        for r in rows:
            assert r["n"] == 0
            assert r["wins"] == 0
            assert r["win_rate_pct"] == 0.0
            assert r["avg_pnl_usd"] == 0.0
            assert r["sum_pnl_usd"] == 0.0

    def test_aggregates_only_live_trades(self, store: AiFxTraderStore):
        # 2 paper trades по NG=F (должны игнорироваться)
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=3.1, pnl=10.0, is_paper=True)
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=2.9, pnl=-10.0, is_paper=True)
        # 3 live trades по NG=F: 1 win, 2 loss
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=3.1, pnl=10.0, is_paper=False)
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=2.95, pnl=-5.0, is_paper=False)
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=2.9, pnl=-10.0, is_paper=False)

        rows = store.get_pnl_by_symbol(["NG=F"])
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == "NG=F"
        assert r["n"] == 3
        assert r["wins"] == 1
        assert r["win_rate_pct"] == pytest.approx(100 / 3, abs=0.01)
        assert r["avg_pnl_usd"] == pytest.approx(-5.0 / 3, abs=0.01)
        assert r["sum_pnl_usd"] == pytest.approx(-5.0, abs=0.01)

    def test_preserves_symbol_order(self, store: AiFxTraderStore):
        _open_and_close(store, symbol="BZ=F", side="BUY", entry=80.0, exit_price=81.0, pnl=10.0, is_paper=False)
        rows = store.get_pnl_by_symbol(["NG=F", "BZ=F", "XAUUSD"])
        assert [r["symbol"] for r in rows] == ["NG=F", "BZ=F", "XAUUSD"]
        assert rows[0]["n"] == 0   # NG=F
        assert rows[1]["n"] == 1   # BZ=F
        assert rows[2]["n"] == 0   # XAUUSD

    def test_win_rate_zero_pnl_is_not_win(self, store: AiFxTraderStore):
        # PnL == 0 трактуется как НЕ-win (break-even не победа)
        _open_and_close(store, symbol="XAUUSD", side="BUY", entry=2700.0, exit_price=2700.0, pnl=0.0, is_paper=False)
        rows = store.get_pnl_by_symbol(["XAUUSD"])
        assert rows[0]["n"] == 1
        assert rows[0]["wins"] == 0


class TestGetRecentClosedTrades:
    def test_empty_store_returns_empty_list(self, store: AiFxTraderStore):
        assert store.get_recent_closed_trades(limit=10) == []

    def test_ignores_paper_trades(self, store: AiFxTraderStore):
        _open_and_close(store, symbol="NG=F", side="BUY", entry=3.0, exit_price=3.1, pnl=10.0, is_paper=True)
        assert store.get_recent_closed_trades(limit=10) == []

    def test_returns_oldest_to_newest(self, store: AiFxTraderStore):
        ids = []
        for i in range(3):
            pid = _open_and_close(
                store, symbol="BZ=F", side="BUY",
                entry=80.0 + i, exit_price=81.0 + i, pnl=10.0,
                is_paper=False, llm_reason=f"trade_{i}",
            )
            ids.append(pid)
        trades = store.get_recent_closed_trades(limit=10)
        assert [t["id"] for t in trades] == ids  # oldest → newest

    def test_limit_keeps_most_recent(self, store: AiFxTraderStore):
        # 5 sequentially закрытых live-trades, limit=3 → последние 3 (id 3,4,5)
        for i in range(5):
            _open_and_close(
                store, symbol="NG=F", side="BUY",
                entry=3.0, exit_price=3.0 + i * 0.01, pnl=float(i),
                is_paper=False, llm_reason=f"r{i}",
            )
        trades = store.get_recent_closed_trades(limit=3)
        assert len(trades) == 3
        # Sorted oldest → newest, последние 3 (id 3,4,5)
        assert [t["id"] for t in trades] == [3, 4, 5]

    def test_clamps_long_reasons(self, store: AiFxTraderStore):
        long_text = "A" * 500
        _open_and_close(
            store, symbol="BZ=F", side="SELL",
            entry=80.0, exit_price=78.0, pnl=20.0, is_paper=False,
            llm_reason=long_text, close_reason=long_text,
        )
        trades = store.get_recent_closed_trades(limit=10, reason_clamp=180)
        assert len(trades) == 1
        assert len(trades[0]["llm_reason"]) == 180
        assert len(trades[0]["close_reason"]) == 180

    def test_duration_minutes_is_non_negative(self, store: AiFxTraderStore):
        _open_and_close(
            store, symbol="XAUUSD", side="BUY",
            entry=2700.0, exit_price=2705.0, pnl=5.0, is_paper=False,
        )
        trades = store.get_recent_closed_trades(limit=10)
        assert trades[0]["duration_minutes"] is not None
        assert trades[0]["duration_minutes"] >= 0


# ─── Форматтеры ──────────────────────────────────────────────────────────


class TestFormatPerformanceBySymbol:
    def test_empty_returns_empty_string(self):
        assert format_performance_by_symbol(None) == ""
        assert format_performance_by_symbol([]) == ""

    def test_n_zero_symbol_shown_explicitly(self):
        stats = [
            {"symbol": "NG=F", "n": 0, "wins": 0, "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0},
        ]
        out = format_performance_by_symbol(stats)
        assert "NG=F" in out
        assert "n=0" in out
        assert "no closed live trades yet" in out

    def test_non_empty_blocks_contain_header_and_lines(self):
        stats = [
            {"symbol": "XAUUSD", "n": 12, "wins": 7, "win_rate_pct": 58.3,
             "avg_pnl_usd": 2.15, "sum_pnl_usd": 25.80},
            {"symbol": "BZ=F", "n": 8, "wins": 4, "win_rate_pct": 50.0,
             "avg_pnl_usd": -0.85, "sum_pnl_usd": -6.80},
        ]
        out = format_performance_by_symbol(stats)
        assert "PERFORMANCE BY SYMBOL" in out
        assert "live, since experiment start" in out
        assert "XAUUSD: n=12, wins=7 (58.3%)" in out
        assert "BZ=F: n=8, wins=4 (50.0%)" in out
        assert "+25.80$" in out  # positive sum_pnl signed
        assert "-6.80$" in out


class TestFormatRecentTrades:
    def test_empty_returns_empty_string(self):
        assert format_recent_trades(None) == ""
        assert format_recent_trades([]) == ""

    def test_one_trade_render(self):
        trades = [
            {
                "id": 27, "symbol": "NG=F", "side": "BUY", "volume_lots": 0.06,
                "entry_price": 3.05, "exit_price": 3.044,
                "realized_pnl_usd": -14.40,
                "opened_at": "2026-05-25T14:55:46+00:00",
                "closed_at": "2026-05-25T15:11:39+00:00",
                "duration_minutes": 16,
                "llm_reason": "NOAA cold anomaly + STEO + 1H breakout above BB",
                "close_reason": "Macro bearish: storage build, mild weather",
            }
        ]
        out = format_recent_trades(trades)
        assert "RECENT CLOSED TRADES (last 1, oldest -> newest)" in out
        assert "[id=27] NG=F BUY 0.06 lots" in out
        assert "entry 3.05000 -> exit 3.04400" in out
        assert "pnl -14.40$, 16 min" in out
        assert "open : NOAA cold anomaly" in out
        assert "close: Macro bearish" in out

    def test_handles_missing_close_reason(self):
        trades = [
            {
                "id": 1, "symbol": "BZ=F", "side": "SELL", "volume_lots": 0.01,
                "entry_price": 80.0, "exit_price": 79.0,
                "realized_pnl_usd": 10.0,
                "opened_at": "2026-05-25T08:00:00+00:00",
                "closed_at": "2026-05-25T09:00:00+00:00",
                "duration_minutes": 60,
                "llm_reason": "trend short",
                "close_reason": "",
            }
        ]
        out = format_recent_trades(trades)
        assert "(broker auto / SL or TP)" in out


# ─── build_user_prompt[_review] integration ──────────────────────────────


class TestBuildUserPromptBackwardCompat:
    def test_no_params_returns_v10_layout(self):
        out = build_user_prompt("MARKET_CTX_HERE")
        assert out.startswith("Current market state")
        assert "MARKET_CTX_HERE" in out
        assert "PERFORMANCE BY SYMBOL" not in out
        assert "RECENT CLOSED TRADES" not in out

    def test_empty_strings_treated_as_none(self):
        out = build_user_prompt(
            "MARKET_CTX_HERE",
            performance_by_symbol="",
            recent_trades="",
        )
        assert "PERFORMANCE BY SYMBOL" not in out
        assert "RECENT CLOSED TRADES" not in out

    def test_blocks_inserted_before_market_context(self):
        out = build_user_prompt(
            "MARKET_CTX_HERE",
            performance_by_symbol="PERF_BLOCK",
            recent_trades="TRADES_BLOCK",
        )
        idx_perf = out.find("PERF_BLOCK")
        idx_trades = out.find("TRADES_BLOCK")
        idx_market = out.find("Current market state")
        assert 0 <= idx_perf < idx_trades < idx_market

    def test_self_reflection_step_mentioned_in_outro(self):
        out = build_user_prompt(
            "MARKET_CTX_HERE",
            performance_by_symbol="PERF_BLOCK",
            recent_trades="TRADES_BLOCK",
        )
        assert "SELF-REFLECTION" in out


class TestBuildUserPromptReviewBackwardCompat:
    def test_no_params_returns_v10_layout(self):
        out = build_user_prompt_review("REVIEW_CTX_HERE")
        assert out.startswith("Mid-cycle review")
        assert "REVIEW_CTX_HERE" in out
        assert "PERFORMANCE BY SYMBOL" not in out

    def test_performance_block_inserted(self):
        out = build_user_prompt_review(
            "REVIEW_CTX_HERE",
            performance_by_symbol="PERF_BLOCK",
        )
        idx_perf = out.find("PERF_BLOCK")
        idx_review = out.find("Mid-cycle review")
        assert 0 <= idx_perf < idx_review

    def test_review_does_not_accept_recent_trades(self):
        """review-cycle должен оставаться lightweight; recent_trades там
        не должны появляться по дизайну (см. SYSTEM_PROMPT_REVIEW
        comment в prompts.py)."""
        import inspect
        sig = inspect.signature(build_user_prompt_review)
        assert "recent_trades" not in sig.parameters
