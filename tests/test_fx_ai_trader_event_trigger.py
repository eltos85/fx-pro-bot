"""Tests для Phase 3.1 (2026-05-29): EVENT TRIGGER блок в промпте.

Идея пользователя: аналитик должен получать сигнал датчика (что и где
сработало) и сопоставлять его со СВОИМИ macro/structure данными, а не
просыпаться «вслепую». Покрывает:
- format_event_trigger: рендер по категориям (breakout / adverse /
  locked-profit) + нейтральная рамка; пусто/None → "".
- build_user_prompt / build_user_prompt_review: блок присутствует при
  event_trigger, отсутствует при scheduled (default "").
- SYSTEM_PROMPT / SYSTEM_PROMPT_REVIEW: секция EVENT TRIGGER + анти-FOMO
  формулировка (compliance: strategy-guard.mdc — рамка не толкает в вход).

Compliance: no-data-fitting.mdc (рамка опирается на MFP rule 3 — вход на
откате, не на пробое), strategy-guard.mdc (правка промпта согласована).
"""
from __future__ import annotations

from fx_ai_trader.llm.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    build_user_prompt_review,
    format_event_trigger,
)


# ─── format_event_trigger ──────────────────────────────────────────────


class TestFormatEventTrigger:
    def test_empty_and_none_return_blank(self):
        assert format_event_trigger(None) == ""
        assert format_event_trigger([]) == ""

    def test_breakout_block_has_header_signal_and_neutral_frame(self):
        out = format_event_trigger(["XAUUSD up-break @4528.03 > Donchian hi"])
        assert "EVENT TRIGGER" in out
        assert "XAUUSD up-break @4528.03 > Donchian hi" in out
        # анти-FOMO рамка
        assert "ATTENTION CUE" in out
        assert "NOT a recommendation" in out
        # breakout-specific guidance (MFP rule 3 pullback)
        assert "pullback" in out.lower()
        assert "Breakout alone is NOT an entry" in out

    def test_adverse_block_asks_thesis_recheck(self):
        out = format_event_trigger(["#17 -1.20R adverse"])
        assert "#17 -1.20R adverse" in out
        assert "re-judge the macro thesis" in out
        assert "macro+news" in out

    def test_locked_profit_block_defers_to_own_R(self):
        out = format_event_trigger(["#9 +1.62R locked-profit"])
        assert "#9 +1.62R locked-profit" in out
        assert "verify R from" in out

    def test_multiple_triggers_all_listed(self):
        out = format_event_trigger([
            "NG=F up-break @3.33 > Donchian hi",
            "#5 -1.10R adverse",
        ])
        assert "NG=F up-break @3.33 > Donchian hi" in out
        assert "#5 -1.10R adverse" in out
        # обе guidance-ветки присутствуют
        assert "Breakout alone is NOT an entry" in out
        assert "re-judge the macro thesis" in out


# ─── build_user_prompt (full) ──────────────────────────────────────────


class TestBuildUserPromptEventTrigger:
    def test_block_present_when_event_trigger_given(self):
        block = format_event_trigger(["XAUUSD up-break @4528 > Donchian hi"])
        out = build_user_prompt("MARKET CTX HERE", event_trigger=block)
        assert "EVENT TRIGGER" in out
        assert "XAUUSD up-break @4528 > Donchian hi" in out
        # event-блок идёт ПЕРЕД market context (framing-first)
        assert out.index("EVENT TRIGGER") < out.index("MARKET CTX HERE")

    def test_no_block_on_scheduled_default(self):
        out = build_user_prompt("MARKET CTX HERE")
        assert "EVENT TRIGGER" not in out

    def test_empty_event_trigger_no_block(self):
        out = build_user_prompt("MARKET CTX HERE", event_trigger="")
        assert "EVENT TRIGGER" not in out


# ─── build_user_prompt_review ──────────────────────────────────────────


class TestBuildUserPromptReviewEventTrigger:
    def test_block_present_when_event_trigger_given(self):
        block = format_event_trigger(["#9 +1.62R locked-profit"])
        out = build_user_prompt_review("REVIEW CTX", event_trigger=block)
        assert "EVENT TRIGGER" in out
        assert "#9 +1.62R locked-profit" in out
        assert out.index("EVENT TRIGGER") < out.index("REVIEW CTX")

    def test_no_block_on_scheduled_default(self):
        out = build_user_prompt_review("REVIEW CTX")
        assert "EVENT TRIGGER" not in out


# ─── SYSTEM_PROMPT content asserts ─────────────────────────────────────


class TestSystemPromptEventTriggerSection:
    def test_full_system_prompt_has_event_trigger_section(self):
        assert "EVENT TRIGGER" in SYSTEM_PROMPT
        # анти-FOMO инвариант: событие НЕ повод открывать
        assert "NEVER, by itself, a reason to open" in SYSTEM_PROMPT

    def test_review_system_prompt_has_event_trigger_note(self):
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trader.llm.prompts import build_system_prompt_review
        sp = build_system_prompt_review(AiFxTraderSettings())
        assert "EVENT TRIGGER" in sp
        # timing-only, своя R главнее датчика
        assert "your computation wins" in sp
