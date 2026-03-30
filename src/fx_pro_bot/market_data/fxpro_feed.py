"""Получение котировок через FxPro cTrader Open API (OAuth 2.0 + REST).

Если FXPRO_ENABLED=false (по умолчанию), модуль не используется —
бот работает через yfinance. Включите после получения API-ключей
от FxPro (нужен верифицированный аккаунт).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import requests

from fx_pro_bot.market_data.models import Bar, InstrumentId

log = logging.getLogger(__name__)

YAHOO_TO_FXPRO: dict[str, str] = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD",
    "EURGBP=X": "EURGBP",
    "GC=F": "XAUUSD",
    "SI=F": "XAGUSD",
    "CL=F": "USOUSD",
    "BZ=F": "UKOUSD",
}


class FxProClient:
    """Клиент FxPro REST API с OAuth 2.0 авторизацией."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        account_id: str,
        api_url: str = "https://connect.fxpro.com/api/v1",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_id = account_id
        self._api_url = api_url.rstrip("/")
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._session = requests.Session()

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._token_expires - 60:
            return
        resp = self._session.post(
            f"{self._api_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)
        log.info("FxPro: токен обновлён")

    def _headers(self) -> dict[str, str]:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._api_url}{path}"
        resp = self._session.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_bars(
        self,
        yahoo_symbol: str,
        *,
        timeframe: str = "m5",
        count: int = 200,
    ) -> list[Bar]:
        """Загрузка баров для инструмента.

        timeframe: m1, m5, m15, m30, h1, h4, d1
        """
        fxpro_symbol = YAHOO_TO_FXPRO.get(yahoo_symbol, yahoo_symbol)
        data = self._get(
            f"/accounts/{self._account_id}/symbols/{fxpro_symbol}/bars",
            params={"timeframe": timeframe, "count": str(count)},
        )

        instrument = InstrumentId(symbol=yahoo_symbol, venue="fxpro")
        bars: list[Bar] = []
        for candle in data.get("bars", data.get("data", [])):
            ts = datetime.fromtimestamp(candle["timestamp"] / 1000, tz=UTC)
            bars.append(
                Bar(
                    instrument=instrument,
                    ts=ts,
                    open=float(candle["open"]),
                    high=float(candle["high"]),
                    low=float(candle["low"]),
                    close=float(candle["close"]),
                    volume=float(candle.get("volume", 0)),
                )
            )
        return bars

    def get_price(self, yahoo_symbol: str) -> float | None:
        """Текущая цена (mid) для инструмента."""
        fxpro_symbol = YAHOO_TO_FXPRO.get(yahoo_symbol, yahoo_symbol)
        try:
            data = self._get(f"/accounts/{self._account_id}/symbols/{fxpro_symbol}/quote")
            bid = float(data.get("bid", 0))
            ask = float(data.get("ask", 0))
            if bid and ask:
                return (bid + ask) / 2
        except Exception:
            log.warning("FxPro: не удалось получить цену %s", yahoo_symbol)
        return None
