"""Phase 0: review-цикл как GUARDIAN, не strategist.

Контекст (BUILDLOG_AI_FX_TRADER.md 2026-05-29):
review-цикл закрывал позиции по 1H техническому шуму (setup invalidation
/ adverse technical), пока full-цикл по Phase 1 thesis discipline держал
их по интактному macro-тезису. Конфликт «review overrules full» сжёг
BZ=F long (id=30). Аудит истории бота: 22/26 LLM-закрытий были по 1H
технике в одиночку (Mark Douglas «Trading in the Zone»: реакция на шум
без edge).

Фикс (Фаза 0): review закрывает ТОЛЬКО по locked-profit ≥1.5R. Любые
1H setup invalidation / adverse technical → HOLD (full-цикл судит с
macro; broker SL — пол для убыточных). Тот же research, что Phase 1.

Эти тесты фиксируют, что:
- SYSTEM_PROMPT_REVIEW описывает guardian-роль и единственный close-повод;
- удалены самостоятельные triggers «setup invalidation» / «adverse
  technical» как close-поводы;
- примеры показывают HOLD на 1H weakness (а не close);
- build_user_prompt_review TASK RESTATEMENT отражает guardian-правило;
- build_system_prompt_review форматируется без незаполненных токенов.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AI_FX_TRADER_DEEPSEEK_API_KEY", "test-key")

from fx_ai_trader.config.settings import AiFxTraderSettings  # noqa: E402
from fx_ai_trader.llm.prompts import (  # noqa: E402
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

    def test_setup_invalidation_is_not_a_close_trigger_anymore(self):
        """Старый Trigger 1 не должен фигурировать как close-повод."""
        # Не осталось формулировки старого триггера-заголовка.
        assert "SETUP INVALIDATION —" not in SYSTEM_PROMPT_REVIEW
        assert "ADVERSE TECHNICAL EVIDENCE —" not in SYSTEM_PROMPT_REVIEW
        # И явно сказано НЕ закрывать по этим сигналам.
        assert "this is NOT your call" in SYSTEM_PROMPT_REVIEW

    def test_losing_position_left_to_broker_sl(self):
        assert "broker stop-loss is the floor" in SYSTEM_PROMPT_REVIEW

    def test_audit_research_basis_cited(self):
        assert "22 / 26" in SYSTEM_PROMPT_REVIEW
        assert "Mark Douglas" in SYSTEM_PROMPT_REVIEW

    def test_decision_rule_threshold_present(self):
        assert "1.5R" in SYSTEM_PROMPT_REVIEW

    def test_thesis_discipline_markers_retained(self):
        # Совместимость со старыми тестами persistent_thesis.
        assert "THESIS DISCIPLINE" in SYSTEM_PROMPT_REVIEW
        assert "thesis_status" in SYSTEM_PROMPT_REVIEW
        assert "thesis_invalidator" in SYSTEM_PROMPT_REVIEW


class TestReviewExamples:
    def test_examples_show_hold_on_1h_weakness(self):
        start = SYSTEM_PROMPT_REVIEW.find("CONCRETE EXAMPLES")
        end = SYSTEM_PROMPT_REVIEW.find("FINAL RULES", start)
        assert start != -1 and end != -1
        examples = SYSTEM_PROMPT_REVIEW[start:end]
        # Есть HOLD-пример с 1H weakness.
        assert "hold" in examples
        assert "1H noise" in examples or "1H weakness" in examples.lower() \
            or "1H showing strength against" in examples
        # Единственный close-пример — locked-profit.
        assert "locked-profit" in examples

    def test_examples_do_not_close_on_partial_invalidation(self):
        """Старый partial-close-пример (BRENT MACD flip) удалён."""
        assert "partial setup invalidation" not in SYSTEM_PROMPT_REVIEW


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
        s = AiFxTraderSettings()
        p = build_system_prompt_review(s)
        assert "%(" not in p, "unsubstituted % format token left in prompt"
        assert "GUARDIAN" in p

    def test_minutes_rendered_from_settings(self):
        s = AiFxTraderSettings()
        p = build_system_prompt_review(s)
        full_min = max(1, s.poll_interval_sec // 60)
        review_min = max(1, s.review_interval_sec // 60)
        assert f"every {full_min} minutes" in p
        assert f"every {review_min} minutes" in p
