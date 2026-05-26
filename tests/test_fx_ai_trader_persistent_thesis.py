"""Тесты Phase 1 persistent-thesis (2026-05-26).

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

См. ``BUILDLOG_AI_FX_TRADER.md`` запись 2026-05-26 «feat(persistent-
thesis)» — research artifact, acceptance criteria.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from fx_ai_trader.app.main import _extract_thesis
from fx_ai_trader.llm.prompts import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_REVIEW,
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
