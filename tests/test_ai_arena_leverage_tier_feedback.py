"""Tests for v2.y user-approved exception: Performance Self-Reflection by Leverage Tier.

Source: правило ``ai-arena-sources.mdc`` § «Допустимые исключения по решению
пользователя» (2026-05-21).

Покрытие:
1. ``AiArenaStore.get_pnl_by_leverage_tier()`` — корректный биннинг по
   tier'ам gist confidence→leverage mapping (1-3x / 4-8x / 9-20x).
2. ``_format_leverage_tier_block`` — форматирование под source layout.
3. ``build_user_prompt`` integration: блок появляется в Performance
   Metrics секции и не ломает структуру USER_PROMPT.
4. Edge cases: empty, only one tier, mixed wins/losses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_arena.llm.prompts import _format_leverage_tier_block, build_user_prompt
from ai_arena.state.db import AiArenaStore


def _new_store(tmp_path: Path) -> AiArenaStore:
    return AiArenaStore(tmp_path / "ai_arena_lev.sqlite")


def _open_and_close(
    store: AiArenaStore,
    *,
    leverage: int,
    realized_pnl: float,
    confidence: float = 0.5,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.01,
    entry: float = 100000.0,
    exit_price: float = 100100.0,
) -> int:
    """Helper: открывает + закрывает позицию с given leverage и PnL."""
    pid = store.open_position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        sl_price=99000.0,
        tp_price=101000.0,
        leverage=leverage,
        order_link_id=f"arena_test_{pid_counter()}",
        llm_justification="test",
        confidence=confidence,
        invalidation_condition="test",
        risk_usd=10.0,
    )
    store.close_position(
        pid,
        exit_price=exit_price,
        realized_pnl_usd=realized_pnl,
        close_reason="test",
    )
    return pid


_counter = [0]


def pid_counter() -> int:
    _counter[0] += 1
    return _counter[0]


# ─── Тесты get_pnl_by_leverage_tier ──────────────────────────────────────────


class TestPnlByLeverageTierAggregation:
    def test_empty_store_returns_three_zero_tiers(self, tmp_path):
        store = _new_store(tmp_path)
        result = store.get_pnl_by_leverage_tier()
        assert len(result) == 3
        assert [t["label"] for t in result] == ["1-3x", "4-8x", "9-20x"]
        for tier in result:
            assert tier["n_trades"] == 0
            assert tier["n_wins"] == 0
            assert tier["sum_pnl"] == 0.0
            assert tier["avg_pnl"] == 0.0

    def test_low_tier_aggregates_1_to_3x(self, tmp_path):
        store = _new_store(tmp_path)
        _open_and_close(store, leverage=1, realized_pnl=5.0)
        _open_and_close(store, leverage=2, realized_pnl=-3.0)
        _open_and_close(store, leverage=3, realized_pnl=10.0)
        # 4x уже в medium
        _open_and_close(store, leverage=4, realized_pnl=100.0)

        result = store.get_pnl_by_leverage_tier()
        low = next(t for t in result if t["label"] == "1-3x")
        assert low["n_trades"] == 3
        assert low["n_wins"] == 2  # 5.0, 10.0 (положительные)
        assert low["sum_pnl"] == pytest.approx(12.0)  # 5 - 3 + 10
        assert low["avg_pnl"] == pytest.approx(4.0)

    def test_medium_tier_aggregates_4_to_8x(self, tmp_path):
        store = _new_store(tmp_path)
        _open_and_close(store, leverage=4, realized_pnl=-10.0)
        _open_and_close(store, leverage=5, realized_pnl=-50.0)
        _open_and_close(store, leverage=8, realized_pnl=20.0)
        _open_and_close(store, leverage=9, realized_pnl=999.0)  # high tier

        result = store.get_pnl_by_leverage_tier()
        medium = next(t for t in result if t["label"] == "4-8x")
        assert medium["n_trades"] == 3
        assert medium["n_wins"] == 1
        assert medium["sum_pnl"] == pytest.approx(-40.0)
        assert medium["avg_pnl"] == pytest.approx(-40.0 / 3, rel=1e-3)

    def test_high_tier_aggregates_9_to_20x(self, tmp_path):
        store = _new_store(tmp_path)
        _open_and_close(store, leverage=9, realized_pnl=100.0)
        _open_and_close(store, leverage=15, realized_pnl=-200.0)
        _open_and_close(store, leverage=20, realized_pnl=300.0)

        result = store.get_pnl_by_leverage_tier()
        high = next(t for t in result if t["label"] == "9-20x")
        assert high["n_trades"] == 3
        assert high["n_wins"] == 2
        assert high["sum_pnl"] == pytest.approx(200.0)

    def test_zero_pnl_counts_as_loss(self, tmp_path):
        """realized_pnl=0 не считается win (строгое > 0)."""
        store = _new_store(tmp_path)
        _open_and_close(store, leverage=2, realized_pnl=0.0)
        result = store.get_pnl_by_leverage_tier()
        low = next(t for t in result if t["label"] == "1-3x")
        assert low["n_trades"] == 1
        assert low["n_wins"] == 0

    def test_open_positions_excluded_from_aggregation(self, tmp_path):
        """Открытые позиции (closed_at IS NULL) не учитываются."""
        store = _new_store(tmp_path)
        # Открытая позиция (без close)
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.01, entry_price=100000.0,
            sl_price=99000.0, tp_price=101000.0, leverage=2,
            order_link_id="arena_open", llm_justification="test",
            confidence=0.5, invalidation_condition="test", risk_usd=10.0,
        )
        # Закрытая позиция
        _open_and_close(store, leverage=2, realized_pnl=5.0)

        result = store.get_pnl_by_leverage_tier()
        low = next(t for t in result if t["label"] == "1-3x")
        assert low["n_trades"] == 1  # только closed
        assert low["sum_pnl"] == pytest.approx(5.0)


# ─── Тесты _format_leverage_tier_block ───────────────────────────────────────


class TestFormatLeverageTierBlock:
    def test_none_returns_insufficient_history(self):
        assert "insufficient history" in _format_leverage_tier_block(None)

    def test_all_zero_tiers_returns_insufficient_history(self):
        empty = [
            {"label": "1-3x", "lev_min": 1, "lev_max": 3,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
            {"label": "4-8x", "lev_min": 4, "lev_max": 8,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
            {"label": "9-20x", "lev_min": 9, "lev_max": 20,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
        ]
        assert "insufficient history" in _format_leverage_tier_block(empty)

    def test_partial_tiers_show_per_tier_lines(self):
        stats = [
            {"label": "1-3x", "lev_min": 1, "lev_max": 3,
             "n_trades": 5, "n_wins": 3, "sum_pnl": 12.5, "avg_pnl": 2.5},
            {"label": "4-8x", "lev_min": 4, "lev_max": 8,
             "n_trades": 2, "n_wins": 0, "sum_pnl": -50.0, "avg_pnl": -25.0},
            {"label": "9-20x", "lev_min": 9, "lev_max": 20,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
        ]
        out = _format_leverage_tier_block(stats)
        assert "1-3x: n=5" in out
        assert "wins=3 (60%)" in out
        assert "+$2.50" in out
        assert "+$12.50" in out
        assert "4-8x: n=2" in out
        assert "wins=0 (0%)" in out
        assert "-$25.00" in out
        assert "-$50.00" in out
        assert "9-20x: n=0 (no data)" in out

    def test_format_uses_signed_pnl(self):
        stats = [
            {"label": "1-3x", "lev_min": 1, "lev_max": 3,
             "n_trades": 1, "n_wins": 1, "sum_pnl": 100.0, "avg_pnl": 100.0},
            {"label": "4-8x", "lev_min": 4, "lev_max": 8,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
            {"label": "9-20x", "lev_min": 9, "lev_max": 20,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
        ]
        out = _format_leverage_tier_block(stats)
        # знак `+` обязательно для positive (чтобы LLM не путался)
        assert "+$100.00" in out


# ─── Integration tests: build_user_prompt с leverage_stats ──────────────────


class TestUserPromptIntegration:
    def _build(self, leverage_stats=None) -> str:
        return build_user_prompt(
            minutes_elapsed=180,
            per_symbol_blocks="### ALL BTC DATA\n(test)",
            total_return_pct=-1.5,
            sharpe=-0.05,
            cash=9500.0,
            equity=9850.0,
            open_positions_block="[]",
            leverage_stats=leverage_stats,
        )

    def test_prompt_contains_performance_by_leverage_tier_section(self):
        out = self._build(leverage_stats=None)
        assert "Performance by Leverage Tier" in out
        assert "cumulative since experiment start" in out

    def test_prompt_with_none_leverage_stats_shows_insufficient_history(self):
        out = self._build(leverage_stats=None)
        assert "insufficient history" in out

    def test_prompt_with_real_stats_shows_per_tier_breakdown(self):
        stats = [
            {"label": "1-3x", "lev_min": 1, "lev_max": 3,
             "n_trades": 12, "n_wins": 5, "sum_pnl": 30.05, "avg_pnl": 2.5},
            {"label": "4-8x", "lev_min": 4, "lev_max": 8,
             "n_trades": 13, "n_wins": 3, "sum_pnl": -348.40, "avg_pnl": -26.80},
            {"label": "9-20x", "lev_min": 9, "lev_max": 20,
             "n_trades": 0, "n_wins": 0, "sum_pnl": 0.0, "avg_pnl": 0.0},
        ]
        out = self._build(leverage_stats=stats)
        # Все 3 tier'а в выводе
        assert "1-3x: n=12" in out
        assert "4-8x: n=13" in out
        assert "9-20x: n=0" in out
        # Под Performance Metrics, не где-то ещё
        perf_idx = out.find("**Performance Metrics:**")
        acc_idx = out.find("**Account Status:**")
        tier_idx = out.find("Performance by Leverage Tier")
        assert perf_idx < tier_idx < acc_idx

    def test_prompt_existing_metrics_intact(self):
        """Регресс: добавление leverage_tier не сломало Sharpe/Total Return."""
        out = self._build(leverage_stats=None)
        assert "Current Total Return (percent): -1.50%" in out
        assert "Sharpe Ratio: -0.050" in out
        assert "Available Cash: $9500.00" in out

    def test_prompt_default_arg_works_for_backward_compat(self):
        """Старые тесты вызывают build_user_prompt без leverage_stats —
        не должно ломаться (default None → insufficient history)."""
        out = build_user_prompt(
            minutes_elapsed=60,
            per_symbol_blocks="x",
            total_return_pct=0.0,
            sharpe=None,
            cash=1000.0,
            equity=1000.0,
            open_positions_block="[]",
        )
        assert "Performance by Leverage Tier" in out
        assert "insufficient history" in out
