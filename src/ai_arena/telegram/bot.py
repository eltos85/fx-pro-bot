"""Telegram-бот для AI Arena (read-only UX, не часть Nof1-стратегии).

Отдельный токен от ai_trader — env var ``AI_ARENA_TELEGRAM_BOT_TOKEN``.
Если токен пустой — TG-модуль молчит, всё остальное работает как обычно.

Команды: /start, /help, /status, /pnl, /last_decision, /history.

Auto-detect chat_id: при первой команде запоминается в БД (kv_state).
Push-нотификации при open / close / error.

ВАЖНО: никаких /pause /resume / killswitch команд — Nof1 source не
имеет server-side capital safety hard-limits. Telegram только для
read-only мониторинга и push'ей о значимых событиях.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: int | None
    enabled: bool = True


# Network errors которые ожидаемы для long-polling getUpdates: peer
# (api.telegram.org) ритмично закрывает idle TCP-соединение, особенно
# когда нет updates в течение polling timeout (30s). Это НЕ ошибка бота
# и не должна спамить stack-trace'ами в лог. Логируем как warning без
# трассировки и продолжаем polling.
_TG_NETWORK_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


class TelegramArenaBot:
    def __init__(
        self,
        config: TelegramConfig,
        store,
        commands: dict[str, Callable[[str], str]] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.commands = commands or {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_update_id: int = 0
        # requests.Session переиспользует TCP/TLS соединение к
        # api.telegram.org — меньше handshake'ов, меньше шанс RST от peer'а.
        self._session = requests.Session()

    def _api(self, method: str, params: dict | None = None, timeout: float = 35.0):
        url = TELEGRAM_API.format(token=self.config.bot_token, method=method)
        try:
            r = self._session.get(url, params=params or {}, timeout=timeout)
            data = r.json()
            if not data.get("ok"):
                log.warning("telegram %s NOK: %s", method, data)
            return data
        except _TG_NETWORK_EXC as e:
            # Ожидаемый network glitch — не фатально, retry на следующей
            # итерации polling. Без stack-trace.
            log.warning("telegram %s network glitch (%s)", method, type(e).__name__)
            return None
        except Exception:
            log.exception("telegram %s failed", method)
            return None

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

    def notify_error(self, where: str, err: str) -> None:
        self.send(f"❌ *ERROR* in {where}\n```\n{err[:500]}\n```")

    def start(self) -> None:
        if not self.config.enabled or not self.config.bot_token:
            log.info("telegram: disabled или нет bot_token")
            return
        me = self._api("getMe", timeout=10)
        if not me or not me.get("ok"):
            log.error("telegram: getMe failed, бот не стартует")
            return
        username = me.get("result", {}).get("username", "?")
        log.info("telegram: подключён как @%s", username)

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="arena-tg")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._session.close()
        except Exception:
            pass

    def _run_loop(self) -> None:
        # Простой backoff: 1s после network glitch'а, удваиваем до 30s.
        # На успешном poll сбрасываем обратно к 1s.
        backoff = 1.0
        while not self._stop.is_set():
            try:
                resp = self._api(
                    "getUpdates",
                    {"offset": self._last_update_id + 1, "timeout": 30},
                    timeout=35,
                )
                if not resp or not resp.get("ok"):
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = 1.0
                for upd in resp.get("result", []):
                    self._last_update_id = max(
                        self._last_update_id, upd.get("update_id", 0)
                    )
                    self._handle_update(upd)
            except _TG_NETWORK_EXC as e:
                # Сюда обычно не попадаем — _api сам глотает network exc;
                # double-safety на случай если упадёт за пределами _api.
                log.warning("telegram poll network glitch (%s)", type(e).__name__)
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30.0)
            except Exception:
                log.exception("telegram poll error")
                if self._stop.wait(5):
                    break

    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if not self.config.chat_id:
            stored = self.store.get_telegram_chat_id()
            if stored != chat_id:
                self.store.set_telegram_chat_id(int(chat_id))
                log.info("telegram: bound chat_id=%s", chat_id)

        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().strip()
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


def _split_message(text: str, max_len: int) -> list[str]:
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


def build_command_handlers(store, settings) -> dict[str, Callable[[str], str]]:
    """Команды для AI Arena (read-only мониторинг)."""

    def cmd_start(_: str) -> str:
        return (
            "👋 *AI Arena online*\n\n"
            "Я — клон Nof1.ai Alpha Arena на DeepSeek + Bybit demo.\n\n"
            "Команды:\n"
            "/status — текущее состояние\n"
            "/pnl — реализованный PnL\n"
            "/last\\_decision — последнее решение LLM\n"
            "/history — последние 5 решений\n"
            "/help — эта справка"
        )

    def cmd_help(_: str) -> str:
        return cmd_start("")

    def cmd_status(_: str) -> str:
        positions = store.get_open_positions()
        today_pnl = store.get_today_pnl()
        total_pnl = store.get_total_pnl()
        n_closed, n_wins = store.get_closed_positions_count()
        wr = (n_wins / n_closed * 100) if n_closed else 0

        lines = [
            "📊 *AI Arena status*",
            f"Mode: `{'LIVE' if settings.trading_enabled else 'PAPER'}`",
            f"Symbols: {', '.join(settings.symbols)}",
            f"Virtual capital: ${settings.virtual_capital_usd:.2f} (leverage cap 1-{settings.leverage_max}x)",
            f"Open positions: {len(positions)}",
            f"Today realized PnL: ${today_pnl:+.2f}  Total: ${total_pnl:+.2f}",
            f"Closed trades: {n_closed} (WR {wr:.0f}%)",
        ]
        if positions:
            lines.append("\n*Open positions:*")
            for p in positions:
                lines.append(
                    f"  • id={p.id} {p.side} {p.symbol} qty={p.qty} "
                    f"entry=${p.entry_price:.4g} SL=${p.sl_price or 0:.4g} "
                    f"TP=${p.tp_price or 0:.4g} conf={p.confidence or 0:.2f}"
                )
        return "\n".join(lines)

    def cmd_pnl(_: str) -> str:
        today = store.get_today_pnl()
        total = store.get_total_pnl()
        n_closed, n_wins = store.get_closed_positions_count()
        wr = (n_wins / n_closed * 100) if n_closed else 0
        return (
            "💰 *Realized PnL* (closed positions only)\n"
            f"Today: `${today:+.2f}`\n"
            f"Total: `${total:+.2f}`\n"
            f"Trades: {n_closed} closed, {n_wins} wins, WR {wr:.0f}%"
        )

    def cmd_last_decision(_: str) -> str:
        rows = store.get_recent_decisions(limit=1)
        if not rows:
            return "Решений ещё не было."
        r = rows[0]
        action_obj = json.loads(r["parsed_action"]) if r["parsed_action"] else None
        signal = action_obj.get("signal") if action_obj else (r.get("signal") or "?")
        just = action_obj.get("justification", "") if action_obj else ""
        executed = "✓ executed" if r["executed"] else "✗ not executed"
        err = f"\nerror: `{r['error']}`" if r.get("error") else ""
        body = json.dumps(action_obj, indent=2, ensure_ascii=False) if action_obj else "(none)"
        return (
            f"🧠 *Last decision* (cycle {r['cycle']}, {r['ts']})\n"
            f"{signal.upper()} | {executed}{err}\n"
            f"Justification: _{just[:300]}_\n"
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
            signal = (obj.get("signal") if obj else r.get("signal")) or "?"
            just = obj.get("justification", "") if obj else ""
            short_just = (just[:60] + "…") if len(just) > 60 else just
            mark = "✓" if r["executed"] else "·"
            ts_short = r["ts"][:16].replace("T", " ")
            lines.append(f"`{ts_short}` {mark} *{signal}* — {short_just}")
        return "\n".join(lines)

    return {
        "/start": cmd_start,
        "/help": cmd_help,
        "/status": cmd_status,
        "/pnl": cmd_pnl,
        "/last_decision": cmd_last_decision,
        "/last": cmd_last_decision,
        "/history": cmd_history,
    }
