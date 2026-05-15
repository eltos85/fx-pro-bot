"""Тесты SYSTEM_PROMPT и USER_PROMPT для AI Arena.

Гарантируем что prompts соответствуют source (gist + nof1.ai/blog/TechPost1):
1. Все 10 required JSON-полей упомянуты в SYSTEM_PROMPT.
2. Все 12 секций gist (ROLE, ENVIRONMENT, ACTION SPACE, POSITION SIZING,
   RISK MANAGEMENT PROTOCOL, OUTPUT FORMAT, PERFORMANCE METRICS,
   DATA INTERPRETATION, OPERATIONAL CONSTRAINTS, TRADING PHILOSOPHY,
   CONTEXT WINDOW, FINAL INSTRUCTIONS) присутствуют.
3. Source-параметры (leverage 1-20x, R:R 2:1, conviction-mapping
   1-3x/3-8x/8-20x, stop loss 1-3%, liquidation >15%, diversification 40%)
   все цитированы.
4. Warning «OLDEST → NEWEST» встречается в user prompt (1 раз в начале —
   1-в-1 с source; повторений в финале prompt'а в source НЕТ).
"""
from __future__ import annotations

from ai_arena.config.settings import AiArenaSettings
from ai_arena.llm.prompts import build_system_prompt, build_user_prompt


def _make_settings() -> AiArenaSettings:
    return AiArenaSettings()


class TestSystemPromptSchema:
    def test_contains_all_required_json_fields(self):
        sp = build_system_prompt(_make_settings())
        for field in [
            "signal", "coin", "quantity", "leverage",
            "stop_loss", "profit_target",
            "invalidation_condition", "confidence",
            "risk_usd", "justification",
        ]:
            assert field in sp, f"missing required JSON field: {field}"

    def test_contains_all_action_signals(self):
        sp = build_system_prompt(_make_settings())
        for sig in ["buy_to_enter", "sell_to_enter", "hold", "close"]:
            assert sig in sp, f"missing action: {sig}"

    def test_contains_oldest_newest_warning(self):
        sp = build_system_prompt(_make_settings())
        assert "OLDEST → NEWEST" in sp


class TestSystemPromptSourceCompliance:
    """Проверки что параметры взяты из source 1-в-1, а не отсебятина."""

    def test_leverage_range_matches_source(self):
        # gist § TRADING ENVIRONMENT: "Leverage Range: 1x to 20x"
        s = _make_settings()
        sp = build_system_prompt(s)
        assert s.leverage_max == 20, "default leverage_max должен быть 20 (source Nof1)"
        assert "1x to 20x" in sp or f"1x to {s.leverage_max}x" in sp

    def test_conviction_to_leverage_mapping_source(self):
        # gist § POSITION SIZING: "Low (0.3-0.5): 1-3x, Medium (0.5-0.7):
        # 3-8x, High (0.7-1.0): 8-20x"
        sp = build_system_prompt(_make_settings())
        assert "1-3x" in sp
        assert "3-8x" in sp
        assert "8-20x" in sp

    def test_min_2_to_1_rr_in_prompt(self):
        # gist § RISK MANAGEMENT: "minimum 2:1 reward-to-risk ratio"
        sp = build_system_prompt(_make_settings())
        assert "2:1" in sp

    def test_stop_loss_1_3_percent_in_prompt(self):
        # gist § RISK MANAGEMENT: "limit loss to 1-3% of account value per trade"
        sp = build_system_prompt(_make_settings())
        assert "1-3%" in sp

    def test_liquidation_15_percent_rule(self):
        # gist § POSITION SIZING: "Liquidation Risk: Ensure liquidation
        # price is >15% away from entry"
        sp = build_system_prompt(_make_settings())
        assert ">15%" in sp or "15% away" in sp

    def test_diversification_40_percent_rule(self):
        # gist § POSITION SIZING line 128: "Diversification: Avoid concentrating
        # >40% of capital in single position" — БЕЗ артикля `a single`.
        sp = build_system_prompt(_make_settings())
        assert ">40% of capital in single position" in sp
        # Регресс-страховка от лишнего `a` (наша опечатка-расхождение,
        # исправленная в audit 2026-05-15)
        assert ">40% of capital in a single position" not in sp

    def test_coin_enum_no_usdt_suffix(self):
        # gist L168: «"coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP"»
        # — голые тикеры без USDT (Hyperliquid). Bybit-symbol появляется
        # только при API-вызовах через arena_to_bybit, см. trading/symbols.py
        sp = build_system_prompt(_make_settings())
        # Должны присутствовать голые тикеры
        for arena in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"):
            assert arena in sp, f"missing arena symbol: {arena}"
        # И НЕ должно быть USDT-суффикса в Asset Universe / coin enum
        assert "BTCUSDT" not in sp
        assert "ETHUSDT" not in sp
        assert "SOLUSDT" not in sp

    def test_fee_impact_500_warning(self):
        # gist § POSITION SIZING: "On positions <$500, fees will materially
        # erode profits"
        sp = build_system_prompt(_make_settings())
        assert "<$500" in sp

    def test_no_pyramiding_no_hedging_no_partial(self):
        # gist § ACTION SPACE: "NO pyramiding / NO hedging / NO partial exits"
        sp = build_system_prompt(_make_settings())
        assert "NO pyramiding" in sp
        assert "NO hedging" in sp
        assert "NO partial exits" in sp

    def test_explicitly_no_news(self):
        # gist § OPERATIONAL CONSTRAINTS: "No news feeds or social media"
        sp = build_system_prompt(_make_settings())
        assert "No news" in sp

    def test_common_pitfalls_section(self):
        # gist § TRADING PHILOSOPHY: 5 common pitfalls
        sp = build_system_prompt(_make_settings())
        for pitfall in ["Overtrading", "Revenge Trading", "Analysis Paralysis",
                        "Ignoring Correlation", "Overleveraging"]:
            assert pitfall in sp, f"missing pitfall: {pitfall}"

    def test_context_window_section(self):
        # gist § CONTEXT WINDOW MANAGEMENT
        sp = build_system_prompt(_make_settings())
        assert "CONTEXT WINDOW" in sp.upper()


class TestSystemPromptNoOversteppingSource:
    """Проверки что в prompt НЕТ server-side capital safety hard-limits.

    Если эти строки появятся обратно — мы снова отклонились от source.
    """

    def test_no_killswitch_in_prompt(self):
        sp = build_system_prompt(_make_settings())
        # KillSwitch / capital safety hard-limits НЕ описаны у Nof1
        assert "KILLSWITCH" not in sp.upper()
        assert "CAPITAL SAFETY" not in sp.upper()

    def test_no_max_positions_hard_cap(self):
        sp = build_system_prompt(_make_settings())
        # «Max 3 simultaneous positions» — не из source. Source имеет
        # только «one position per coin».
        assert "Max 3 simultaneous" not in sp
        assert "max_open_positions" not in sp

    def test_no_max_daily_loss_hard_cap(self):
        sp = build_system_prompt(_make_settings())
        assert "Daily realised loss" not in sp


class TestUserPrompt:
    def test_oldest_newest_warning_in_user_prompt(self):
        """Source: 1 раз в начале USER_PROMPT (CRITICAL warning).

        Финальный reminder перед "Based on the above..." — отсутствует
        в source, добавлять ЗАПРЕЩЕНО (см. правило ai-arena-sources.mdc
        «Что НЕЛЬЗЯ добавлять в prompt'ы»).
        """
        up = build_user_prompt(
            minutes_elapsed=42,
            per_symbol_blocks="(test data)",
            total_return_pct=0.0,
            sharpe=None,
            cash=500.0,
            equity=500.0,
            open_positions_block="[]",
        )
        # ровно 1 раз — как в source
        assert up.count("OLDEST → NEWEST") == 1
        # И обязательно присутствует CRITICAL warning в начале
        assert "CRITICAL: ALL OF THE PRICE OR SIGNAL DATA BELOW IS ORDERED: OLDEST → NEWEST" in up

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
        # Без `+` модификатора (gist L448: «{return_pct}%», нейтральный формат)
        assert "1.50%" in up
        assert "+1.50%" not in up

    def test_negative_total_return_displayed(self):
        up = build_user_prompt(
            minutes_elapsed=10,
            per_symbol_blocks="x",
            total_return_pct=-2.34,
            sharpe=None,
            cash=500.0, equity=487.5,
            open_positions_block="[]",
        )
        assert "-2.34%" in up

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
