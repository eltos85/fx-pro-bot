"""Лёгкий Telegram-нотификатор tradecard_momentum (только исходящие).

Изолированная копия паттерна notifier'а (read-only-инфраструктура, без
кросс-зависимости — TASKSPEC §9). No-op если выключен/нет token/chat_id.
Ошибки сети глушатся (отчёт не должен ронять прогон).

Telegram Bot API: https://core.telegram.org/bots/api#sendmessage
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("tradecard_momentum.tg")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, enabled: bool = True,
                 prefix: str = "", timeout: float = 5.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._prefix = prefix
        self._timeout = timeout
        if enabled and not (token and chat_id):
            log.warning("Telegram включён, но нет token/chat_id — нотификации выкл")

    @property
    def active(self) -> bool:
        return self._enabled

    def send(self, text: str) -> None:
        if not self._enabled:
            return
        body = f"{self._prefix} {text}".strip() if self._prefix else text
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": body,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.warning("Telegram sendMessage %s: %s", resp.status_code,
                            resp.text[:200])
        except Exception:
            log.exception("Telegram send failed")
