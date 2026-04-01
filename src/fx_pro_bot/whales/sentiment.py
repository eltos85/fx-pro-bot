"""Myfxbook Community Outlook — настроения ретейл-трейдеров.

Используется как контрариан-индикатор: если 70%+ ретейла в одну сторону,
крупные игроки скорее всего в противоположную.

Требует бесплатную регистрацию на myfxbook.com (100 запросов/день).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from fx_pro_bot.analysis.signals import TrendDirection

log = logging.getLogger(__name__)

API_BASE = "https://www.myfxbook.com/api"
CONTRARIAN_THRESHOLD = 70.0

MYFXBOOK_TO_SYMBOL: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "EURGBP": "EURGBP=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
}


@dataclass(frozen=True, slots=True)
class SentimentSignal:
    symbol: str
    direction: TrendDirection
    retail_long_pct: float
    retail_short_pct: float
    total_positions: int


class MyfxbookClient:
    """Клиент Myfxbook API с авторизацией по email/password."""

    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._session_id: str | None = None

    def _login(self) -> str | None:
        try:
            resp = requests.get(
                f"{API_BASE}/login.json",
                params={"email": self._email, "password": self._password},
                timeout=15,
            )
            data = resp.json()
            if data.get("error"):
                log.warning("Myfxbook login error: %s", data.get("message"))
                return None
            self._session_id = data.get("session")
            return self._session_id
        except Exception:
            log.warning("Myfxbook: ошибка авторизации")
            return None

    def _ensure_session(self) -> str | None:
        if self._session_id:
            return self._session_id
        return self._login()

    def get_community_outlook(self) -> list[dict]:
        """Получить sentiment-данные. Возвращает список символов с % long/short."""
        session = self._ensure_session()
        if not session:
            return []

        try:
            resp = requests.get(
                f"{API_BASE}/get-community-outlook.json",
                params={"session": session},
                timeout=15,
            )
            data = resp.json()

            if data.get("error"):
                log.warning("Myfxbook outlook error: %s", data.get("message"))
                self._session_id = None
                return []

            return data.get("symbols", [])
        except Exception:
            log.warning("Myfxbook: ошибка получения sentiment")
            return []


def fetch_sentiment_signals(
    email: str = "",
    password: str = "",
) -> list[SentimentSignal]:
    """Получить sentiment-сигналы от Myfxbook с контрариан-логикой."""
    if not email or not password:
        log.debug("Myfxbook: email/password не настроены — sentiment недоступен")
        return []

    client = MyfxbookClient(email, password)
    symbols = client.get_community_outlook()
    if not symbols:
        return []

    signals: list[SentimentSignal] = []
    for sym in symbols:
        name = str(sym.get("name", "")).upper()
        our_symbol = MYFXBOOK_TO_SYMBOL.get(name)
        if our_symbol is None:
            continue

        try:
            long_pct = float(sym.get("longPercentage", 50))
            short_pct = float(sym.get("shortPercentage", 50))
            total_pos = int(sym.get("totalPositions", 0))
        except (ValueError, TypeError):
            continue

        if long_pct >= CONTRARIAN_THRESHOLD:
            direction = TrendDirection.SHORT
        elif short_pct >= CONTRARIAN_THRESHOLD:
            direction = TrendDirection.LONG
        else:
            direction = TrendDirection.FLAT

        signals.append(SentimentSignal(
            symbol=our_symbol,
            direction=direction,
            retail_long_pct=round(long_pct, 1),
            retail_short_pct=round(short_pct, 1),
            total_positions=total_pos,
        ))

    log.info("Sentiment: %d сигналов (%d активных)",
             len(signals), sum(1 for s in signals if s.direction != TrendDirection.FLAT))
    return signals
