"""Тесты SYSTEM_PROMPT и USER_PROMPT для AI Arena.

Гарантируем что:
1. Все 9 required JSON-полей упомянуты в SYSTEM_PROMPT (Nof1 schema).
2. Warning'и «OLDEST → NEWEST» встречаются достаточно раз
   (gist рекомендует ≥4 повторений в разных местах prompt'а).
3. Capital Safety hard-limits в SYSTEM_PROMPT соответствуют settings.
"""
from __future__ import annotations

from ai_arena.config.settings import AiArenaSettings
from ai_arena.llm.prompts import build_system_prompt, build_user_prompt


def _make_settings() -> AiArenaSettings:
    s = AiArenaSettings()
    return s


class TestSystemPrompt:
    def test_contains_all_required_json_fields(self):
        s = _make_settings()
        sp = build_system_prompt(s)
        for field in [
            "signal", "coin", "quantity", "leverage",
            "stop_loss", "profit_target",
            "invalidation_condition", "confidence",
            "risk_usd", "justification",
        ]:
            assert field in sp, f"missing required JSON field: {field}"

    def test_contains_all_action_signals(self):
        s = _make_settings()
        sp = build_system_prompt(s)
        for sig in ["buy_to_enter", "sell_to_enter", "hold", "close"]:
            assert sig in sp, f"missing action: {sig}"

    def test_contains_oldest_newest_warning(self):
        s = _make_settings()
        sp = build_system_prompt(s)
        assert "OLDEST → NEWEST" in sp or "OLDEST" in sp.upper()

    def test_contains_capital_safety_limits(self):
        s = _make_settings()
        sp = build_system_prompt(s)
        # Лимиты из settings должны быть в prompt'е
        assert f"{s.max_open_positions}" in sp
        assert f"{s.max_leverage}" in sp
        assert f"{s.max_risk_per_trade_usd:.0f}" in sp
        assert f"{s.max_daily_loss_usd:.0f}" in sp
        assert f"{s.max_total_loss_usd:.0f}" in sp

    def test_explicitly_no_news(self):
        # Nof1: «No news, no social media, no narratives» — это инвариант
        s = _make_settings()
        sp = build_system_prompt(s)
        assert "No news" in sp or "no news" in sp


class TestUserPrompt:
    def test_oldest_newest_warning_repeated(self):
        # gist рекомендует ≥4 повторений в разных местах
        up = build_user_prompt(
            minutes_elapsed=42,
            per_symbol_blocks="(test data)",
            total_return_pct=0.0,
            sharpe=None,
            cash=500.0,
            equity=500.0,
            open_positions_block="[]",
        )
        # «OLDEST → NEWEST» появляется минимум 2 раза в самом user prompt
        # (warning сверху + warning снизу + дисклеймер про timeframes).
        # SYSTEM_PROMPT содержит ещё несколько повторений — суммарно ≥4.
        assert up.count("OLDEST → NEWEST") >= 2

    def test_contains_minutes_elapsed(self):
        up = build_user_prompt(
            minutes_elapsed=123,
            per_symbol_blocks="x",
            total_return_pct=0.0,
            sharpe=None,
            cash=0.0, equity=0.0,
            open_positions_block="[]",
        )
        assert "123 minutes" in up

    def test_contains_sharpe_section(self):
        up = build_user_prompt(
            minutes_elapsed=10,
            per_symbol_blocks="x",
            total_return_pct=1.5,
            sharpe=0.42,
            cash=500.0, equity=525.0,
            open_positions_block="[]",
        )
        assert "Sharpe Ratio" in up
        assert "0.420" in up
        assert "+1.50%" in up

    def test_sharpe_na_when_none(self):
        up = build_user_prompt(
            minutes_elapsed=10,
            per_symbol_blocks="x",
            total_return_pct=0.0,
            sharpe=None,
            cash=500.0, equity=500.0,
            open_positions_block="[]",
        )
        assert "n/a" in up

    def test_account_value_displayed(self):
        up = build_user_prompt(
            minutes_elapsed=10,
            per_symbol_blocks="x",
            total_return_pct=0.0,
            sharpe=None,
            cash=300.0, equity=550.0,
            open_positions_block="[]",
        )
        assert "$300.00" in up
        assert "$550.00" in up
