"""Bybit read-only клиент tradecard: closedPnl (ground truth) + klines (MFE).

Только **чтение** (closedPnl/klines) — никаких ордеров (advisory §1/§9). За
образец взят read-only-скоуп scalp_bot/trading/client.py.

P&L ground truth = Bybit ``closedPnl`` net (уже с fees/funding — офдок
close-pnl: gross − openFee − closeFee). Тянем с **полной пагинацией**
(``while cursor:``) — без неё выводы по первой странице запрещены
(stats-collection.mdc). Окно одного запроса Bybit ≤ 7 дней → режем диапазон.

Источники (api-docs.mdc):
- https://bybit-exchange.github.io/docs/v5/position/close-pnl
- https://bybit-exchange.github.io/docs/v5/market/kline
"""
from __future__ import annotations

import logging
import time

from pybit.unified_trading import HTTP

log = logging.getLogger("tradecard_bybit.bybit")

# Окно одного запроса get_closed_pnl: endTime − startTime ≤ 7 дней.
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


class TradecardBybitReadOnly:
    """Read-only обёртка над pybit HTTP (closedPnl + klines)."""

    def __init__(self, api_key: str, api_secret: str, *, demo: bool = True,
                 category: str = "linear") -> None:
        self._session = HTTP(api_key=api_key, api_secret=api_secret,
                             demo=demo, recv_window=20000)
        self._category = category

    def fetch_closed_pnl(self, *, start_ms: int, end_ms: int,
                         symbol: str | None = None) -> list[dict]:
        """Все closedPnl-записи в [start_ms, end_ms] с ПОЛНОЙ пагинацией.

        Режем на ≤7-дневные окна (лимит API) и внутри каждого крутим
        ``while cursor:`` до конца. closedPnl уже net (stats-collection.mdc).
        """
        out: list[dict] = []
        cur_start = start_ms
        while cur_start < end_ms:
            cur_end = min(cur_start + SEVEN_DAYS_MS, end_ms)
            cursor = ""
            while True:
                params: dict = {"category": self._category,
                                "startTime": cur_start, "endTime": cur_end,
                                "limit": 200}
                if symbol:
                    params["symbol"] = symbol
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = self._session.get_closed_pnl(**params)
                except Exception:  # noqa: BLE001
                    log.exception("get_closed_pnl failed")
                    break
                if resp.get("retCode") not in (0, None):
                    log.warning("get_closed_pnl retCode=%s msg=%s",
                                resp.get("retCode"), resp.get("retMsg"))
                    break
                result = resp.get("result", {}) or {}
                rows = result.get("list", []) or []
                out += rows
                cursor = result.get("nextPageCursor") or ""
                if not cursor or not rows:
                    break
                time.sleep(0.05)
            cur_start = cur_end
            time.sleep(0.05)
        return out

    def get_kline(self, symbol: str, interval: str, *, start_ms: int,
                  end_ms: int, limit: int = 200) -> list[list]:
        """Свечи для post-exit MFE (детектор exit_left_money). list DESC,
        элемент: [startTime, open, high, low, close, volume, turnover]."""
        try:
            resp = self._session.get_kline(
                category=self._category, symbol=symbol, interval=interval,
                start=start_ms, end=end_ms, limit=limit)
        except Exception:  # noqa: BLE001
            log.exception("get_kline %s %s failed", symbol, interval)
            return []
        return resp.get("result", {}).get("list", []) or []
