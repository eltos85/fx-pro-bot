"""Тесты Phase 1 persistent-thesis + v4 prompt-tune (2026-05-26).

Покрытие:
- ``CloseAction`` schema: новые опциональные поля thesis_status /
  thesis_invalidator парсятся корректно, clamp длинного invalidator
  работает.
- ``parse_action``: missing/intact/broken/partial без reject — soft
  validation через ``_log_thesis_audit`` (WARN/INFO в logs, но
  ParsedAction возвращается).
- ``AiFxTraderStore``: idempotent миграция (применима к pre-migration
  БД), новые поля сохраняются в ``log_decision`` и поднимаются обратно.
- ``SYSTEM_PROMPT`` / ``SYSTEM_PROMPT_REVIEW``: содержат ключевые маркеры
  THESIS DISCIPLINE / thesis_status / thesis_invalidator.
- ``main._extract_thesis``: корректно извлекает поля из parsed JSON.
- v4 prompt-tune (A+B+C+D): task-sandwich маркеры в начале/конце
  prompts, concrete JSON examples в SYSTEM_PROMPT, prime expected
  output в build_user_prompt[_review], ``DeepSeekClient`` принимает
  ``effort`` и передаёт через extra_body.

См. ``BUILDLOG_AI_FX_TRADER.md`` записи 2026-05-26 — research artifact,
acceptance criteria.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_trader.llm.client import DeepSeekClient
from fx_ai_trader.app.main import _extract_thesis
from fx_ai_trader.llm.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_REVIEW,
    build_user_prompt,
    build_user_prompt_review,
)
from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.executor import (
    CloseAction,
    ParsedAction,
    parse_action,
)


ALLOWED = ("XAUUSD", "BZ=F", "NG=F")


# ─── CloseAction Pydantic schema ─────────────────────────────────────────


class TestCloseActionSchema:
    def test_missing_thesis_fields_is_valid(self):
        """Backward-compat: старый формат без thesis_* должен парситься."""
        m = CloseAction.model_validate(
            {"action": "close", "position_id": 1, "reason": "MACD flip"}
        )
        assert m.thesis_status is None
        assert m.thesis_invalidator is None

    @pytest.mark.parametrize("status", ["broken", "intact", "partial"])
    def test_all_thesis_status_literals_accepted(self, status: str):
        m = CloseAction.model_validate(
            {
                "action": "close",
                "position_id": 1,
                "reason": "test",
                "thesis_status": status,
                "thesis_invalidator": "EIA surprise -8 Bcf",
            }
        )
        assert m.thesis_status == status

    def test_invalid_thesis_status_rejected(self):
        """Несуществующий literal должен попасть в ValidationError."""
        with pytest.raises(Exception):  # noqa: PT011 — Pydantic ValidationError
            CloseAction.model_validate(
                {
                    "action": "close",
                    "position_id": 1,
                    "thesis_status": "weakened",  # not in literal set
                }
            )

    def test_long_thesis_invalidator_is_clamped_not_rejected(self):
        """200-char clamp через BeforeValidator (тот же паттерн что и reason)."""
        long_inv = "X" * 500
        m = CloseAction.model_validate(
            {
                "action": "close",
                "position_id": 1,
                "thesis_status": "broken",
                "thesis_invalidator": long_inv,
            }
        )
        assert m.thesis_invalidator is not None
        assert len(m.thesis_invalidator) == 200


# ─── parse_action — soft validation ──────────────────────────────────────


def _build_close_payload(**overrides) -> str:
    payload = {"action": "close", "position_id": 7, "reason": "test"}
    payload.update(overrides)
    return json.dumps(payload)


class TestParseActionThesisSoftValidation:
    def test_missing_thesis_logs_warning_but_accepts(
        self, caplog: pytest.LogCaptureFixture
    ):
        text = _build_close_payload()
        with caplog.at_level(logging.WARNING, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED)
        assert isinstance(parsed, ParsedAction)
        assert parsed.action_type == "close"
        assert any("missing_thesis_status" in rec.message for rec in caplog.records)

    def test_broken_without_invalidator_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        text = _build_close_payload(thesis_status="broken")
        with caplog.at_level(logging.WARNING, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED)
        assert isinstance(parsed, ParsedAction)
        assert any(
            "broken_thesis_without_invalidator" in rec.message for rec in caplog.records
        )

    def test_partial_without_invalidator_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        text = _build_close_payload(thesis_status="partial")
        with caplog.at_level(logging.WARNING, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED)
        assert isinstance(parsed, ParsedAction)
        assert any(
            "broken_thesis_without_invalidator" in rec.message for rec in caplog.records
        )

    def test_intact_close_logs_info_audit(self, caplog: pytest.LogCaptureFixture):
        text = _build_close_payload(
            thesis_status="intact",
            thesis_invalidator="locked-profit 1.7R",
        )
        with caplog.at_level(logging.INFO, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED)
        assert isinstance(parsed, ParsedAction)
        assert any(
            "closed_intact_thesis" in rec.message for rec in caplog.records
        )

    def test_broken_with_invalidator_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        text = _build_close_payload(
            thesis_status="broken",
            thesis_invalidator="EIA surprise -8 Bcf vs +35 consensus",
        )
        with caplog.at_level(logging.WARNING, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED)
        assert isinstance(parsed, ParsedAction)
        assert not any(
            "broken_thesis_without_invalidator" in rec.message for rec in caplog.records
        )

    def test_review_mode_close_still_audits_thesis(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Review-cycle close тоже проходит через _log_thesis_audit."""
        text = _build_close_payload(thesis_status="intact", thesis_invalidator="x")
        with caplog.at_level(logging.INFO, logger="fx_ai_trader.trading.executor"):
            parsed = parse_action(text, ALLOWED, review_mode=True)
        assert isinstance(parsed, ParsedAction)
        assert any("closed_intact_thesis" in rec.message for rec in caplog.records)


# ─── DB миграция + log_decision persistence ──────────────────────────────


@pytest.fixture
def fresh_store(tmp_path: Path) -> AiFxTraderStore:
    return AiFxTraderStore(tmp_path / "fx_ai_trader.sqlite")


class TestDbMigrationAndPersistence:
    def test_fresh_store_has_thesis_columns(self, fresh_store: AiFxTraderStore):
        with sqlite3.connect(fresh_store.db_path) as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "thesis_status" in cols
        assert "thesis_invalidator" in cols

    def test_migration_is_idempotent(self, fresh_store: AiFxTraderStore):
        """Повторное открытие store не должно падать (повторный ALTER TABLE)."""
        # Создаём ещё один store на том же файле — миграция должна быть no-op
        store2 = AiFxTraderStore(fresh_store.db_path)
        with sqlite3.connect(store2.db_path) as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "thesis_status" in cols
        assert "thesis_invalidator" in cols

    def test_migration_adds_columns_to_pre_existing_db(self, tmp_path: Path):
        """Симулируем pre-migration БД (без thesis_*) и проверяем что
        миграция добавляет колонки без потери данных."""
        db_path = tmp_path / "pre_migration.sqlite"
        # Создаём pre-migration схему (без thesis_*)
        with sqlite3.connect(db_path) as c:
            c.executescript(
                """
                CREATE TABLE decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle INTEGER NOT NULL,
                    cycle_type TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    prompt_system TEXT NOT NULL,
                    prompt_user TEXT NOT NULL,
                    response_raw TEXT,
                    parsed_action TEXT,
                    sentiment_json TEXT,
                    executed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    tokens_input INTEGER,
                    tokens_output INTEGER,
                    cost_usd REAL
                );
                """
            )
            c.execute(
                """
                INSERT INTO decisions (cycle, cycle_type, ts, prompt_system,
                    prompt_user, executed)
                VALUES (1, 'full', '2026-05-20T00:00:00+00:00', 'sys', 'usr', 0)
                """
            )

        # Открываем store — должна сработать миграция
        store = AiFxTraderStore(db_path)
        with sqlite3.connect(store.db_path) as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(decisions)").fetchall()}
            row = c.execute("SELECT cycle, thesis_status FROM decisions WHERE id=1").fetchone()
        assert "thesis_status" in cols
        assert "thesis_invalidator" in cols
        # Старая запись жива, новые поля NULL
        assert row[0] == 1
        assert row[1] is None

    def test_log_decision_persists_thesis_fields(self, fresh_store: AiFxTraderStore):
        rec_id = fresh_store.log_decision(
            cycle=1,
            cycle_type="full",
            prompt_system="sys",
            prompt_user="usr",
            response_raw="{}",
            parsed_action={"action": "close"},
            sentiment=None,
            executed=True,
            error=None,
            thesis_status="broken",
            thesis_invalidator="EIA surprise -8 Bcf",
        )
        assert rec_id > 0
        with sqlite3.connect(fresh_store.db_path) as c:
            row = c.execute(
                "SELECT thesis_status, thesis_invalidator FROM decisions WHERE id=?",
                (rec_id,),
            ).fetchone()
        assert row[0] == "broken"
        assert row[1] == "EIA surprise -8 Bcf"

    def test_log_decision_null_thesis_for_open(self, fresh_store: AiFxTraderStore):
        """Open / hold / parse-error решения должны писаться с NULL."""
        rec_id = fresh_store.log_decision(
            cycle=1,
            cycle_type="full",
            prompt_system="sys",
            prompt_user="usr",
            response_raw="{}",
            parsed_action={"action": "open"},
            sentiment=None,
            executed=True,
            error=None,
        )
        with sqlite3.connect(fresh_store.db_path) as c:
            row = c.execute(
                "SELECT thesis_status, thesis_invalidator FROM decisions WHERE id=?",
                (rec_id,),
            ).fetchone()
        assert row[0] is None
        assert row[1] is None


# ─── Prompt content ──────────────────────────────────────────────────────


class TestPromptContainsThesisDiscipline:
    def test_system_prompt_has_thesis_discipline_block(self):
        assert "THESIS DISCIPLINE" in SYSTEM_PROMPT
        assert "thesis_status" in SYSTEM_PROMPT
        assert "thesis_invalidator" in SYSTEM_PROMPT

    def test_system_prompt_lists_all_three_statuses(self):
        for status in ("broken", "intact", "partial"):
            assert f'"{status}"' in SYSTEM_PROMPT or f"'{status}'" in SYSTEM_PROMPT

    def test_system_prompt_close_schema_has_thesis_fields(self):
        """JSON-schema для Close в SYSTEM_PROMPT упоминает оба поля."""
        # Грубая проверка — поля упомянуты в близости с "Close:" header
        close_section_start = SYSTEM_PROMPT.find("Close:")
        # ищем после Close: оба поля до Hold:
        hold_section_start = SYSTEM_PROMPT.find("Hold:", close_section_start)
        assert close_section_start != -1
        assert hold_section_start != -1
        close_schema = SYSTEM_PROMPT[close_section_start:hold_section_start]
        assert "thesis_status" in close_schema
        assert "thesis_invalidator" in close_schema

    def test_system_prompt_review_has_thesis_discipline(self):
        assert "THESIS DISCIPLINE" in SYSTEM_PROMPT_REVIEW
        assert "thesis_status" in SYSTEM_PROMPT_REVIEW
        assert "thesis_invalidator" in SYSTEM_PROMPT_REVIEW

    def test_system_prompt_review_close_schema_has_thesis_fields(self):
        close_section_start = SYSTEM_PROMPT_REVIEW.find("Close:")
        hold_section_start = SYSTEM_PROMPT_REVIEW.find("Hold:", close_section_start)
        assert close_section_start != -1
        assert hold_section_start != -1
        close_schema = SYSTEM_PROMPT_REVIEW[close_section_start:hold_section_start]
        assert "thesis_status" in close_schema
        assert "thesis_invalidator" in close_schema


# ─── main._extract_thesis ────────────────────────────────────────────────


class TestExtractThesis:
    def test_extracts_both_fields_from_close(self):
        raw = {
            "action": "close",
            "position_id": 1,
            "thesis_status": "broken",
            "thesis_invalidator": "EIA -8 Bcf",
        }
        assert _extract_thesis(raw) == ("broken", "EIA -8 Bcf")

    def test_returns_none_when_missing(self):
        raw = {"action": "close", "position_id": 1}
        assert _extract_thesis(raw) == (None, None)

    def test_returns_none_for_open_action(self):
        raw = {"action": "open", "symbol": "XAUUSD", "side": "BUY"}
        assert _extract_thesis(raw) == (None, None)

    def test_returns_none_for_non_string_types(self):
        """Defensive: если LLM прислал число или dict вместо string."""
        raw = {
            "action": "close",
            "position_id": 1,
            "thesis_status": 42,           # not str
            "thesis_invalidator": ["x"],   # not str
        }
        assert _extract_thesis(raw) == (None, None)


# ─── v4 prompt-tune (A+B+C+D, BUILDLOG 2026-05-26) ──────────────────────


class TestSystemPromptTaskSandwich:
    """A: task-summary в начале SYSTEM_PROMPT — deepseekai.guide
    Practitioner's Guide («early tokens весомее»)."""

    def test_task_block_appears_before_gold_framework(self):
        """Task-summary должен быть ДО длинного domain context'а."""
        task_idx = SYSTEM_PROMPT.find("YOUR TASK EACH CYCLE")
        gold_idx = SYSTEM_PROMPT.find("GOLD (XAUUSD) — FIVE-DRIVER")
        assert task_idx != -1, "Task summary block missing"
        assert gold_idx != -1
        assert task_idx < gold_idx, (
            "Task summary должен быть ДО длинного domain context"
        )

    def test_task_block_mentions_all_three_actions(self):
        task_section_start = SYSTEM_PROMPT.find("YOUR TASK EACH CYCLE")
        task_section = SYSTEM_PROMPT[task_section_start:task_section_start + 800]
        for action in ("open", "close", "hold"):
            assert f"`{action}`" in task_section, f"action `{action}` missing in task summary"

    def test_task_block_mentions_thesis_status_for_close(self):
        task_section_start = SYSTEM_PROMPT.find("YOUR TASK EACH CYCLE")
        task_section = SYSTEM_PROMPT[task_section_start:task_section_start + 800]
        assert "thesis_status" in task_section


class TestSystemPromptConcreteExamples:
    """B: concrete JSON examples с заполненными values — deepseekai.guide
    «show small example schema, not just describe it»."""

    def test_concrete_examples_block_exists(self):
        assert "CONCRETE EXAMPLES" in SYSTEM_PROMPT

    def test_example_open_has_filled_values(self):
        """Example OPEN должен быть с realistic numeric values, не <placeholders>."""
        examples_start = SYSTEM_PROMPT.find("CONCRETE EXAMPLES")
        examples_end = SYSTEM_PROMPT.find("FINAL RULES", examples_start)
        examples_section = SYSTEM_PROMPT[examples_start:examples_end]
        assert '"action": "open"' in examples_section
        assert '"BZ=F"' in examples_section or '"XAUUSD"' in examples_section
        assert '"BUY"' in examples_section
        # должен быть пример без placeholder'ов
        assert "<float" not in examples_section
        assert "<number" not in examples_section

    def test_example_close_has_all_three_thesis_statuses(self):
        examples_start = SYSTEM_PROMPT.find("CONCRETE EXAMPLES")
        examples_end = SYSTEM_PROMPT.find("FINAL RULES", examples_start)
        examples_section = SYSTEM_PROMPT[examples_start:examples_end]
        for status in ("broken", "intact", "partial"):
            assert f'"thesis_status": "{status}"' in examples_section, (
                f"Concrete example для thesis_status={status!r} отсутствует"
            )

    def test_example_close_intact_cites_locked_profit_invalidator(self):
        """thesis_status=intact close должен показать как использовать
        alternative trigger в thesis_invalidator."""
        examples_start = SYSTEM_PROMPT.find("CONCRETE EXAMPLES")
        examples_end = SYSTEM_PROMPT.find("FINAL RULES", examples_start)
        examples_section = SYSTEM_PROMPT[examples_start:examples_end]
        assert "locked-profit" in examples_section

    def test_review_prompt_has_concrete_examples_too(self):
        assert "CONCRETE EXAMPLES" in SYSTEM_PROMPT_REVIEW

    def test_review_examples_dont_use_open_action(self):
        """В review-mode пример open — запрещён (см. SYSTEM_PROMPT_REVIEW)."""
        examples_start = SYSTEM_PROMPT_REVIEW.find("CONCRETE EXAMPLES")
        examples_end = SYSTEM_PROMPT_REVIEW.find("FINAL RULES", examples_start)
        assert examples_start != -1
        examples_section = SYSTEM_PROMPT_REVIEW[examples_start:examples_end]
        assert '"action": "open"' not in examples_section


class TestUserPromptTaskSandwichAndPrime:
    """A+C: task-restatement и prime expected output в build_user_prompt[_review]."""

    def test_full_user_prompt_has_task_restatement(self):
        out = build_user_prompt("MARKET_CTX")
        assert "TASK RESTATEMENT" in out

    def test_full_user_prompt_ends_with_prime_directive(self):
        out = build_user_prompt("MARKET_CTX")
        # Prime directive должен быть в **конце** (после market context)
        prime_idx = out.find("Begin your reply with")
        market_idx = out.find("MARKET_CTX")
        assert prime_idx != -1, "prime directive missing"
        assert market_idx < prime_idx

    def test_full_user_prompt_primes_analysis_header(self):
        out = build_user_prompt("MARKET_CTX")
        assert "## ANALYSIS" in out
        assert "1) MACRO DRIVER:" in out

    def test_full_user_prompt_task_after_market_context(self):
        """Task-sandwich: restatement идёт ПОСЛЕ market_context."""
        out = build_user_prompt(
            "MARKET_CTX",
            performance_by_symbol="PERF",
            recent_trades="TRADES",
        )
        market_idx = out.find("MARKET_CTX")
        task_restate_idx = out.find("TASK RESTATEMENT")
        assert 0 <= market_idx < task_restate_idx

    def test_review_user_prompt_has_task_restatement(self):
        out = build_user_prompt_review("REVIEW_CTX")
        assert "TASK RESTATEMENT" in out

    def test_review_user_prompt_primes_review_header(self):
        out = build_user_prompt_review("REVIEW_CTX")
        assert "Begin your reply" in out
        assert "## REVIEW" in out

    def test_review_user_prompt_forbids_open(self):
        """Outro явно напоминает что open запрещён."""
        out = build_user_prompt_review("REVIEW_CTX")
        assert "FORBIDDEN" in out or "forbidden" in out


# ─── D: output_config.effort в DeepSeekClient ───────────────────────────


class TestDeepSeekClientEffortParam:
    """D: ``effort`` передаётся через extra_body."""

    def test_effort_none_does_not_send_extra_body(self):
        client = DeepSeekClient(api_key="sk-test", effort=None)
        mock_messages = MagicMock()
        mock_messages.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="hello")],
            usage=MagicMock(input_tokens=10, output_tokens=5),
        )
        with patch.object(client, "_client") as mock_anthropic:
            mock_anthropic.messages = mock_messages
            client._call("sys", "usr", with_thinking=True)
        call_kwargs = mock_messages.create.call_args.kwargs
        assert "extra_body" not in call_kwargs

    def test_effort_high_sends_output_config_via_extra_body(self):
        client = DeepSeekClient(api_key="sk-test", effort="high")
        mock_messages = MagicMock()
        mock_messages.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="hello")],
            usage=MagicMock(input_tokens=10, output_tokens=5),
        )
        with patch.object(client, "_client") as mock_anthropic:
            mock_anthropic.messages = mock_messages
            client._call("sys", "usr", with_thinking=True)
        call_kwargs = mock_messages.create.call_args.kwargs
        assert call_kwargs["extra_body"] == {"output_config": {"effort": "high"}}

    def test_effort_not_sent_in_no_thinking_fallback(self):
        """effort attached ТОЛЬКО к thinking-calls (без thinking — нет смысла)."""
        client = DeepSeekClient(api_key="sk-test", effort="high")
        mock_messages = MagicMock()
        mock_messages.create.return_value = MagicMock(
            content=[MagicMock(type="text", text="hello")],
            usage=MagicMock(input_tokens=10, output_tokens=5),
        )
        with patch.object(client, "_client") as mock_anthropic:
            mock_anthropic.messages = mock_messages
            client._call("sys", "usr", with_thinking=False)
        call_kwargs = mock_messages.create.call_args.kwargs
        assert "extra_body" not in call_kwargs
        assert "thinking" not in call_kwargs

    def test_default_effort_is_none_for_backward_compat(self):
        """Backward compat: ai_trader вызывает без effort → None → не передаётся."""
        client = DeepSeekClient(api_key="sk-test")
        assert client._effort is None
