"""Исходящий Telegram impulse-bot. Без поллинга — тот же токен, что у scalp.

https://core.telegram.org/bots/api#sendmessage
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("impulse_bot.tg")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_start(*, demo: bool, session: str) -> str:
    return (f"<b>[impulse]</b> старт demo={demo} session={esc(session)} UTC\n"
            "вход / выход / scratch")


def fmt_enter(*, symbol: str, side: str, qty: float, px: float,
              sl: float, tp: float) -> str:
    return (f"<b>[impulse]</b> вход {esc(side)} {esc(symbol)}\n"
            f"qty={qty:.6f} px={px:.6f}\n"
            f"sl={sl:.6f} tp={tp:.6f}")


def fmt_exit(*, symbol: str, side: str, qty: float, entry: float,
             exit_px: float, pnl_usd: float, reason: str) -> str:
    return (f"<b>[impulse]</b> выход {esc(reason)} {esc(side)} {esc(symbol)}\n"
            f"qty={qty:.6f} {entry:.6f} → {exit_px:.6f}\n"
            f"pnl≈${pnl_usd:.2f}")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, enabled: bool = True,
                 timeout: float = 5.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._timeout = timeout
        if enabled and not (token and chat_id):
            log.warning("Telegram включён, но нет token/chat_id — выкл")

    @property
    def active(self) -> bool:
        return self._enabled

    def send(self, text: str) -> None:
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.warning("Telegram sendMessage %s: %s", resp.status_code,
                            resp.text[:200])
        except Exception:
            log.exception("Telegram send failed")
