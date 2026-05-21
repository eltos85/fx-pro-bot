"""Tests for ``compute_scaled_account`` (offset-based equity scaling).

Source: правило ``ai-arena-sources.mdc`` § «Equity scaling — offset-based»
(L130-133):

    scaled_equity = virtual_capital_usd + (real_equity_now − real_at_start)
    scaled_cash   = real_available_cash − (real_at_start − virtual_capital_usd)
    total_return_pct = cumulative_real_pnl / virtual_capital_usd × 100

v2.x bug-fix (2026-05-21): убран clamp ``scaled_cash < 0 → 0`` — был
наша отсебятина, не из правила. См. BUILDLOG_AI_ARENA.md.
"""
from __future__ import annotations

import pytest

from ai_arena.app.scaling import compute_scaled_account


class TestComputeScaledAccountFormula:
    def test_no_pnl_no_open_positions(self):
        """Чистый старт: real_equity == anchor, real_avail == real_equity.
        scaled_equity == scaled_cash == virtual_capital, total_return_pct=0.
        """
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=50000.0,
            real_at_start=50000.0,
            real_available_cash=50000.0,
            virtual_capital_usd=10000.0,
        )
        assert equity == pytest.approx(10000.0)
        assert cash == pytest.approx(10000.0)
        assert ret_pct == pytest.approx(0.0)

    def test_real_pnl_added_one_to_one(self):
        """Реальный PnL +$100 → scaled_equity = virtual + 100, не divisor."""
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=50100.0,
            real_at_start=50000.0,
            real_available_cash=50100.0,
            virtual_capital_usd=10000.0,
        )
        assert equity == pytest.approx(10100.0)
        assert cash == pytest.approx(10100.0)
        assert ret_pct == pytest.approx(1.0)  # 100/10000 * 100 = 1%

    def test_real_loss_subtracts_one_to_one(self):
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=49900.0,
            real_at_start=50000.0,
            real_available_cash=49900.0,
            virtual_capital_usd=10000.0,
        )
        assert equity == pytest.approx(9900.0)
        assert cash == pytest.approx(9900.0)
        assert ret_pct == pytest.approx(-1.0)


class TestScaledCashNoClamp:
    """v2.x bug-fix: scaled_cash может быть отрицательным —
    раньше был clamp до 0, ломавший канон-формулу sizing.
    """

    def test_overleveraged_state_returns_negative_cash(self):
        """Сильно загруженный margin: real_avail << anchor → scaled_cash < 0.

        Сценарий: anchor=$50000, virtual=$10000, real_equity=$50000 (без
        PnL пока), но позиции залочили margin → real_avail=$30000.
        По формуле: scaled_cash = 30000 - (50000 - 10000) = -10000.
        v2.x: показываем -10000 (LLM сигнал «нет margin»), не 0.
        """
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=50000.0,
            real_at_start=50000.0,
            real_available_cash=30000.0,
            virtual_capital_usd=10000.0,
        )
        assert equity == pytest.approx(10000.0)
        assert cash == pytest.approx(-10000.0)  # NOT clamped to 0
        assert ret_pct == pytest.approx(0.0)

    def test_partially_used_margin_returns_partial_cash(self):
        """Чуть-чуть margin занято: real_avail = anchor − $1000.
        scaled_cash = (anchor−1000) − (anchor−virtual) = virtual − 1000 = $9000.
        """
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=50000.0,
            real_at_start=50000.0,
            real_available_cash=49000.0,
            virtual_capital_usd=10000.0,
        )
        assert cash == pytest.approx(9000.0)

    def test_full_margin_used_zero_cash_passes_through(self):
        """real_avail = anchor − virtual точно → scaled_cash = 0 (не clamp,
        a математически).
        """
        equity, cash, _ = compute_scaled_account(
            real_equity_now=50000.0,
            real_at_start=50000.0,
            real_available_cash=40000.0,
            virtual_capital_usd=10000.0,
        )
        assert cash == pytest.approx(0.0)


class TestEdgeCases:
    def test_zero_virtual_capital_returns_zero_pct(self):
        """div/0 защита."""
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=50100.0,
            real_at_start=50000.0,
            real_available_cash=50100.0,
            virtual_capital_usd=0.0,
        )
        assert ret_pct == pytest.approx(0.0)

    def test_anchor_below_virtual_capital_inflates_cash(self):
        """Если anchor=$500 (ниже virtual=$1000) — scaled_cash > real_avail.
        Это редкий edge case (anchor обычно >> virtual в demo), но формула
        корректна.
        """
        equity, cash, ret_pct = compute_scaled_account(
            real_equity_now=500.0,
            real_at_start=500.0,
            real_available_cash=500.0,
            virtual_capital_usd=1000.0,
        )
        assert equity == pytest.approx(1000.0)
        assert cash == pytest.approx(1000.0)

    def test_cumulative_loss_negative_total_return(self):
        equity, _, ret_pct = compute_scaled_account(
            real_equity_now=49000.0,
            real_at_start=50000.0,
            real_available_cash=49000.0,
            virtual_capital_usd=10000.0,
        )
        assert equity == pytest.approx(9000.0)
        assert ret_pct == pytest.approx(-10.0)


class TestNoSilentClampRegression:
    """Защита от регрессии — если кто-то снова добавит clamp ``< 0 → 0``,
    тест упадёт. Прямая проверка против исторического баг-кода.
    """

    @pytest.mark.parametrize(
        "real_avail, expected_negative",
        [
            (29999.99, True),
            (40000.01, False),  # граница: чуть выше нуля
            (39999.99, True),  # граница: чуть ниже нуля
        ],
    )
    def test_negative_scaled_cash_passes_through(
        self, real_avail: float, expected_negative: bool
    ):
        _, cash, _ = compute_scaled_account(
            real_equity_now=50000.0,
            real_at_start=50000.0,
            real_available_cash=real_avail,
            virtual_capital_usd=10000.0,
        )
        if expected_negative:
            assert cash < 0, (
                f"v2.x bug-fix REGRESSION: scaled_cash должно быть < 0, "
                f"получено {cash} (clamp вернулся?)"
            )
        else:
            assert cash >= 0
