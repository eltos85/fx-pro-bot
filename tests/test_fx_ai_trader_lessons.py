"""Тесты persistent-lessons слоя FX AI Trader (2026-06-02).

Покрытие:
- DB: add_lesson / get_active_lessons / supersede / cap / empty-text guard
- parse_action: CloseAction с полями lesson + lesson_supersedes_id
- executor: на CLOSE с lesson строка попадает в таблицу lessons
- prompts: format_lessons + блок в build_user_prompt

Фича — поведенческие приоры из исходов закрытых сделок. НЕ disable-правила;
механически вход не блокируют (compliance sample-size.mdc / no-data-fitting.mdc).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.llm.prompts import build_user_prompt, format_lessons
from fx_ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.executor import (
    CloseAction,
    ParsedAction,
    apply_action,
    parse_action,
)


@pytest.fixture
def store(tmp_path: Path) -> AiFxTraderStore:
    return AiFxTraderStore(str(tmp_path / "lessons.sqlite"))


# ─── DB layer ─────────────────────────────────────────────────────────────


class TestLessonsDB:
    def test_add_and_get(self, store: AiFxTraderStore):
        lid = store.add_lesson(
            lesson_text="XAUUSD longs need >=1.5 ATR stops; tight stops noise out",
            symbol="XAUUSD", side="BUY", trade_id=42, outcome_usd=-30.66,
        )
        assert lid > 0
        active = store.get_active_lessons()
        assert len(active) == 1
        ls = active[0]
        assert ls["symbol"] == "XAUUSD"
        assert ls["side"] == "BUY"
        assert ls["trade_id"] == 42
        assert ls["outcome_usd"] == pytest.approx(-30.66)
        assert "1.5 ATR" in ls["lesson_text"]

    def test_order_oldest_to_newest(self, store: AiFxTraderStore):
        store.add_lesson(lesson_text="first", symbol="XAUUSD")
        store.add_lesson(lesson_text="second", symbol="BZ=F")
        store.add_lesson(lesson_text="third", symbol="NG=F")
        texts = [x["lesson_text"] for x in store.get_active_lessons()]
        assert texts == ["first", "second", "third"]

    def test_supersede_deactivates_old(self, store: AiFxTraderStore):
        old = store.add_lesson(lesson_text="gold tight stop bad", symbol="XAUUSD")
        new = store.add_lesson(
            lesson_text="gold: use 2 ATR stop, confirmed twice",
            symbol="XAUUSD", supersedes_id=old,
        )
        active = store.get_active_lessons()
        ids = [x["id"] for x in active]
        assert new in ids
        assert old not in ids
        assert len(active) == 1

    def test_cap_enforced(self, store: AiFxTraderStore):
        for i in range(5):
            store.add_lesson(lesson_text=f"lesson {i}", max_active=3)
        active = store.get_active_lessons(limit=50)
        assert len(active) == 3
        # остаются 3 последних (FIFO деактивирует старейшие)
        assert [x["lesson_text"] for x in active] == [
            "lesson 2", "lesson 3", "lesson 4",
        ]

    def test_empty_text_ignored(self, store: AiFxTraderStore):
        assert store.add_lesson(lesson_text="   ") == 0
        assert store.add_lesson(lesson_text="") == 0
        assert store.get_active_lessons() == []

    def test_text_clamped(self, store: AiFxTraderStore):
        store.add_lesson(lesson_text="x" * 500, text_clamp=240)
        assert len(store.get_active_lessons()[0]["lesson_text"]) == 240


# ─── parse_action schema ────────────────────────────────────────────────


class TestCloseActionLessonSchema:
    def test_close_with_lesson_fields(self):
        text = (
            '{"action": "close", "position_id": 7, "reason": "thesis broken", '
            '"thesis_status": "broken", "thesis_invalidator": "EIA print", '
            '"lesson": "NG longs into storage builds fail; wait for draw", '
            '"lesson_supersedes_id": 3}'
        )
        result = parse_action(text, ("XAUUSD", "BZ=F", "NG=F"))
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, CloseAction)
        assert result.model.lesson.startswith("NG longs")
        assert result.model.lesson_supersedes_id == 3

    def test_close_without_lesson_ok(self):
        text = (
            '{"action": "close", "position_id": 7, '
            '"thesis_status": "intact", "thesis_invalidator": "locked-profit 1.6R"}'
        )
        result = parse_action(text, ("XAUUSD",))
        assert isinstance(result, ParsedAction)
        assert result.model.lesson is None
        assert result.model.lesson_supersedes_id is None

    def test_lesson_clamped(self):
        long_lesson = "y" * 400
        text = (
            '{"action": "close", "position_id": 1, "thesis_status": "intact", '
            f'"lesson": "{long_lesson}"}}'
        )
        result = parse_action(text, ("XAUUSD",))
        assert isinstance(result, ParsedAction)
        assert len(result.model.lesson) == 240


# ─── executor persistence ───────────────────────────────────────────────


class _MiniAdapter:
    """Минимальный adapter для paper-close (нужен лишь get_current_price)."""

    def __init__(self, price: float) -> None:
        self._price = price

    def get_current_price(self, internal_symbol: str) -> float | None:
        return self._price


class TestExecutorPersistsLesson:
    def test_paper_close_persists_lesson(self, store: AiFxTraderStore):
        pid = store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.01,
            entry_price=4500.0, sl_price=4470.0, tp_price=4560.0,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="discovery", is_paper=True,
        )
        action = ParsedAction(
            action_type="close",
            model=CloseAction(
                action="close", position_id=pid,
                reason="stopped on noise", thesis_status="broken",
                thesis_invalidator="1H reversal",
                lesson="XAUUSD: 30pt stop too tight, noise-out; widen to 2 ATR",
            ),
            raw={"action": "close", "position_id": pid, "reason": "noise"},
        )
        settings = AiFxTraderSettings()  # trading_enabled=False → paper path
        ks = KillSwitch(KillSwitchConfig(
            max_daily_loss_usd=150, max_total_loss_usd=300,
            max_open_positions=3, max_positions_per_symbol=3,
        ), store)

        result = apply_action(
            action, adapter=_MiniAdapter(4490.0), store=store,
            settings=settings, killswitch=ks,
        )
        assert result.executed is True
        lessons = store.get_active_lessons()
        assert len(lessons) == 1
        assert lessons[0]["trade_id"] == pid
        assert lessons[0]["symbol"] == "XAUUSD"
        assert lessons[0]["side"] == "BUY"
        # outcome_usd записан как realized pnl (отрицательный — закрыли ниже entry)
        assert lessons[0]["outcome_usd"] is not None
        assert lessons[0]["outcome_usd"] < 0
        # рационал закрытия также виден в summary (фикс 2026-06-01)
        assert "stopped on noise" in result.summary

    def test_close_without_lesson_no_row(self, store: AiFxTraderStore):
        pid = store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.01,
            entry_price=4500.0, sl_price=4470.0, tp_price=4560.0,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="discovery", is_paper=True,
        )
        action = ParsedAction(
            action_type="close",
            model=CloseAction(
                action="close", position_id=pid, thesis_status="intact",
                thesis_invalidator="locked-profit 1.6R",
            ),
            raw={"action": "close", "position_id": pid},
        )
        settings = AiFxTraderSettings()
        ks = KillSwitch(KillSwitchConfig(
            max_daily_loss_usd=150, max_total_loss_usd=300,
            max_open_positions=3, max_positions_per_symbol=3,
        ), store)
        result = apply_action(
            action, adapter=_MiniAdapter(4560.0), store=store,
            settings=settings, killswitch=ks,
        )
        assert result.executed is True
        assert store.get_active_lessons() == []


# ─── prompts formatting ──────────────────────────────────────────────────


class TestFormatLessons:
    def test_empty(self):
        assert format_lessons(None) == ""
        assert format_lessons([]) == ""

    def test_render(self):
        block = format_lessons([
            {
                "id": 1, "symbol": "XAUUSD", "side": "BUY",
                "trade_id": 42, "outcome_usd": -30.66,
                "lesson_text": "use 2 ATR stops on gold",
            },
            {
                "id": 2, "symbol": None, "side": None,
                "trade_id": None, "outcome_usd": None,
                "lesson_text": "avoid revenge trades",
            },
        ])
        assert "LESSONS LEARNED" in block
        assert "XAUUSD BUY" in block
        assert "trade #42" in block
        assert "-30.66" in block
        assert "use 2 ATR stops on gold" in block
        assert "GENERAL" in block  # symbol=None → GENERAL

    def test_build_user_prompt_includes_lessons(self):
        prompt = build_user_prompt(
            "MARKET CONTEXT HERE",
            lessons=format_lessons([
                {"id": 1, "symbol": "BZ=F", "side": "SELL",
                 "trade_id": 9, "outcome_usd": 12.0,
                 "lesson_text": "fade Brent spikes after geopolitical headlines"},
            ]),
        )
        assert "LESSONS LEARNED" in prompt
        assert "fade Brent spikes" in prompt

    def test_build_user_prompt_no_lessons_backward_compatible(self):
        prompt = build_user_prompt("MARKET CONTEXT HERE")
        assert "LESSONS LEARNED" not in prompt
