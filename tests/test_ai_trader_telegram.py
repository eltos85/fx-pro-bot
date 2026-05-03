"""Тесты Telegram-модуля AI-Trader.

Проверяем:
- _split_message режет длинные сообщения
- KV-state в БД (paused, chat_id)
- Команды (/status, /pnl, /last_decision, /history, /pause, /resume)
  работают на пустой и наполненной БД
- TelegramBot не падает без chat_id
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai_trader.config.settings import AiTraderSettings
from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from ai_trader.state.db import AiTraderStore
from ai_trader.telegram.bot import (
    TelegramBot,
    TelegramConfig,
    _split_message,
    build_command_handlers,
)


@pytest.fixture
def store(tmp_path: Path) -> AiTraderStore:
    return AiTraderStore(tmp_path / "tg.sqlite")


@pytest.fixture
def settings() -> AiTraderSettings:
    return AiTraderSettings(_env_file=None)


@pytest.fixture
def killswitch(store: AiTraderStore) -> KillSwitch:
    return KillSwitch(
        KillSwitchConfig(
            max_daily_loss_usd=50, max_total_loss_usd=200,
            max_open_positions=3, max_leverage=5,
        ),
        store,
    )


# ─── _split_message ──────────────────────────────────────────────────────


class TestSplitMessage:
    def test_short_returns_single_chunk(self):
        assert _split_message("hello", 100) == ["hello"]

    def test_long_split_by_lines(self):
        text = "\n".join(["aaa"] * 100)
        chunks = _split_message(text, 50)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 50 + 4  # с учётом newline-границ

    def test_preserves_total_content(self):
        text = "line1\nline2\nline3\n"
        chunks = _split_message(text, 7)
        # Все строки должны присутствовать в склейке
        joined = "".join(chunks)
        assert "line1" in joined and "line2" in joined and "line3" in joined


# ─── KV state ────────────────────────────────────────────────────────────


class TestKvState:
    def test_paused_default_false(self, store):
        assert store.is_paused() is False

    def test_paused_persists(self, store):
        store.set_paused(True)
        assert store.is_paused() is True
        store.set_paused(False)
        assert store.is_paused() is False

    def test_chat_id_default_none(self, store):
        assert store.get_telegram_chat_id() is None

    def test_chat_id_persists(self, store):
        store.set_telegram_chat_id(123456)
        assert store.get_telegram_chat_id() == 123456

    def test_chat_id_invalid_in_kv(self, store):
        store.kv_set("telegram_chat_id", "not_a_number")
        assert store.get_telegram_chat_id() is None


# ─── Команды ─────────────────────────────────────────────────────────────


class TestCommands:
    def test_status_empty(self, store, settings, killswitch):
        h = build_command_handlers(store, settings, killswitch)
        out = h["/status"]("")
        assert "AI-Trader status" in out
        assert "Open positions: 0" in out
        assert "Killswitch: OK" in out

    def test_status_with_position(self, store, settings, killswitch):
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.001, entry_price=60000,
            sl_price=58000, tp_price=63000, leverage=3,
            order_link_id="ai_test1", llm_reason="test",
        )
        h = build_command_handlers(store, settings, killswitch)
        out = h["/status"]("")
        assert "Open positions: 1" in out
        assert "BTCUSDT" in out

    def test_pnl_empty(self, store, settings, killswitch):
        h = build_command_handlers(store, settings, killswitch)
        out = h["/pnl"]("")
        assert "Today: `$+0.00`" in out
        assert "Total: `$+0.00`" in out

    def test_pnl_after_close(self, store, settings, killswitch):
        pid = store.open_position(
            symbol="ETHUSDT", side="Sell", qty=0.5, entry_price=3000,
            sl_price=3100, tp_price=2800, leverage=3,
            order_link_id="ai_aaa", llm_reason="x",
        )
        store.close_position(pid, exit_price=2900, realized_pnl_usd=50.0, close_reason="tp")
        h = build_command_handlers(store, settings, killswitch)
        out = h["/pnl"]("")
        assert "+50.00" in out
        assert "WR 100%" in out

    def test_last_decision_empty(self, store, settings, killswitch):
        h = build_command_handlers(store, settings, killswitch)
        out = h["/last_decision"]("")
        assert "Решений ещё не было" in out

    def test_last_decision_with_data(self, store, settings, killswitch):
        store.log_decision(
            cycle=5, prompt_system="s", prompt_user="u",
            response_raw='{"action":"hold","reason":"low vol"}',
            parsed_action={"action": "hold", "reason": "low vol"},
            executed=False, error=None,
            tokens_input=100, tokens_output=20, cost_usd=0.0001,
        )
        h = build_command_handlers(store, settings, killswitch)
        out = h["/last_decision"]("")
        assert "cycle 5" in out
        assert "HOLD" in out
        assert "low vol" in out

    def test_history(self, store, settings, killswitch):
        for i in range(3):
            store.log_decision(
                cycle=i, prompt_system="s", prompt_user="u",
                response_raw=f"{{\"action\":\"hold\",\"reason\":\"r{i}\"}}",
                parsed_action={"action": "hold", "reason": f"r{i}"},
                executed=False, error=None,
            )
        h = build_command_handlers(store, settings, killswitch)
        out = h["/history"]("")
        assert "Last 3 decisions" in out
        assert "r0" in out and "r1" in out and "r2" in out

    def test_pause_resume(self, store, settings, killswitch):
        h = build_command_handlers(store, settings, killswitch)
        h["/pause"]("")
        assert store.is_paused() is True
        h["/resume"]("")
        assert store.is_paused() is False

    def test_start_help_works(self, store, settings, killswitch):
        h = build_command_handlers(store, settings, killswitch)
        assert "AI-Trader" in h["/start"]("")
        assert "AI-Trader" in h["/help"]("")


# ─── TelegramBot без chat_id и без token ─────────────────────────────────


class TestTelegramBotInit:
    def test_send_skipped_when_disabled(self, store):
        cfg = TelegramConfig(bot_token="", chat_id=None, enabled=False)
        bot = TelegramBot(cfg, store)
        assert bot.send("hi") is False

    def test_send_skipped_when_no_chat_id(self, store):
        cfg = TelegramConfig(bot_token="x", chat_id=None, enabled=True)
        bot = TelegramBot(cfg, store)
        # Нет chat_id ни в config ни в БД → send возвращает False, не кидает
        assert bot.send("hi") is False

    def test_send_uses_db_chat_id_when_no_config(self, store, monkeypatch):
        store.set_telegram_chat_id(99999)
        cfg = TelegramConfig(bot_token="x", chat_id=None, enabled=True)
        bot = TelegramBot(cfg, store)
        called = {}

        def fake_api(method, params=None, timeout=None):
            called["method"] = method
            called["chat_id"] = params.get("chat_id")
            return {"ok": True}

        bot._api = fake_api  # type: ignore[method-assign]
        assert bot.send("hi") is True
        assert called["chat_id"] == 99999

    def test_handle_update_binds_chat_id(self, store):
        cfg = TelegramConfig(bot_token="x", chat_id=None, enabled=True)
        bot = TelegramBot(cfg, store, commands={"/start": lambda _: "ok"})
        bot._api = MagicMock(return_value={"ok": True})

        update = {
            "update_id": 1,
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "text": "/start",
            },
        }
        bot._handle_update(update)
        assert store.get_telegram_chat_id() == 12345

    def test_handle_update_unknown_command_replies(self, store):
        cfg = TelegramConfig(bot_token="x", chat_id=None, enabled=True)
        bot = TelegramBot(cfg, store, commands={"/start": lambda _: "ok"})
        api_mock = MagicMock(return_value={"ok": True})
        bot._api = api_mock

        update = {
            "update_id": 2,
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "text": "/nonexistent",
            },
        }
        bot._handle_update(update)
        # Должен был вызвать sendMessage с текстом про unknown
        sent_text = api_mock.call_args.args[1]["text"] if api_mock.call_args else ""
        assert "Неизвестная команда" in sent_text
