"""Исходящий Telegram solana-bot. Без поллинга.

https://core.telegram.org/bots/api#sendmessage
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("solana_bot.tg")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def should_alert(last: dict[str, float], key: str, now: float,
                 cooldown_sec: float) -> bool:
    prev = last.get(key)
    if prev is not None and now - prev < cooldown_sec:
        return False
    last[key] = now
    return True


def fmt_start(*, trading: bool) -> str:
    mode = "swap" if trading else "скан"
    return f"<b>[solana]</b> старт {mode} trading={trading}\nкандидат / вход / выход"


def fmt_candidate(*, symbol: str, mint: str, volume_m5: float,
                  move_m5_pct: float, liquidity_usd: float,
                  price_usd: float) -> str:
    short = mint if len(mint) <= 12 else f"{mint[:4]}…{mint[-4:]}"
    return (f"<b>[solana]</b> кандидат {esc(symbol)} ({esc(short)})\n"
            f"vol5=${volume_m5:,.0f} move={move_m5_pct:.1f}% "
            f"liq=${liquidity_usd:,.0f}\n"
            f"px={price_usd:.8f} свап выкл")


def fmt_enter(*, symbol: str, px: float, size_sol: float) -> str:
    return (f"<b>[solana]</b> вход {esc(symbol)}\n"
            f"px={px:.8f} size={size_sol:.4f} SOL")


def fmt_exit(*, symbol: str, entry: float, exit_px: float, reason: str) -> str:
    pnl = ((exit_px / entry) - 1.0) * 100.0 if entry else 0.0
    return (f"<b>[solana]</b> выход {esc(reason)} {esc(symbol)}\n"
            f"{entry:.8f} → {exit_px:.8f} ({pnl:+.1f}%)")


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
