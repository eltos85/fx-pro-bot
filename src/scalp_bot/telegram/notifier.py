"""Лёгкий Telegram-нотификатор scalp_bot.

Только исходящие сообщения (sendMessage), без поллинга/команд — чтобы не
конфликтовать с другими ботами на том же токене. No-op если выключен или
нет token/chat_id. Ошибки сети глушатся (нотификации не должны ронять торговлю).

Bybit Telegram Bot API: https://core.telegram.org/bots/api#sendmessage
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("scalp_bot.tg")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, enabled: bool = True,
                 timeout: float = 5.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._timeout = timeout
        if enabled and not (token and chat_id):
            log.warning("Telegram включён, но нет token/chat_id — нотификации выкл")

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
