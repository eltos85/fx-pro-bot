"""1-в-1 source compliance тесты для AI Arena.

Source: gist `nof1-prompt.md` by @wquguru
        https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83

Каждый тест **дословно цитирует** конкретную строку source и проверяет
что наш SYSTEM_PROMPT / USER_PROMPT / per-symbol block / open positions
block / output schema содержит ровно эту фразу — посимвольно.

Это защита от регрессии: если кто-то «улучшит» промпт перефразировкой,
переведёт на русский, добавит «полезную» аннотацию или сожмёт labels —
тест сломается. Любое расхождение должно либо обновлять source-цитату
(если source реально изменился), либо откатывать «улучшение».

Адаптации vs source разрешены ТОЛЬКО из списка в правиле
`.cursor/rules/ai-arena-sources.mdc` (Bybit ↔ Hyperliquid маппинг).
Все они задокументированы соответствующими тестами в этом файле
с пометкой `# Bybit-adaptation`.
"""
from __future__ import annotations

from ai_arena.config.settings import AiArenaSettings
from ai_arena.llm.prompts import build_system_prompt, build_user_prompt
from ai_arena.state.db import ArenaPosition
from ai_arena.trading.context import (
    SymbolBlock,
    format_open_positions_block,
    format_symbol_block,
)


def _make_settings() -> AiArenaSettings:
    return AiArenaSettings()


def _sp() -> str:
    return build_system_prompt(_make_settings())


# ─── ROLE & IDENTITY (gist L59-64) ──────────────────────────────────────


class TestRoleIdentitySource:
    def test_section_header_exact(self):
        # gist L59: `# ROLE & IDENTITY`
        assert "# ROLE & IDENTITY" in _sp()

    def test_designation_line_exact(self):
        # gist L63: `Your designation: AI Trading Model [MODEL_NAME]`
        # Мы подставляем реальный model name; начало строки — буквально.
        assert "Your designation: AI Trading Model " in _sp()

    def test_mission_line_exact(self):
        # gist L64
        assert (
            "Your mission: Maximize risk-adjusted returns (PnL) through "
            "systematic, disciplined trading." in _sp()
        )

    def test_exchange_is_bybit_not_hyperliquid(self):
        # Bybit-adaptation (правило ai-arena-sources.mdc § Bybit маппинг):
        # Hyperliquid → Bybit USDT-perp (demo). Source L61 говорит
        # «Hyperliquid», у нас — Bybit; единственно допустимое отклонение.
        sp = _sp()
        assert "Bybit USDT-perp" in sp
        assert "Hyperliquid" not in sp


# ─── TRADING ENVIRONMENT SPECIFICATION (gist L68-86) ────────────────────


class TestTradingEnvironmentSource:
    def test_section_headers_exact(self):
        sp = _sp()
        # gist L68
        assert "# TRADING ENVIRONMENT SPECIFICATION" in sp
        # gist L70
        assert "## Market Parameters" in sp
        # gist L79
        assert "## Trading Mechanics" in sp

    def test_decision_frequency_exact(self):
        # gist L76: `**Decision Frequency**: Every 2-3 minutes (mid-to-low frequency trading)`
        # Дословно «2-3» — описание характера mid-to-low frequency, не
        # точная конфигурация (наш poll_interval=180s попадает в диапазон).
        sp = _sp()
        assert "**Decision Frequency**: Every 2-3 minutes (mid-to-low frequency trading)" in sp

    def test_starting_capital_format_exact(self):
        # gist L62: `**Starting Capital**: $10,000 USD`. У нас sandbox
        # $1,000 (единственное обоснованное отклонение от source — см.
        # правило ai-arena-sources.mdc § Equity scaling). Формат с
        # разделителем тысяч обязателен.
        sp = _sp()
        assert "**Starting Capital**: $1,000 USD" in sp

    def test_asset_universe_format_exact(self):
        # gist L62: `**Asset Universe**: BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)`
        # Порядок DOGE, XRP (не XRP, DOGE) и хвост `(perpetual contracts)`
        # обязательны буквально.
        sp = _sp()
        assert (
            "**Asset Universe**: BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)"
            in sp
        )

    def test_market_hours_exact(self):
        # gist L75
        assert "**Market Hours**: 24/7 continuous trading" in _sp()

    def test_leverage_range_exact(self):
        # gist L77: `**Leverage Range**: 1x to 20x (use judiciously based on conviction)`
        sp = _sp()
        assert "**Leverage Range**: 1x to 20x (use judiciously based on conviction)" in sp

    def test_funding_mechanism_lines_exact(self):
        # gist L83-84
        sp = _sp()
        assert "Positive funding rate = longs pay shorts (bullish market sentiment)" in sp
        assert "Negative funding rate = shorts pay longs (bearish market sentiment)" in sp

    def test_trading_fees_line_exact(self):
        # gist L85
        assert "**Trading Fees**: ~0.02-0.05% per trade (maker/taker fees apply)" in _sp()

    def test_slippage_line_exact(self):
        # gist L86
        assert (
            "**Slippage**: Expect 0.01-0.1% on market orders depending on size"
            in _sp()
        )

    def test_contract_type_line_exact(self):
        # gist L81
        assert "**Contract Type**: Perpetual futures (no expiration)" in _sp()

    def test_funding_schedule_bybit_adaptation(self):
        # Bybit-adaptation (правило): Bybit funding 8h vs Hyperliquid 1h.
        # Одна строка в § Trading Mechanics — единственно допустимое
        # дополнение к source.
        assert "Bybit funding schedule: 00:00 / 08:00 / 16:00 UTC (every 8 hours)" in _sp()


# ─── ACTION SPACE DEFINITION (gist L90-110) ─────────────────────────────


class TestActionSpaceSource:
    def test_section_header_exact(self):
        # gist L90
        assert "# ACTION SPACE DEFINITION" in _sp()

    def test_intro_line_exact(self):
        # gist L92
        assert "You have exactly FOUR possible actions per decision cycle:" in _sp()

    def test_buy_to_enter_exact(self):
        # gist L94-95
        sp = _sp()
        assert "**buy_to_enter**: Open a new LONG position (bet on price appreciation)" in sp
        assert (
            "Use when: Bullish technical setup, positive momentum, "
            "risk-reward favors upside" in sp
        )

    def test_sell_to_enter_exact(self):
        # gist L97-98
        sp = _sp()
        assert "**sell_to_enter**: Open a new SHORT position (bet on price depreciation)" in sp
        assert (
            "Use when: Bearish technical setup, negative momentum, "
            "risk-reward favors downside" in sp
        )

    def test_hold_exact(self):
        # gist L100-101
        sp = _sp()
        assert "**hold**: Maintain current positions without modification" in sp
        assert (
            "Use when: Existing positions are performing as expected, "
            "or no clear edge exists" in sp
        )

    def test_close_exact(self):
        # gist L103-104
        sp = _sp()
        assert "**close**: Exit an existing position entirely" in sp
        assert "Use when: Profit target reached, stop loss triggered, or thesis invalidated" in sp

    def test_no_pyramiding_hedging_partial_exits_exact(self):
        # gist L108-110
        sp = _sp()
        assert (
            "**NO pyramiding**: Cannot add to existing positions "
            "(one position per coin maximum)" in sp
        )
        assert (
            "**NO hedging**: Cannot hold both long and short positions in the same asset"
            in sp
        )
        assert "**NO partial exits**: Must close entire position at once" in sp

    def test_position_management_constraints_header(self):
        # gist L106
        assert "## Position Management Constraints" in _sp()


# ─── POSITION SIZING FRAMEWORK (gist L114-130) ──────────────────────────


class TestPositionSizingSource:
    def test_section_header_exact(self):
        # gist L114
        assert "# POSITION SIZING FRAMEWORK" in _sp()

    def test_intro_line_exact(self):
        # gist L116
        assert "Calculate position size using this formula:" in _sp()

    def test_position_size_formula_exact(self):
        # gist L118-119 (без отступов в gist'е)
        sp = _sp()
        assert "Position Size (USD)" in sp
        assert "Available Cash × Leverage × Allocation %" in sp
        assert "Position Size (Coins)" in sp
        assert "Position Size (USD) / Current Price" in sp

    def test_sizing_considerations_header(self):
        # gist L121
        assert "## Sizing Considerations" in _sp()

    def test_available_capital_exact(self):
        # gist L123
        assert "**Available Capital**: Only use available cash (not account value)" in _sp()

    def test_conviction_to_leverage_mapping_exact(self):
        # gist L125-127
        sp = _sp()
        assert "Low conviction (0.3-0.5): Use 1-3x leverage" in sp
        assert "Medium conviction (0.5-0.7): Use 3-8x leverage" in sp
        assert "High conviction (0.7-1.0): Use 8-20x leverage" in sp

    def test_diversification_exact(self):
        # gist L128: `Diversification: Avoid concentrating >40% of capital in single position`
        # БЕЗ артикля `a single` (наша опечатка из BUILDLOG 2026-05-15).
        sp = _sp()
        assert (
            "**Diversification**: Avoid concentrating >40% of capital in single position"
            in sp
        )

    def test_fee_impact_exact(self):
        # gist L129
        assert "**Fee Impact**: On positions <$500, fees will materially erode profits" in _sp()

    def test_liquidation_risk_exact(self):
        # gist L130
        assert (
            "**Liquidation Risk**: Ensure liquidation price is >15% away from entry"
            in _sp()
        )


# ─── RISK MANAGEMENT PROTOCOL (gist L134-157) ───────────────────────────


class TestRiskManagementSource:
    def test_section_header_exact(self):
        # gist L134
        assert "# RISK MANAGEMENT PROTOCOL (MANDATORY)" in _sp()

    def test_intro_line_exact(self):
        # gist L136
        assert "For EVERY trade decision, you MUST specify:" in _sp()

    def test_profit_target_exact(self):
        # gist L138-140
        sp = _sp()
        assert "**profit_target** (float): Exact price level to take profits" in sp
        assert "Should offer minimum 2:1 reward-to-risk ratio" in sp
        assert (
            "Based on technical resistance levels, Fibonacci extensions, "
            "or volatility bands" in sp
        )

    def test_stop_loss_exact(self):
        # gist L142-144
        sp = _sp()
        assert "**stop_loss** (float): Exact price level to cut losses" in sp
        assert "Should limit loss to 1-3% of account value per trade" in sp
        assert "Placed beyond recent support/resistance to avoid premature stops" in sp

    def test_invalidation_condition_exact(self):
        # gist L146-148
        sp = _sp()
        assert (
            "**invalidation_condition** (string): Specific market signal "
            "that voids your thesis" in sp
        )
        assert (
            'Examples: "BTC breaks below $100k", "RSI drops below 30", '
            '"Funding rate flips negative"' in sp
        )
        assert "Must be objective and observable" in sp

    def test_confidence_levels_exact(self):
        # gist L150-154
        sp = _sp()
        assert "**confidence** (float, 0-1): Your conviction level in this trade" in sp
        assert "0.0-0.3: Low confidence (avoid trading or use minimal size)" in sp
        assert "0.3-0.6: Moderate confidence (standard position sizing)" in sp
        assert "0.6-0.8: High confidence (larger position sizing acceptable)" in sp
        assert "0.8-1.0: Very high confidence (use cautiously, beware overconfidence)" in sp

    def test_risk_usd_formula_exact(self):
        # gist L156-157 — формула после правки автором (комменты gist'а).
        # БЕЗ "× Leverage" (это была ошибка в первой версии, исправлено).
        sp = _sp()
        assert (
            "**risk_usd** (float): Dollar amount at risk "
            "(distance from entry to stop loss)" in sp
        )
        assert "Calculate as: |Entry Price - Stop Loss| × Position Size" in sp
        # Регресс-страховка: НЕТ умножения на leverage
        assert "× Position Size × Leverage" not in sp
        assert "Do NOT multiply by leverage" not in sp


# ─── OUTPUT FORMAT SPECIFICATION (gist L161-186) ────────────────────────


class TestOutputFormatSource:
    def test_section_header_exact(self):
        # gist L161
        assert "# OUTPUT FORMAT SPECIFICATION" in _sp()

    def test_intro_line_exact(self):
        # gist L163
        assert (
            "Return your decision as a **valid JSON object** with these exact fields:"
            in _sp()
        )

    def test_json_schema_all_fields_exact(self):
        # gist L165-178: 10 required полей. Проверяем каждое имя.
        sp = _sp()
        for field in [
            '"signal":', '"coin":', '"quantity":', '"leverage":',
            '"profit_target":', '"stop_loss":',
            '"invalidation_condition":', '"confidence":',
            '"risk_usd":', '"justification":',
        ]:
            assert field in sp, f"missing field in JSON schema: {field}"

    def test_signal_enum_exact(self):
        # gist L167
        sp = _sp()
        assert '"buy_to_enter" | "sell_to_enter" | "hold" | "close"' in sp

    def test_coin_enum_exact_arena_format(self):
        # gist L168 — БУКВАЛЬНО pipe-separated с кавычками для каждого
        # значения (1-в-1, не «<one of X, Y, Z>»-форма!):
        #   "coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP",
        sp = _sp()
        assert (
            '"coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP"' in sp
        ), "coin enum должен быть буквально pipe-separated, не <one of X, Y>"
        # Регрессы — старая форма не должна возвращаться
        assert "<one of " not in sp, (
            "<one of X, Y> — наша «удобная» форма, source использует pipe"
        )
        # USDT-суффикс быть НЕ должен (Bybit-mapping происходит позже)
        for usdt in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            assert usdt not in sp

    def test_position_size_formula_no_indent(self):
        # gist L65-66 — формула БЕЗ 4-пробельного отступа (не code-block,
        # просто строки с переносом). Иначе модель воспринимает как
        # отдельный синтаксический блок.
        sp = _sp()
        assert (
            "\nPosition Size (USD) = Available Cash × Leverage × Allocation %\n"
            in sp
        ), "Position Size формула должна быть БЕЗ 4-пробельного отступа"
        assert "    Position Size (USD)" not in sp, (
            "Регресс: 4-пробельный отступ возвращён"
        )

    def test_sharpe_formula_no_indent(self):
        # gist L194 — Sharpe формула тоже БЕЗ отступа (как Position Size).
        sp = _sp()
        assert (
            "\nSharpe Ratio = (Average Return - Risk-Free Rate) / "
            "Standard Deviation of Returns\n"
        ) in sp, "Sharpe формула должна быть БЕЗ 4-пробельного отступа"
        assert "    Sharpe Ratio = " not in sp

    def test_leverage_integer_1_to_20_exact(self):
        # gist L170: `"leverage": <integer 1-20>`
        assert "<integer 1-20>" in _sp()

    def test_confidence_range_exact(self):
        # gist L174: `"confidence": <float 0-1>`
        assert "<float 0-1>" in _sp()

    def test_output_validation_rules_header(self):
        # gist L180
        assert "## Output Validation Rules" in _sp()

    def test_validation_rule_positive_numbers_exact(self):
        # gist L182
        assert (
            'All numeric fields must be positive numbers '
            '(except when signal is "hold")' in _sp()
        )

    def test_validation_rule_target_above_entry_for_longs_exact(self):
        # gist L183
        assert "profit_target must be above entry price for longs, below for shorts" in _sp()

    def test_validation_rule_stop_below_entry_for_longs_exact(self):
        # gist L184
        assert "stop_loss must be below entry price for longs, above for shorts" in _sp()

    def test_validation_rule_justification_max_chars_exact(self):
        # gist L185
        assert "justification must be concise (max 500 characters)" in _sp()

    def test_validation_rule_hold_placeholders_exact(self):
        # gist L186
        assert (
            'When signal is "hold": Set quantity=0, leverage=1, '
            "and use placeholder values for risk fields" in _sp()
        )


# ─── PERFORMANCE METRICS & FEEDBACK (gist L190-204) ─────────────────────


class TestPerformanceMetricsSource:
    def test_section_header_exact(self):
        # gist L190
        assert "# PERFORMANCE METRICS & FEEDBACK" in _sp()

    def test_intro_line_exact(self):
        # gist L192
        assert "You will receive your Sharpe Ratio at each invocation:" in _sp()

    def test_sharpe_formula_exact(self):
        # gist L194
        assert (
            "Sharpe Ratio = (Average Return - Risk-Free Rate) / Standard Deviation of Returns"
            in _sp()
        )

    def test_sharpe_interpretation_intervals_exact(self):
        # gist L196-200 — 4 интервала
        sp = _sp()
        assert "< 0: Losing money on average" in sp
        assert "0-1: Positive returns but high volatility" in sp
        assert "1-2: Good risk-adjusted performance" in sp
        assert "> 2: Excellent risk-adjusted performance" in sp

    def test_calibration_low_high_exact(self):
        # gist L203-204
        sp = _sp()
        assert "Low Sharpe → Reduce position sizes, tighten stops, be more selective" in sp
        assert "High Sharpe → Current strategy is working, maintain discipline" in sp


# ─── DATA INTERPRETATION GUIDELINES (gist L208-246) ─────────────────────


class TestDataInterpretationSource:
    def test_section_header_exact(self):
        # gist L208
        assert "# DATA INTERPRETATION GUIDELINES" in _sp()

    def test_indicators_subheader_exact(self):
        # gist L210
        assert "## Technical Indicators Provided" in _sp()

    def test_ema_block_exact(self):
        # gist L212-214
        sp = _sp()
        assert "**EMA (Exponential Moving Average)**: Trend direction" in sp
        assert "Price > EMA = Uptrend" in sp
        assert "Price < EMA = Downtrend" in sp

    def test_macd_block_exact(self):
        # gist L216-218
        sp = _sp()
        assert "**MACD (Moving Average Convergence Divergence)**: Momentum" in sp
        assert "Positive MACD = Bullish momentum" in sp
        assert "Negative MACD = Bearish momentum" in sp

    def test_rsi_block_exact(self):
        # gist L220-223
        sp = _sp()
        assert "**RSI (Relative Strength Index)**: Overbought/Oversold conditions" in sp
        assert "RSI > 70 = Overbought (potential reversal down)" in sp
        assert "RSI < 30 = Oversold (potential reversal up)" in sp
        assert "RSI 40-60 = Neutral zone" in sp

    def test_atr_block_exact(self):
        # gist L225-227
        sp = _sp()
        assert "**ATR (Average True Range)**: Volatility measurement" in sp
        assert "Higher ATR = More volatile (wider stops needed)" in sp
        assert "Lower ATR = Less volatile (tighter stops possible)" in sp

    def test_open_interest_block_exact(self):
        # gist L229-232
        sp = _sp()
        assert "**Open Interest**: Total outstanding contracts" in sp
        assert "Rising OI + Rising Price = Strong uptrend" in sp
        assert "Rising OI + Falling Price = Strong downtrend" in sp
        assert "Falling OI = Trend weakening" in sp

    def test_funding_rate_block_exact(self):
        # gist L234-237
        sp = _sp()
        assert "**Funding Rate**: Market sentiment indicator" in sp
        assert "Positive funding = Bullish sentiment (longs paying shorts)" in sp
        assert "Negative funding = Bearish sentiment (shorts paying longs)" in sp
        assert "Extreme funding rates (>0.01%) = Potential reversal signal" in sp

    def test_data_ordering_warning_exact(self):
        # gist L239
        sp = _sp()
        assert "## Data Ordering (CRITICAL)" in sp
        # gist L241
        assert (
            "⚠️ **ALL PRICE AND INDICATOR DATA IS ORDERED: OLDEST → NEWEST**" in sp
        )
        # gist L243-244
        assert "**The LAST element in each array is the MOST RECENT data point.**" in sp
        assert "**The FIRST element is the OLDEST data point.**" in sp
        # gist L246
        assert (
            "Do NOT confuse the order. This is a common error that leads to "
            "incorrect decisions." in sp
        )


# ─── OPERATIONAL CONSTRAINTS (gist L250-265) ────────────────────────────


class TestOperationalConstraintsSource:
    def test_section_header_exact(self):
        # gist L250
        assert "# OPERATIONAL CONSTRAINTS" in _sp()

    def test_dont_have_subheader(self):
        # gist L252
        assert "## What You DON'T Have Access To" in _sp()

    def test_dont_have_lines_exact(self):
        # gist L254-258 — 5 явных запретов
        sp = _sp()
        assert "No news feeds or social media sentiment" in sp
        assert "No conversation history (each decision is stateless)" in sp
        assert "No ability to query external APIs" in sp
        assert "No access to order book depth beyond mid-price" in sp
        assert "No ability to place limit orders (market orders only)" in sp

    def test_must_infer_subheader(self):
        # gist L260
        assert "## What You MUST Infer From Data" in _sp()

    def test_must_infer_lines_exact(self):
        # gist L262-265
        sp = _sp()
        assert "Market narratives and sentiment (from price action + funding rates)" in sp
        assert "Institutional positioning (from open interest changes)" in sp
        assert "Trend strength and sustainability (from technical indicators)" in sp
        assert "Risk-on vs risk-off regime (from correlation across coins)" in sp


# ─── TRADING PHILOSOPHY (gist L269-293) ─────────────────────────────────


class TestTradingPhilosophySource:
    def test_section_header_exact(self):
        # gist L269
        assert "# TRADING PHILOSOPHY & BEST PRACTICES" in _sp()

    def test_core_principles_exact(self):
        # gist L273-277 — 5 принципов
        sp = _sp()
        assert (
            "**Capital Preservation First**: Protecting capital is more "
            "important than chasing gains" in sp
        )
        assert (
            "**Discipline Over Emotion**: Follow your exit plan, "
            "don't move stops or targets" in sp
        )
        assert (
            "**Quality Over Quantity**: Fewer high-conviction trades beat "
            "many low-conviction trades" in sp
        )
        assert "**Adapt to Volatility**: Adjust position sizes based on market conditions" in sp
        assert "**Respect the Trend**: Don't fight strong directional moves" in sp

    def test_common_pitfalls_exact(self):
        # gist L281-285 — 5 предупреждений
        sp = _sp()
        assert "⚠️ **Overtrading**: Excessive trading erodes capital through fees" in sp
        assert (
            "⚠️ **Revenge Trading**: Don't increase size after losses to "
            '"make it back"' in sp
        )
        assert (
            "⚠️ **Analysis Paralysis**: Don't wait for perfect setups, they don't exist"
            in sp
        )
        assert "⚠️ **Ignoring Correlation**: BTC often leads altcoins, watch BTC first" in sp
        assert "⚠️ **Overleveraging**: High leverage amplifies both gains AND losses" in sp

    def test_decision_making_framework_exact(self):
        # gist L289-293 — 5 шагов
        sp = _sp()
        assert "Analyze current positions first (are they performing as expected?)" in sp
        assert "Check for invalidation conditions on existing trades" in sp
        assert "Scan for new opportunities only if capital is available" in sp
        assert "Prioritize risk management over profit maximization" in sp
        assert 'When in doubt, choose "hold" over forcing a trade' in sp


# ─── CONTEXT WINDOW MANAGEMENT (gist L297-307) ──────────────────────────


class TestContextWindowSource:
    def test_section_header_exact(self):
        # gist L297
        assert "# CONTEXT WINDOW MANAGEMENT" in _sp()

    def test_data_points_exact(self):
        # gist L300-302
        sp = _sp()
        assert "~10 recent data points per indicator (3-minute intervals)" in sp
        assert "~10 recent data points for 4-hour timeframe" in sp
        assert "Current account state and open positions" in sp

    def test_optimization_lines_exact(self):
        # gist L305-307
        sp = _sp()
        assert "Focus on most recent 3-5 data points for short-term signals" in sp
        assert "Use 4-hour data for trend context and support/resistance levels" in sp
        assert "Don't try to memorize all numbers, identify patterns instead" in sp


# ─── FINAL INSTRUCTIONS (gist L311-321) ─────────────────────────────────


class TestFinalInstructionsSource:
    def test_section_header_exact(self):
        # gist L311
        assert "# FINAL INSTRUCTIONS" in _sp()

    def test_5_instructions_exact(self):
        # gist L313-317
        sp = _sp()
        assert "Read the entire user prompt carefully before deciding" in sp
        assert "Verify your position sizing math (double-check calculations)" in sp
        assert "Ensure your JSON output is valid and complete" in sp
        assert "Provide honest confidence scores (don't overstate conviction)" in sp
        assert "Be consistent with your exit plans (don't abandon stops prematurely)" in sp

    def test_remember_line_exact(self):
        # gist L319
        sp = _sp()
        assert (
            "Remember: You are trading with real money in real markets. "
            "Every decision has consequences. Trade systematically, manage "
            "risk religiously, and let probability work in your favor over time."
            in sp
        )

    def test_now_analyze_line_exact(self):
        # gist L321
        assert (
            "Now, analyze the market data provided below and make your trading decision."
            in _sp()
        )


# ─── USER_PROMPT compliance (gist L332-486) ─────────────────────────────


def _up(**kw) -> str:
    defaults = dict(
        minutes_elapsed=42,
        per_symbol_blocks="(test)",
        total_return_pct=0.0,
        sharpe=None,
        cash=500.0,
        equity=500.0,
        open_positions_block="[]",
    )
    defaults.update(kw)
    return build_user_prompt(**defaults)


class TestUserPromptSource:
    def test_minutes_elapsed_line_exact(self):
        # gist L333: `It has been {minutes_elapsed} minutes since you started trading.`
        up = _up(minutes_elapsed=123)
        assert "It has been 123 minutes since you started trading." in up

    def test_intro_paragraph_exact(self):
        # gist L335
        up = _up()
        assert (
            "Below, we are providing you with a variety of state data, "
            "price data, and predictive signals so you can discover alpha. "
            "Below that is your current account information, value, "
            "performance, positions, etc." in up
        )

    def test_critical_data_ordering_warning_exact(self):
        # gist L337
        up = _up()
        assert (
            "⚠️ **CRITICAL: ALL OF THE PRICE OR SIGNAL DATA BELOW IS ORDERED: "
            "OLDEST → NEWEST**" in up
        )

    def test_critical_warning_appears_exactly_once(self):
        # source: 1 раз в начале USER_PROMPT, повторов в финале НЕТ
        # (см. правило ai-arena-sources.mdc «Что НЕЛЬЗЯ добавлять»)
        up = _up()
        assert up.count("OLDEST → NEWEST") == 1

    def test_timeframes_note_exact(self):
        # gist L339
        up = _up()
        assert (
            "**Timeframes note:** Unless stated otherwise in a section title, "
            "intraday series are provided at **3-minute intervals**. If a coin "
            "uses a different interval, it is explicitly stated in that "
            "coin's section." in up
        )

    def test_market_state_section_header_exact(self):
        # gist L343
        assert "## CURRENT MARKET STATE FOR ALL COINS" in _up()

    def test_account_info_section_header_exact(self):
        # gist L445
        assert "## HERE IS YOUR ACCOUNT INFORMATION & PERFORMANCE" in _up()

    def test_performance_metrics_subheader_exact(self):
        # gist L447
        assert "**Performance Metrics:**" in _up()

    def test_total_return_line_format_exact(self):
        # gist L448: `Current Total Return (percent): {return_pct}%`
        # Без `+` модификатора (нейтральный формат source).
        up = _up(total_return_pct=1.5)
        assert "Current Total Return (percent): 1.50%" in up
        # Регресс: `+` модификатор удалён
        assert "+1.50%" not in up

    def test_sharpe_line_format_exact(self):
        # gist L449: `Sharpe Ratio: {sharpe_ratio}`
        up = _up(sharpe=0.42)
        assert "Sharpe Ratio: 0.420" in up

    def test_account_status_subheader_exact(self):
        # gist L451
        assert "**Account Status:**" in _up()

    def test_available_cash_line_exact(self):
        # gist L452: `Available Cash: ${cash_available}`
        up = _up(cash=300.0)
        assert "Available Cash: $300.00" in up

    def test_current_account_value_line_exact(self):
        # gist L453: `**Current Account Value:** ${account_value}`
        up = _up(equity=550.0)
        assert "**Current Account Value:** $550.00" in up

    def test_current_positions_subheader_exact(self):
        # gist L455
        assert "**Current Live Positions & Performance:**" in _up()

    def test_final_decision_request_line_exact(self):
        # gist L485
        assert (
            "Based on the above data, provide your trading decision in the "
            "required JSON format." in _up()
        )

    def test_no_extra_oldest_newest_reminder_at_end(self):
        # source НЕ повторяет «OLDEST → NEWEST» в конце (нет «DATA ORDER
        # REMINDER»). Проверяем что мы не вернули эту «полезную подсказку».
        up = _up()
        assert "DATA ORDER REMINDER" not in up
        assert "REMINDER:" not in up


# ─── Per-symbol block compliance (gist L345-379) ────────────────────────


def _make_symbol_block(symbol: str = "BTCUSDT") -> SymbolBlock:
    """SymbolBlock без полных данных — для проверки структурных лейблов."""
    return SymbolBlock(
        symbol=symbol,
        ticker=None,
        intraday=None,
        longer_term=None,
        oi_latest=None,
        oi_avg=None,
    )


class TestPerSymbolBlockSource:
    def test_header_format_uses_arena_naming(self):
        # gist L345: `### ALL BTC DATA` (без USDT)
        out = format_symbol_block(_make_symbol_block("BTCUSDT"))
        assert "### ALL BTC DATA" in out


class TestPerSymbolBlockWithRealData:
    """Проверяем все labels per-symbol блока на реальных данных."""

    def _full_block(self, symbol: str = "BTCUSDT"):
        from ai_arena.analysis.indicators import (
            IntradaySnapshot,
            LongerTermSnapshot,
        )
        from ai_arena.trading.client import Ticker

        ticker = Ticker(
            symbol=symbol,
            last_price=100000.0,
            bid=99999.5,
            ask=100000.5,
            funding_rate=0.000123,  # 0.0123%
            volume_24h=10000.0,
            price_change_pct_24h=1.23,
        )
        intraday = IntradaySnapshot(
            prices=[100000.0] * 10,
            ema20=[100000.0] * 10,
            macd=[1.5] * 10,
            rsi7=[55.0] * 10,
            rsi14=[50.0] * 10,
        )
        longer = LongerTermSnapshot(
            ema20=99500.0,
            ema50=98000.0,
            atr3=120.0,
            atr14=150.0,
            volume_current=500.0,
            volume_avg=400.0,
            macd=[2.5] * 10,
            rsi14=[60.0] * 10,
        )
        return SymbolBlock(
            symbol=symbol,
            ticker=ticker,
            intraday=intraday,
            longer_term=longer,
            oi_latest=12345.0,
            oi_avg=12000.0,
        )

    def test_current_snapshot_labels_exact(self):
        # gist L347-351
        out = format_symbol_block(self._full_block())
        assert "**Current Snapshot:**" in out
        assert "- current_price = " in out
        assert "- current_ema20 = " in out
        assert "- current_macd = " in out
        assert "- current_rsi (7 period) = " in out

    def test_perpetual_metrics_labels_exact(self):
        # gist L353-355
        out = format_symbol_block(self._full_block())
        assert "**Perpetual Futures Metrics:**" in out
        assert "- Open Interest: Latest: " in out
        assert "| Average: " in out
        assert "- Funding Rate: " in out

    def test_funding_rate_no_plus_modifier(self):
        # gist L355: `Funding Rate: 0.0123%` — нейтральный формат БЕЗ `+`.
        # Раньше был `+0.0123%` — наша добавка, удалена.
        out = format_symbol_block(self._full_block())
        # Funding 0.000123 → 0.0123% (положительный, без `+`)
        assert "Funding Rate: 0.0123%" in out
        assert "Funding Rate: +0.0123%" not in out

    def test_intraday_series_labels_exact(self):
        # gist L357-367
        out = format_symbol_block(self._full_block())
        assert "**Intraday Series (3-minute intervals, oldest → latest):**" in out
        assert "Mid prices: " in out
        assert "EMA indicators (20-period): " in out
        assert "MACD indicators: " in out
        assert "RSI indicators (7-Period): " in out
        assert "RSI indicators (14-Period): " in out

    def test_longer_term_labels_exact(self):
        # gist L369-379
        out = format_symbol_block(self._full_block())
        assert "**Longer-term Context (4-hour timeframe):**" in out
        assert "20-Period EMA: " in out
        assert "vs. 50-Period EMA: " in out
        assert "3-Period ATR: " in out
        assert "vs. 14-Period ATR: " in out
        assert "Current Volume: " in out
        assert "vs. Average Volume: " in out
        assert "MACD indicators (4h): " in out
        assert "RSI indicators (14-Period, 4h): " in out

    def test_no_funding_band_annotation(self):
        # Регресс: правило ai-arena-sources.mdc запрещает `(band: …)`
        # после Funding Rate. Source даёт только сырое число.
        out = format_symbol_block(self._full_block())
        assert "(band:" not in out
        assert "neutral" not in out.lower() or "Neutral zone" in out
        # ↑ "Neutral zone" — это про RSI 40-60 в SYSTEM_PROMPT, не имеет
        # отношения к Funding. В per-symbol блоке слова `neutral` нет.

    def test_no_oi_period_annotation(self):
        # Регресс: запрещено `Average (20×5min)` после Open Interest.
        out = format_symbol_block(self._full_block())
        assert "(20×5min)" not in out
        assert "(20x5min)" not in out

    def test_no_volume_period_annotation(self):
        # Регресс: запрещено `Average Volume (20)` — source без `(20)`.
        out = format_symbol_block(self._full_block())
        assert "Average Volume (20)" not in out


# ─── Open positions Python repr (gist L457-478) ─────────────────────────


def _make_position(
    *,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.5,
    entry: float = 100000.0,
) -> ArenaPosition:
    return ArenaPosition(
        id=1,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        sl_price=99000.0,
        tp_price=102000.0,
        leverage=5,
        order_link_id="arena_test",
        opened_at="2026-05-15T09:00:00+00:00",
        closed_at=None,
        exit_price=None,
        realized_pnl_usd=None,
        close_reason=None,
        llm_justification="test",
        confidence=0.7,
        invalidation_condition="BTC < 98k",
        risk_usd=500.0,
    )


class TestOpenPositionsBlockSource:
    """gist L457-478 — Python list-of-dicts repr (НЕ JSON).

    ```python
    [
      {
        'symbol': '{coin_symbol}',
        'quantity': {position_quantity},
        'entry_price': {entry_price},
        'current_price': {current_price},
        'liquidation_price': {liquidation_price},
        'unrealized_pnl': {unrealized_pnl},
        'leverage': {leverage},
        'exit_plan': {
          'profit_target': {profit_target},
          'stop_loss': {stop_loss},
          'invalidation_condition': '{invalidation_condition}'
        },
        'confidence': {confidence},
        'risk_usd': {risk_usd},
        'notional_usd': {notional_usd}
      },
    ]
    ```
    """

    def _format(self, positions: list[ArenaPosition]) -> str:
        return format_open_positions_block(
            positions,
            current_prices={p.symbol: p.entry_price for p in positions},
            liquidation_prices={p.symbol: 0.0 for p in positions},
            notional_by_symbol={p.symbol: p.qty * p.entry_price for p in positions},
            unrealized_by_symbol={p.symbol: 0.0 for p in positions},
        )

    def test_empty_uses_python_empty_list(self):
        # gist L481-483
        assert format_open_positions_block(
            [], current_prices={}, liquidation_prices={},
            notional_by_symbol={}, unrealized_by_symbol={},
        ) == "[]"

    def test_uses_single_quotes_not_json_double_quotes(self):
        # source — Python literal с `'`, не JSON с `"`.
        out = self._format([_make_position()])
        assert "'symbol':" in out
        assert "'quantity':" in out
        assert '"symbol":' not in out
        assert '"quantity":' not in out

    def test_all_required_fields_present_exact(self):
        out = self._format([_make_position()])
        for field in [
            "'symbol'", "'quantity'", "'entry_price'", "'current_price'",
            "'liquidation_price'", "'unrealized_pnl'", "'leverage'",
            "'exit_plan'", "'confidence'", "'risk_usd'", "'notional_usd'",
        ]:
            assert field in out, f"missing field in open positions: {field}"

    def test_exit_plan_nested_fields_exact(self):
        out = self._format([_make_position()])
        # gist L468-471
        assert "'profit_target'" in out
        assert "'stop_loss'" in out
        assert "'invalidation_condition'" in out

    def test_no_side_field(self):
        # gist L457-478 — НЕТ поля `'side'`. Направление кодируется
        # знаком quantity (positive=long, negative=short).
        out = self._format([_make_position(side="Buy")])
        assert "'side':" not in out
        out2 = self._format([_make_position(side="Sell")])
        assert "'side':" not in out2

    def test_signed_quantity_long_positive(self):
        # Long → положительный `quantity` (signed). Hyperliquid конвенция.
        out = self._format([_make_position(side="Buy", qty=0.5)])
        assert "'quantity': 0.5" in out

    def test_signed_quantity_short_negative(self):
        # Short → отрицательный `quantity`.
        out = self._format([_make_position(side="Sell", qty=0.5)])
        assert "'quantity': -0.5" in out

    def test_symbol_rendered_in_arena_format(self):
        # gist L460: `'symbol': 'BTC'` (без USDT-суффикса).
        out = self._format([_make_position(symbol="ETHUSDT")])
        assert "'symbol': 'ETH'" in out
        assert "'symbol': 'ETHUSDT'" not in out

    def test_python_none_not_json_null(self):
        # null — это JSON. В Python literal — `None`.
        pos = ArenaPosition(
            id=1, symbol="BTCUSDT", side="Buy", qty=0.5, entry_price=100000.0,
            sl_price=None, tp_price=None, leverage=5,
            order_link_id="x", opened_at="2026-05-15T09:00:00+00:00",
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None, llm_justification="t",
            confidence=None, invalidation_condition=None, risk_usd=None,
        )
        out = self._format([pos])
        assert "None" in out
        assert "null" not in out
