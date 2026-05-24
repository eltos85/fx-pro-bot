"""Tests for v2.z3 user-approved exception #4: server-side notional cap.

Source: правило ``.cursor/rules/ai-arena-sources.mdc`` § «Допустимые
исключения по решению пользователя» (исключение #4, 2026-05-22).

Покрытие:
1. ``apply_notional_cap`` — pure-функция расчёта rescale/reject.
2. ``format_rescale_notice`` — текст блока для USER_PROMPT.
3. Integration: ``build_user_prompt(..., rescale_notice=...)`` корректно
   вставляет блок и не ломает структуру при ``None``.
4. Edge-cases: cap отключён (pct=1.0), отрицательный price/qty, cap-too-small.
"""
from __future__ import annotations

from ai_arena.llm.prompts import build_user_prompt
from ai_arena.trading.notional_cap import (
    CapResult,
    apply_notional_cap,
    format_rescale_notice,
)


# ─── apply_notional_cap ──────────────────────────────────────────────────────


class TestApplyNotionalCap:
    def test_within_cap_no_rescale(self):
        # qty=1 BTC × $100 000 = $100 000, cap = 0.30 × $1 000 000 = $300 000
        r = apply_notional_cap(
            requested_qty=1.0,
            price=100_000.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=1_000_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rescaled is False
        assert r.rejected is False
        assert r.capped_qty == 1.0
        assert r.original_notional == 100_000.0
        assert r.max_notional == 300_000.0

    def test_at_cap_boundary_no_rescale(self):
        # qty=1 × $3000 = $3000, cap = 0.30 × $10000 = $3000 → ровно на границе
        r = apply_notional_cap(
            requested_qty=1.0,
            price=3000.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rescaled is False
        assert r.rejected is False
        assert r.capped_qty == 1.0

    def test_above_cap_rescales(self):
        # SOLUSDT-like случай: qty=545 × $87.14 = $47491.30
        # cap = 0.30 × $10000 = $3000. capped_qty = floor($3000 / $87.14, step=0.1)
        # = floor(34.428, 0.1) = 34.4
        r = apply_notional_cap(
            requested_qty=545.0,
            price=87.14,
            qty_step=0.1,
            min_order_qty=0.1,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rescaled is True
        assert r.rejected is False
        assert r.original_qty == 545.0
        assert abs(r.original_notional - 47491.30) < 0.01
        assert r.capped_qty == 34.4
        assert r.capped_notional <= 3000.0  # ровно или чуть ниже cap (qty_step округление)
        assert r.max_notional == 3000.0

    def test_above_cap_too_small_rejects(self):
        # cap=$3, price=$87 → max_qty = 0.0345, qty_step=0.1, min_order=0.1
        # capped_qty = floor(0.0345, 0.1) = 0 < min_order → reject
        r = apply_notional_cap(
            requested_qty=545.0,
            price=87.14,
            qty_step=0.1,
            min_order_qty=0.1,
            virtual_capital_usd=10.0,
            max_allocation_pct=0.30,
        )
        assert r.rejected is True
        assert r.rescaled is False
        assert r.reject_reason == "cap_too_small_to_open"

    def test_pct_one_disables_cap(self):
        # max_allocation_pct=1.0, virtual_capital=$10000 → cap=$10000.
        # Запрашиваем notional = $5000 (< cap) → no rescale.
        r = apply_notional_cap(
            requested_qty=1.0,
            price=5_000.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=1.0,
        )
        assert r.rescaled is False
        assert r.rejected is False
        # Но notional > cap всё равно может сработать → проверяем второй кейс:
        r2 = apply_notional_cap(
            requested_qty=1.0,
            price=15_000.0,  # notional $15000 > cap $10000
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=1.0,
        )
        assert r2.rescaled is True  # cap=$10k всё ещё сработает

    def test_invalid_price_rejects(self):
        r = apply_notional_cap(
            requested_qty=1.0,
            price=0.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rejected is True
        assert r.reject_reason == "invalid_price"

    def test_invalid_capital_rejects(self):
        r = apply_notional_cap(
            requested_qty=1.0,
            price=100.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=0.0,
            max_allocation_pct=0.30,
        )
        assert r.rejected is True
        assert r.reject_reason == "invalid_cap_config"

    def test_invalid_pct_rejects(self):
        r = apply_notional_cap(
            requested_qty=1.0,
            price=100.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.0,
        )
        assert r.rejected is True
        assert r.reject_reason == "invalid_cap_config"

    def test_zero_qty_rejects(self):
        r = apply_notional_cap(
            requested_qty=0.0,
            price=100.0,
            qty_step=0.001,
            min_order_qty=0.001,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rejected is True
        assert r.reject_reason == "non_positive_qty"

    def test_qty_step_rounds_down(self):
        # cap=$3000, price=$100 → max_qty=30. qty_step=2 → floor(30, 2)=30
        # qty_step=7 → floor(30, 7)=28
        r = apply_notional_cap(
            requested_qty=100.0,
            price=100.0,
            qty_step=7.0,
            min_order_qty=1.0,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert r.rescaled is True
        assert r.capped_qty == 28.0  # floor(30/1, 7) = 28
        assert r.capped_notional == 2800.0


# ─── format_rescale_notice ───────────────────────────────────────────────────


class TestFormatRescaleNotice:
    def _rescaled_cap(self) -> CapResult:
        return CapResult(
            original_qty=545.0,
            original_notional=47491.30,
            capped_qty=34.4,
            capped_notional=2997.62,
            max_notional=3000.0,
            rescaled=True,
            rejected=False,
        )

    def _rejected_cap(self) -> CapResult:
        return CapResult(
            original_qty=545.0,
            original_notional=47491.30,
            capped_qty=0.0,
            capped_notional=0.0,
            max_notional=3.0,
            rescaled=False,
            rejected=True,
            reject_reason="cap_too_small_to_open",
        )

    def test_rescaled_notice_contains_key_info(self):
        notice = format_rescale_notice(
            coin="SOL",
            side="Buy",
            cap=self._rescaled_cap(),
            leverage=3,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert "System notice" in notice
        assert "SOL" in notice
        assert "BUY" in notice
        assert "lev=3x" in notice
        assert "545" in notice  # original
        assert "34.4" in notice  # capped
        assert "30%" in notice
        assert "$3,000.00" in notice  # max_notional
        assert "rescaled" in notice.lower()

    def test_rejected_notice_says_no_position(self):
        notice = format_rescale_notice(
            coin="SOL",
            side="Buy",
            cap=self._rejected_cap(),
            leverage=3,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert "No position was opened" in notice
        assert "cap_too_small_to_open" in notice

    def test_short_side_uppercase(self):
        notice = format_rescale_notice(
            coin="BTC",
            side="Sell",
            cap=self._rescaled_cap(),
            leverage=5,
            virtual_capital_usd=10_000.0,
            max_allocation_pct=0.30,
        )
        assert "SELL" in notice
        assert "lev=5x" in notice


# ─── build_user_prompt с rescale_notice ──────────────────────────────────────


class TestUserPromptRescaleIntegration:
    def _build(self, rescale_notice=None) -> str:
        return build_user_prompt(
            minutes_elapsed=180,
            per_symbol_blocks="### TEST",
            total_return_pct=-3.5,
            sharpe=-0.05,
            cash=9500.0,
            equity=9650.0,
            open_positions_block="[]",
            rescale_notice=rescale_notice,
        )

    def test_no_notice_no_block_in_prompt(self):
        out = self._build(rescale_notice=None)
        assert "System notice" not in out

    def test_empty_string_treated_as_none(self):
        # store.kv_get может вернуть "" после clear → не считаем за notice
        out = self._build(rescale_notice="")
        assert "System notice" not in out

    def test_with_notice_appears_before_market_state(self):
        notice = "⚠️ **System notice (previous cycle):** Test rescale notice."
        out = self._build(rescale_notice=notice)
        assert notice in out
        # Position: notice до "## CURRENT MARKET STATE"
        notice_idx = out.find("System notice")
        market_idx = out.find("## CURRENT MARKET STATE FOR ALL COINS")
        assert 0 < notice_idx < market_idx

    def test_notice_does_not_break_existing_blocks(self):
        notice = "⚠️ **System notice (previous cycle):** Test."
        out = self._build(rescale_notice=notice)
        # Все привычные секции на месте
        assert "**Performance Metrics:**" in out
        assert "Performance by Leverage Tier" in out
        assert "Performance by Symbol" in out
        assert "**Account Status:**" in out
        assert "**Current Live Positions" in out

    def test_default_arg_works_for_backward_compat(self):
        # Старые тесты вызывают без rescale_notice — должно не сломаться
        out = build_user_prompt(
            minutes_elapsed=60,
            per_symbol_blocks="x",
            total_return_pct=0.0,
            sharpe=None,
            cash=1000.0,
            equity=1000.0,
            open_positions_block="[]",
        )
        assert "System notice" not in out
