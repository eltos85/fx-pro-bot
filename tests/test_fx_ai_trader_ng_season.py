"""Tests для сезонного гарда NG=F weather sign (2026-06-03).

Контекст (BUILDLOG_AI_FX_TRADER.md): аудит decisions показал, что LLM
периодически инвертирует знак погодной аномалии для газа — трактует
"above-normal/warm temps" как bearish demand ЛЕТОМ (зимняя HDD-логика),
хотя в сезон охлаждения (CDD, May–Sep) жара = больше кондиционеров =
BULLISH. Это самопротиворечие (id=34/37/39 на 01–03.06) двигало реальные
лоссы.

Усиление:
- SYSTEM_PROMPT: явный SEASONAL SIGN RULE + mistake в NG MISTAKES TO AVOID.
- context: детерминированный `_ng_weather_season(month)` + "NG WEATHER
  SEASON" строка в шапке (LLM не должен сам выводить знак).

Compliance: no-data-fitting.mdc — это bug-fix логической инверсии
(симптом → причина → фикс), а не подгонка thresholds под результат;
строго следует канону SYSTEM_PROMPT (HDD Oct–Mar / CDD May–Sep).
"""
from __future__ import annotations

import pytest

from fx_ai_trader.trading.context import _ng_weather_season


class TestNgWeatherSeason:
    @pytest.mark.parametrize("month", [5, 6, 7, 8, 9])
    def test_cooling_season_warm_is_bullish(self, month: int):
        out = _ng_weather_season(month)
        assert "CDD" in out
        assert "BULLISH" in out
        # знак: жара = bullish, прохлада = bearish
        assert "warm temps = BULLISH" in out
        assert "cool anomaly = bearish" in out

    @pytest.mark.parametrize("month", [11, 12, 1, 2, 3])
    def test_heating_season_cold_is_bullish(self, month: int):
        out = _ng_weather_season(month)
        assert "HDD" in out
        assert "cold temps = BULLISH" in out
        assert "warm/mild anomaly = bearish" in out

    @pytest.mark.parametrize("month", [4, 10])
    def test_shoulder_months_low_conviction(self, month: int):
        out = _ng_weather_season(month)
        assert "shoulder" in out
        assert "low-conviction" in out

    def test_june_is_cooling_not_heating(self):
        """Регресс на сам баг: июнь НЕ должен давать heating-логику."""
        out = _ng_weather_season(6)
        assert "HDD" not in out
        assert "heating" not in out


class TestSeasonalSignRuleInPrompt:
    def test_system_prompt_has_seasonal_sign_rule(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT

        assert "SEASONAL SIGN RULE" in SYSTEM_PROMPT
        # явный анти-инверсия гард для лета
        assert "above-normal temps RAISE cooling demand" in SYSTEM_PROMPT

    def test_system_prompt_lists_inversion_as_top_mistake(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT

        assert "INVERTING THE SEASONAL WEATHER SIGN" in SYSTEM_PROMPT
        assert "self-contradiction" in SYSTEM_PROMPT


class TestContextHeaderSeason:
    def test_context_header_includes_ng_weather_season(self, monkeypatch):
        """Шапка контекста должна нести детерминированный season-ярлык."""
        import fx_ai_trader.trading.context as ctx_mod
        from fx_ai_trader.trading.context import (
            MarketContext,
            format_context_for_prompt,
        )

        class _FixedDt:
            @staticmethod
            def now(tz=None):
                from datetime import datetime as _dt
                return _dt(2026, 6, 3, 10, 0, tzinfo=tz)

        monkeypatch.setattr(ctx_mod, "datetime", _FixedDt)
        ctx = MarketContext(
            snapshots=[], open_positions=[], virtual_capital_usd=1500.0
        )
        out = format_context_for_prompt(ctx)
        assert "NG WEATHER SEASON:" in out
        assert "CDD cooling season" in out
        assert "AS OF: 2026-06-03" in out
        assert "month=June" in out
