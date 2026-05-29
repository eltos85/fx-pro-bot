"""v0.34 Phase 0: review-цикл AI-Trader как GUARDIAN, не strategist.

Порт fx Phase 0 на байбит (2026-05-29). Review-цикл закрывал позиции по
1H техническому шуму (SETUP INVALIDATION / ADVERSE EVIDENCE / PEAK-
DRAWDOWN), пока full-цикл по thesis discipline держал их по интактному
macro-тезису. Sibling FX-bot аудит: 22/26 LLM-закрытий были по 1H технике
в одиночку (Mark Douglas «Trading in the Zone»: реакция на шум без edge).

Фикс (Phase 0): review закрывает ТОЛЬКО по locked-profit ≥1.5R. Любые 1H
setup invalidation / adverse technical / peak-drawdown / funding-timing →
HOLD (full-цикл судит с macro; exchange SL — пол для убыточных).

Эти тесты фиксируют, что:
- SYSTEM_PROMPT_REVIEW описывает guardian-роль и единственный close-повод;
- удалены самостоятельные triggers (setup invalidation / adverse / peak-
  drawdown / funding) как close-поводы;
- примеры показывают HOLD на 1H weakness (а не close);
- build_user_prompt_review TASK RESTATEMENT отражает guardian-правило;
- build_system_prompt_review форматируется без незаполненных токенов;
- bybit-специфика (fee/funding awareness display) сохранена.
"""

from __future__ import annotations

from ai_trader.config.settings import AiTraderSettings
from ai_trader.llm.prompts import (
    SYSTEM_PROMPT_REVIEW,
    build_system_prompt_review,
    build_user_prompt_review,
)


class TestReviewGuardianRole:
    def test_prompt_declares_guardian_not_strategist(self):
        assert "GUARDIAN" in SYSTEM_PROMPT_REVIEW
        assert "NOT STRATEGIST" in SYSTEM_PROMPT_REVIEW.upper()

    def test_locked_profit_is_only_authorised_close(self):
        assert "LOCKED-PROFIT GUARD" in SYSTEM_PROMPT_REVIEW
        assert "ONLY close you are authorised" in SYSTEM_PROMPT_REVIEW

    def test_old_close_triggers_removed(self):
        """Старые self-standing close-поводы (1-4) больше не triggers."""
        # Не осталось формулировок старых триггеров-заголовков.
        assert "SETUP INVALIDATION —" not in SYSTEM_PROMPT_REVIEW
        assert "ADVERSE NEW EVIDENCE —" not in SYSTEM_PROMPT_REVIEW
        # Старые числовые пороги peak-drawdown удалены.
        assert "0.8R" not in SYSTEM_PROMPT_REVIEW
        assert "0.45R" not in SYSTEM_PROMPT_REVIEW
        # И явно сказано НЕ закрывать по этим сигналам.
        assert "NOT your call" in SYSTEM_PROMPT_REVIEW

    def test_losing_position_left_to_exchange_sl(self):
        assert "exchange stop-loss is the floor" in SYSTEM_PROMPT_REVIEW

    def test_funding_deferred_to_full(self):
        assert "FUNDING timing" in SYSTEM_PROMPT_REVIEW
        assert "defer to the full" in SYSTEM_PROMPT_REVIEW

    def test_audit_research_basis_cited(self):
        assert "22 / 26" in SYSTEM_PROMPT_REVIEW
        assert "Mark Douglas" in SYSTEM_PROMPT_REVIEW

    def test_decision_rule_threshold_present(self):
        assert "1.5R" in SYSTEM_PROMPT_REVIEW

    def test_thesis_discipline_markers_retained(self):
        assert "THESIS DISCIPLINE" in SYSTEM_PROMPT_REVIEW
        assert "thesis_status" in SYSTEM_PROMPT_REVIEW
        assert "thesis_invalidator" in SYSTEM_PROMPT_REVIEW

    def test_bybit_fee_awareness_retained(self):
        """Guardian сохраняет fee-aware close (close_net, taker placeholders)."""
        assert "close_net" in SYSTEM_PROMPT_REVIEW
        assert "__TAKER_FEE_PCT__" in SYSTEM_PROMPT_REVIEW


class TestReviewExamples:
    def test_examples_show_hold_on_1h_weakness(self):
        start = SYSTEM_PROMPT_REVIEW.find("CONCRETE EXAMPLES")
        end = SYSTEM_PROMPT_REVIEW.find("FINAL RULES", start)
        assert start != -1 and end != -1
        examples = SYSTEM_PROMPT_REVIEW[start:end]
        assert "hold" in examples
        assert "1H noise" in examples or "1H weakness" in examples.lower() \
            or "1H strength against" in examples
        # Единственный close-пример — locked-profit.
        assert "locked-profit" in examples

    def test_single_close_example_is_locked_profit(self):
        start = SYSTEM_PROMPT_REVIEW.find("CONCRETE EXAMPLES")
        examples = SYSTEM_PROMPT_REVIEW[start:]
        # Ровно один CLOSE-пример (action": "close" в блоке примеров).
        assert examples.count('"action": "close"') == 1


class TestReviewUserPromptTask:
    def test_task_restatement_states_guardian(self):
        out = build_user_prompt_review("CTX")
        assert "GUARDIAN" in out
        assert "1.5R" in out

    def test_task_restatement_hold_default_for_non_locked_profit(self):
        out = build_user_prompt_review("CTX")
        assert "HOLD" in out
        assert "full cycle" in out.lower()


class TestReviewPromptFormatting:
    def test_build_system_prompt_review_substitutes_tokens(self):
        p = build_system_prompt_review(AiTraderSettings())
        assert "%(" not in p, "unsubstituted % format token left in prompt"
        assert "%%" not in p, "literal %% leaked into final prompt"
        assert "__TAKER_FEE" not in p, "unrendered fee placeholder left"
        assert "GUARDIAN" in p

    def test_minutes_rendered_from_settings(self):
        s = AiTraderSettings()
        p = build_system_prompt_review(s)
        full_min = max(1, s.poll_interval_sec // 60)
        review_min = max(1, s.review_interval_sec // 60)
        assert f"every {full_min} minutes" in p or f"{full_min} minutes" in p
        assert f"{review_min} minutes" in p
