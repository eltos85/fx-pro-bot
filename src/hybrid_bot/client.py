"""Минимальный Bybit REST для hybrid_bot.

Офдок: https://bybit-exchange.github.io/docs/v5/order/create-order
kline:  https://bybit-exchange.github.io/docs/v5/market/kline

Метода «поставить стоп/тейк на позицию» здесь нет намеренно: такой ордер
относится ко ВСЕМУ лоту на счёте (tpslMode=Full) и закрывает объём других
ботов — ровно та путаница, из разбора которой выросла стратегия
(STRATEGY_HYBRID.md §17.1). Выход только рыночным reduce-only на свой объём.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger("hybrid_bot.client")


def _qty_decimals(step: float) -> int:
    if step <= 0:
        return 8
    d = f"{step:.10f}".rstrip("0")
    return len(d.split(".")[1]) if "." in d else 0


@dataclass
class InstrumentInfo:
    qty_step: float
    min_order_qty: float
    tick_size: float


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float


class HybridClient:
    def __init__(self, api_key: str, api_secret: str, *, demo: bool,
                 category: str = "linear") -> None:
        self._session = HTTP(api_key=api_key, api_secret=api_secret,
                             demo=demo, recv_window=10000)
        self._category = category
        self._instr: dict[str, InstrumentInfo] = {}

    def instrument(self, symbol: str) -> InstrumentInfo | None:
        if symbol in self._instr:
            return self._instr[symbol]
        try:
            resp = self._session.get_instruments_info(
                category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_instruments_info %s", symbol)
            return None
        items = resp.get("result", {}).get("list") or []
        if not items:
            return None
        lf = items[0].get("lotSizeFilter") or {}
        pf = items[0].get("priceFilter") or {}
        info = InstrumentInfo(
            qty_step=float(lf.get("qtyStep", "0.001")),
            min_order_qty=float(lf.get("minOrderQty", "0")),
            tick_size=float(pf.get("tickSize", "0.01")),
        )
        self._instr[symbol] = info
        return info

    def closed_klines(self, symbol: str, interval: str,
                      limit: int = 250) -> list[tuple[int, float]]:
        """Закрытые бары, старые→новые: (start_ms, close)."""
        try:
            resp = self._session.get_kline(
                category=self._category, symbol=symbol,
                interval=interval, limit=limit)
        except Exception:
            log.exception("get_kline %s %s", symbol, interval)
            return []
        rows = resp.get("result", {}).get("list") or []
        parsed = [(int(r[0]), float(r[4])) for r in rows]
        parsed.sort()
        if len(parsed) < 3:
            return parsed
        # Последний бар ещё формируется — отрезаем.
        return parsed[:-1]

    def last_price(self, symbol: str) -> float:
        try:
            resp = self._session.get_tickers(
                category=self._category, symbol=symbol)
            items = resp.get("result", {}).get("list") or []
            if items:
                return float(items[0].get("lastPrice") or 0)
        except Exception:
            log.exception("get_tickers %s", symbol)
        return 0.0

    def get_position(self, symbol: str) -> Position | None:
        try:
            resp = self._session.get_positions(
                category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_positions %s", symbol)
            return None
        if resp.get("retCode") not in (0, None):
            return None
        for p in resp.get("result", {}).get("list") or []:
            size = float(p.get("size") or 0)
            return Position(
                symbol=symbol,
                side=p.get("side", "") if size > 0 else "",
                size=size,
                entry_price=float(p.get("avgPrice") or 0),
            )
        return Position(symbol=symbol, side="", size=0.0, entry_price=0.0)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._session.set_leverage(
                category=self._category, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            # 110043 = leverage not modified, это не ошибка.
            if "110043" not in str(e) and "not modified" not in str(e).lower():
                log.warning("set_leverage %s: %s", symbol, e)

    def fmt_qty(self, symbol: str, qty: float) -> str:
        info = self.instrument(symbol)
        step = info.qty_step if info else 0.0
        if step and step > 0:
            qty = math.floor(qty / step) * step
            return f"{qty:.{_qty_decimals(step)}f}"
        return repr(qty)

    def market(self, *, symbol: str, side: str, qty: float,
               order_link_id: str, reduce_only: bool = False) -> dict:
        params = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": self.fmt_qty(symbol, qty),
            "orderLinkId": order_link_id,
        }
        if reduce_only:
            params["reduceOnly"] = True
        try:
            resp = self._session.place_order(**params)
        except Exception as e:
            log.exception("place_order %s", symbol)
            return {"ok": False, "error": str(e)}
        if resp.get("retCode") not in (0, None):
            return {"ok": False, "error": resp.get("retMsg", "")}
        return {"ok": True, "result": resp.get("result")}
