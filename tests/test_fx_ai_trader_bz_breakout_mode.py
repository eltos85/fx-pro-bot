"""Тесты flag-gated BZ MOMENTUM MODE + NG MODE V2 инъекции в USER_PROMPT.

BZ momentum mode — paper-эксперимент (2026-06-09): break->retest->hold
continuation вход ТОЛЬКО по BZ=F в режиме повышенной волатильности.
Проверяем:
- по умолчанию блок ОТСУТСТВУЕТ (backward compat, OFF by default);
- при enabled блок присутствует, BZ-only, с порогами из settings;
- блок не появляется для XAUUSD/NG (текст явно ограничивает scope);
- defaults в AiFxTraderSettings = OFF (эксперимент стартует только явным env).
"""
from __future__ import annotations

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.llm.prompts import build_user_prompt


def test_bz_mode_absent_by_default() -> None:
    out = build_user_prompt("MARKET_CTX")
    assert "BZ MOMENTUM MODE" not in out


def test_bz_mode_present_when_enabled() -> None:
    out = build_user_prompt(
        "MARKET_CTX",
        bz_breakout_mode_enabled=True,
        bz_breakout_min_atr_pct=0.6,
        bz_breakout_min_sl_atr=2.0,
        bz_breakout_max_uncertainty=0.55,
    )
    assert "BZ MOMENTUM MODE (targeted, BRENT/BZ=F only)" in out
    # пороги прокидываются из аргументов
    assert ">= 0.60% of price" in out
    assert ">= 2.0x the 1H ATR" in out
    assert "aggregate_uncertainty <= 0.55" in out
    # scope: явно НЕ трогает другие инструменты
    assert "DO NOT modify XAUUSD/NG=F" in out
    # break->retest->hold, не first-spike
    assert "break -> retest -> hold" in out
    assert "NEVER first-spike chase" in out


def test_bz_mode_thresholds_are_parametrized() -> None:
    out = build_user_prompt(
        "MARKET_CTX",
        bz_breakout_mode_enabled=True,
        bz_breakout_min_atr_pct=0.8,
        bz_breakout_min_sl_atr=2.5,
        bz_breakout_max_uncertainty=0.50,
    )
    assert ">= 0.80% of price" in out
    assert ">= 2.5x the 1H ATR" in out
    assert "aggregate_uncertainty <= 0.50" in out


def test_settings_defaults_are_off() -> None:
    s = AiFxTraderSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.bz_breakout_mode_enabled is False
    assert s.bz_breakout_min_atr_pct == 0.6
    assert s.bz_breakout_min_sl_atr == 2.0
    assert s.bz_breakout_max_uncertainty == 0.55
    # NG mode тоже OFF by default — сосуществуют независимо
    assert s.ng_mode_v2_enabled is False


def test_ng_and_bz_modes_coexist() -> None:
    out = build_user_prompt(
        "MARKET_CTX",
        ng_mode_v2_enabled=True,
        bz_breakout_mode_enabled=True,
    )
    assert "NG MODE V2 (targeted, NAT.GAS only)" in out
    assert "BZ MOMENTUM MODE (targeted, BRENT/BZ=F only)" in out
