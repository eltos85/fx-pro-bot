"""Тонкий REST-клиент Tinkoff Invest API v1.

Официальный Python SDK (tinkoff-investments) недоступен в части окружений pip;
унарные методы доступны через REST с тем же Bearer-токеном.
https://tinkoff.github.io/investAPI/swagger-ui/
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("ru_stocks.tinkoff")


class TinkoffInvestError(RuntimeError):
    pass


def quotation_to_float(q: dict[str, Any] | None) -> float:
    """MoneyValue / Quotation → float."""
    if not q:
        return 0.0
    units = int(q.get("units", 0) or 0)
    nano = int(q.get("nano", 0) or 0)
    return units + nano / 1_000_000_000


class TinkoffRestClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise TinkoffInvestError("RU_STOCKS_TINKOFF_TOKEN не задан")
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def post(self, service_method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """service_method: ``UsersService/GetAccounts`` (без префикса tinkoff...)."""
        path = (
            f"/tinkoff.public.invest.api.contract.v1.{service_method}"
        )
        url = f"{self._base}{path}"
        payload = body if body is not None else {}
        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as e:
            raise TinkoffInvestError(f"HTTP ошибка {service_method}: {e}") from e
        if resp.status_code != 200:
            raise TinkoffInvestError(
                f"{service_method} HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        return data

    def get_accounts(self) -> list[dict[str, Any]]:
        data = self.post("UsersService/GetAccounts")
        return list(data.get("accounts") or [])

    def get_portfolio(self, account_id: str) -> dict[str, Any]:
        return self.post(
            "OperationsService/GetPortfolio",
            {"accountId": account_id},
        )

    def get_shares(self) -> list[dict[str, Any]]:
        data = self.post(
            "InstrumentsService/Shares",
            {"instrumentStatus": "INSTRUMENT_STATUS_BASE"},
        )
        return list(data.get("instruments") or [])

    def get_last_prices(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        if not instrument_ids:
            return []
        data = self.post(
            "MarketDataService/GetLastPrices",
            {"instrumentId": instrument_ids},
        )
        return list(data.get("lastPrices") or [])

    def get_candles(
        self,
        instrument_id: str,
        *,
        from_iso: str,
        to_iso: str,
        interval: str = "CANDLE_INTERVAL_DAY",
    ) -> list[dict[str, Any]]:
        data = self.post(
            "MarketDataService/GetCandles",
            {
                "instrumentId": instrument_id,
                "from": from_iso,
                "to": to_iso,
                "interval": interval,
            },
        )
        return list(data.get("candles") or [])
