"""Tests для анти-flip правил приоритета BZ=F и NG=F (2026-06-24).

Контекст (BUILDLOG_AI_FX_TRADER.md): золото (XAUUSD) консистентно за счёт
чёткой иерархии драйверов (real-yields → DXY) и не флипает знак. У нефти и
газа в каноне были «дыры»: BZ=F — DXY-корреляция «флипает по режиму» без
правила приоритета (LLM выбирал знак произвольно); NG=F — погодные инверсии
(закрыто SEASONAL SIGN RULE 03.06), но без явного приоритета storage-vs-weather.

Эта правка формализует УЖЕ процитированный канон (KenMacro oil four-channel;
EIA storage anchor) в детерминированные правила знака — зеркалит сезонный
фикс NG. Никаких новых порогов (no-data-fitting.mdc), строго symbol-scoped:
золото (XAUUSD) обе правки НЕ затрагивают (strategy-guard.mdc).
"""
from __future__ import annotations

from fx_ai_trader.llm.prompts import SYSTEM_PROMPT


class TestBzChannelPriority:
    def test_system_prompt_has_channel_priority_rule(self):
        assert "CHANNEL-PRIORITY / DXY-SIGN RULE (BRENT/BZ=F ONLY)" in SYSTEM_PROMPT

    def test_supply_led_can_be_positive_dxy(self):
        # ключевой анти-flip: supply-led НЕ inverse-DXY by default
        assert "SUPPLY-led" in SYSTEM_PROMPT
        assert "oil can rise WITH DXY" in SYSTEM_PROMPT
        assert "Do NOT trade it as inverse-DXY" in SYSTEM_PROMPT

    def test_demand_led_inverse_dxy(self):
        assert "DEMAND-led" in SYSTEM_PROMPT
        assert "INVERSE to DXY" in SYSTEM_PROMPT

    def test_no_self_flip_within_position(self):
        assert "do NOT flip your own DXY-sign read of the SAME move" in SYSTEM_PROMPT

    def test_scoped_to_brent_only(self):
        # правило не должно менять золото/газ
        assert (
            "applies to BRENT/BZ=F ONLY; XAUUSD and NG=F\nbehavior is UNCHANGED"
            in SYSTEM_PROMPT
        )


class TestNgStorageAnchorPriority:
    def test_system_prompt_has_driver_priority_rule(self):
        assert "DRIVER-PRIORITY RULE (NG=F ONLY)" in SYSTEM_PROMPT
        assert "STORAGE IS THE ANCHOR, WEATHER THE" in SYSTEM_PROMPT

    def test_storage_sets_structural_bias(self):
        assert "Storage vs 5y-average sets the STRUCTURAL directional BIAS" in SYSTEM_PROMPT

    def test_weather_is_catalyst_not_bias_flip(self):
        assert "Weather is a CATALYST" in SYSTEM_PROMPT
        assert "does NOT by itself flip the structural bias" in SYSTEM_PROMPT

    def test_conflict_size_down_against_anchor(self):
        assert "size DOWN when trading AGAINST the\n     storage anchor" in SYSTEM_PROMPT

    def test_scoped_to_ng_only(self):
        assert (
            "applies to NG=F ONLY; XAUUSD and BRENT behavior is UNCHANGED"
            in SYSTEM_PROMPT
        )


class TestGoldUnchangedInvariant:
    """Золото не должно быть затронуто: его иерархия на месте, и обе новые
    правки явно декларируют XAUUSD UNCHANGED."""

    def test_gold_five_driver_hierarchy_still_present(self):
        assert "GOLD (XAUUSD) — FIVE-DRIVER HIERARCHY" in SYSTEM_PROMPT

    def test_both_rules_declare_xauusd_unchanged(self):
        # BZ-правило
        assert "XAUUSD and NG=F\nbehavior is UNCHANGED" in SYSTEM_PROMPT
        # NG-правило
        assert "XAUUSD and BRENT behavior is UNCHANGED" in SYSTEM_PROMPT

    def test_no_new_rule_block_targets_gold_directly(self):
        # ни одно из новых правил-заголовков не привязано к XAUUSD
        assert "DXY-SIGN RULE (XAUUSD" not in SYSTEM_PROMPT
        assert "DRIVER-PRIORITY RULE (XAUUSD" not in SYSTEM_PROMPT
