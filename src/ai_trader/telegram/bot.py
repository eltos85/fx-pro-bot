"""Telegram-бот для AI-Trader.

Реализован на чистом requests без python-telegram-bot SDK — меньше
зависимостей и проще thread-модель (нет async).

Возможности:
- Команды: /start, /status, /pnl, /last_decision, /history, /pause, /resume, /help
- Auto-detect chat_id: при первой команде от любого пользователя бот
  запоминает chat_id в БД (поле kv_state.telegram_chat_id) и далее шлёт
  push-уведомления туда. Если в .env задан TELEGRAM_CHAT_ID — используется он.
- Push при open/close позиций и срабатывании killswitch.
- Polling в отдельном daemon-thread'е, не блокирует главный цикл.
- Long-poll с таймаутом 30 секунд (Telegram-стандарт).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: int | None  # из .env; если None — auto-detect из БД
    enabled: bool = True


class TelegramBot:
    """Минимальный Telegram-клиент с polling и broadcast."""

    def __init__(
        self,
        config: TelegramConfig,
        store,  # AiTraderStore (без импорта циклически)
        commands: dict[str, Callable[[str], str]] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.commands = commands or {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_update_id: int = 0

    # ─── HTTP helpers ────────────────────────────────────────────────────

    def _api(self, method: str, params: dict | None = None, timeout: float = 35.0):
        url = TELEGRAM_API.format(token=self.config.bot_token, method=method)
        try:
            r = requests.get(url, params=params or {}, timeout=timeout)
            data = r.json()
            if not data.get("ok"):
                log.warning("telegram %s NOK: %s", method, data)
            return data
        except Exception:
            log.exception("telegram %s failed", method)
            return None

    # ─── Outgoing messages ───────────────────────────────────────────────

    def _resolve_chat_id(self) -> int | None:
        if self.config.chat_id:
            return self.config.chat_id
        return self.store.get_telegram_chat_id()

    def send(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.config.enabled:
            return False
        chat_id = self._resolve_chat_id()
        if not chat_id:
            log.debug("telegram: no chat_id yet, skipping send")
            return False
        # Telegram limit ~4096 chars; нарежем на куски
        for chunk in _split_message(text, 3800):
            self._api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
        return True

    def notify_open(self, summary: str) -> None:
        self.send(f"🟢 *POSITION OPENED*\n```\n{summary}\n```")

    def notify_close(self, summary: str) -> None:
        self.send(f"🔴 *POSITION CLOSED*\n```\n{summary}\n```")

    def notify_killswitch(self, reason: str) -> None:
        self.send(f"⚠️ *KILLSWITCH*\n{reason}")

    def notify_error(self, where: str, err: str) -> None:
        self.send(f"❌ *ERROR* in {where}\n```\n{err[:500]}\n```")

    # ─── Polling thread ──────────────────────────────────────────────────

    def start(self) -> None:
        if not self.config.enabled or not self.config.bot_token:
            log.info("telegram: disabled или нет bot_token")
            return
        # Sanity-проверка токена + greeting
        me = self._api("getMe", timeout=10)
        if not me or not me.get("ok"):
            log.error("telegram: getMe failed, бот не стартует")
            return
        username = me.get("result", {}).get("username", "?")
        log.info("telegram: подключён как @%s", username)

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tg-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self._api(
                    "getUpdates",
                    {"offset": self._last_update_id + 1, "timeout": 30},
                    timeout=35,
                )
                if not resp or not resp.get("ok"):
                    time.sleep(5)
                    continue
                for upd in resp.get("result", []):
                    self._last_update_id = max(self._last_update_id, upd.get("update_id", 0))
                    self._handle_update(upd)
            except Exception:
                log.exception("telegram poll error")
                time.sleep(5)

    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        # Auto-bind chat_id при первой команде если не задан в .env
        if not self.config.chat_id:
            stored = self.store.get_telegram_chat_id()
            if stored != chat_id:
                self.store.set_telegram_chat_id(int(chat_id))
                log.info("telegram: bound chat_id=%s", chat_id)

        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().strip()
        # Telegram добавляет @bot_username если упомянут в группе
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]

        handler = self.commands.get(cmd)
        if not handler:
            self._reply(chat_id, "Неизвестная команда. /help — список доступных.")
            return
        try:
            reply = handler(args.strip())
        except Exception as e:
            log.exception("telegram cmd handler %s failed", cmd)
            reply = f"Ошибка: {e}"
        self._reply(chat_id, reply)

    def _reply(self, chat_id: int, text: str) -> None:
        for chunk in _split_message(text, 3800):
            self._api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )


# ─── Utilities ───────────────────────────────────────────────────────────


def _split_message(text: str, max_len: int) -> list[str]:
    """Режет длинное сообщение на куски с учётом строк."""
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    cur = 0
    for line in text.splitlines(keepends=True):
        if cur + len(line) > max_len and buf:
            out.append("".join(buf))
            buf = []
            cur = 0
        buf.append(line)
        cur += len(line)
    if buf:
        out.append("".join(buf))
    return out


# ─── Command builders ────────────────────────────────────────────────────


def build_command_handlers(store, settings, killswitch) -> dict[str, Callable[[str], str]]:
    """Создаёт словарь команда→handler. Используется в main.py."""

    def cmd_start(_: str) -> str:
        return (
            "👋 *AI-Trader online*\n\n"
            "Я — автономный криптотрейдер на DeepSeek-V4. Торгую на Bybit demo.\n\n"
            "Команды:\n"
            "/status — текущее состояние\n"
            "/pnl — PnL за сегодня и total\n"
            "/last\\_decision — последнее решение LLM\n"
            "/history — последние 5 решений\n"
            "/pause — приостановить торговлю\n"
            "/resume — возобновить\n"
            "/help — эта справка\n\n"
            "_Push-уведомления будут приходить при открытии/закрытии позиций "
            "и срабатывании killswitch._"
        )

    def cmd_help(_: str) -> str:
        return cmd_start("")

    def cmd_status(_: str) -> str:
        positions = store.get_open_positions()
        today_pnl = store.get_today_pnl()
        total_pnl = store.get_total_pnl()
        n_closed, n_wins = store.get_closed_positions_count()
        wr = (n_wins / n_closed * 100) if n_closed else 0
        paused = store.is_paused()
        ks = killswitch.check_can_trade()

        lines = [
            "📊 *AI-Trader status*",
            f"Mode: `{'PAUSED' if paused else ('LIVE' if settings.trading_enabled else 'PAPER')}`",
            f"Symbols: {', '.join(settings.symbols)}",
            f"Virtual capital: ${settings.virtual_capital_usd:.2f}",
            f"Open positions: {len(positions)} / {settings.max_open_positions}",
            f"Today PnL: ${today_pnl:+.2f}  Total: ${total_pnl:+.2f}",
            f"Closed trades: {n_closed} (WR {wr:.0f}%)",
            f"Killswitch: {'OK' if ks.allowed else 'BLOCKED — ' + ks.reason}",
        ]
        if positions:
            lines.append("\n*Open positions:*")
            for p in positions:
                lines.append(
                    f"  • id={p.id} {p.side} {p.symbol} qty={p.qty} "
                    f"entry=${p.entry_price:.4g} SL=${p.sl_price or 0:.4g} "
                    f"TP=${p.tp_price or 0:.4g}"
                )
        return "\n".join(lines)

    def cmd_pnl(_: str) -> str:
        today = store.get_today_pnl()
        total = store.get_total_pnl()
        n_closed, n_wins = store.get_closed_positions_count()
        wr = (n_wins / n_closed * 100) if n_closed else 0
        return (
            "💰 *PnL*\n"
            f"Today: `${today:+.2f}`  (limit -${settings.max_daily_loss_usd:.0f})\n"
            f"Total: `${total:+.2f}`  (limit -${settings.max_total_loss_usd:.0f})\n"
            f"Trades: {n_closed} closed, {n_wins} wins, WR {wr:.0f}%"
        )

    def cmd_last_decision(_: str) -> str:
        rows = store.get_recent_decisions(limit=1)
        if not rows:
            return "Решений ещё не было."
        r = rows[0]
        action_obj = json.loads(r["parsed_action"]) if r["parsed_action"] else None
        action = action_obj.get("action") if action_obj else "?"
        reason = action_obj.get("reason", "") if action_obj else ""
        ts = r["ts"]
        executed = "✓ executed" if r["executed"] else "✗ not executed"
        err = f"\nerror: `{r['error']}`" if r.get("error") else ""
        body = json.dumps(action_obj, indent=2, ensure_ascii=False) if action_obj else "(none)"
        return (
            f"🧠 *Last decision* (cycle {r['cycle']}, {ts})\n"
            f"{action.upper()} | {executed}{err}\n"
            f"Reason: _{reason}_\n"
            f"```json\n{body}\n```"
        )

    def cmd_history(arg: str) -> str:
        try:
            n = max(1, min(int(arg), 20)) if arg else 5
        except ValueError:
            n = 5
        rows = store.get_recent_decisions(limit=n)
        if not rows:
            return "Решений ещё не было."
        lines = [f"📜 *Last {len(rows)} decisions*"]
        for r in rows:
            obj = json.loads(r["parsed_action"]) if r["parsed_action"] else None
            action = obj.get("action") if obj else "?"
            reason = (obj.get("reason", "")[:60] + "…") if obj and obj.get("reason") else ""
            mark = "✓" if r["executed"] else "·"
            ts_short = r["ts"][:16].replace("T", " ")
            lines.append(f"`{ts_short}` {mark} *{action}* — {reason}")
        return "\n".join(lines)

    def cmd_pause(_: str) -> str:
        store.set_paused(True)
        return "⏸ *Торговля приостановлена*. /resume — продолжить."

    def cmd_resume(_: str) -> str:
        store.set_paused(False)
        return "▶️ *Торговля возобновлена*."

    return {
        "/start": cmd_start,
        "/help": cmd_help,
        "/status": cmd_status,
        "/pnl": cmd_pnl,
        "/last_decision": cmd_last_decision,
        "/last": cmd_last_decision,  # alias
        "/history": cmd_history,
        "/pause": cmd_pause,
        "/resume": cmd_resume,
    }
