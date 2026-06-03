"""Telegram-нотификатор (только исходящие). https://core.telegram.org/bots/api"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("ru_stocks.tg")


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        enabled: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._timeout = timeout
        if enabled and not (token and chat_id):
            log.warning("Telegram: нет token/chat_id — выкл")

    @property
    def active(self) -> bool:
        return self._enabled

    def send(self, text: str) -> bool:
        if not self._enabled:
            log.info("Telegram dry/disabled, msg len=%d", len(text))
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        # лимит 4096 — режем
        chunks = []
        s = text
        while s:
            chunks.append(s[:3800])
            s = s[3800:]
        ok = True
        for chunk in chunks:
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=self._timeout,
                )
                if resp.status_code != 200:
                    log.warning("TG %s: %s", resp.status_code, resp.text[:300])
                    ok = False
            except Exception:
                log.exception("Telegram send failed")
                ok = False
        return ok
