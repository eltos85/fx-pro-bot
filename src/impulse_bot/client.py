"""Bybit REST для impulse-bot.

Офдок:
  recent-trade https://bybit-exchange.github.io/docs/v5/market/recent-trade
  tickers     https://bybit-exchange.github.io/docs/v5/market/tickers
  create-order https://bybit-exchange.github.io/docs/v5/order/create-order
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from pybit.unified_trading import HTTP

from impulse_bot.signals import Cluster, Tape, cluster_from_prints, tape_from_prints

log = logging.getLogger("impulse_bot.client")


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
    # Market: maxMktOrderQty, не maxOrderQty.
    # https://bybit-exchange.github.io/docs/v5/market/instrument
    max_mkt_order_qty: float
    min_notional: float


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float


@dataclass
class ClosedTrade:
    """Факт закрытия с биржи: средние цены исполнения и net PnL."""

    entry_price: float
    exit_price: float
    pnl: float
    updated_ts: int


@dataclass
class Ticker:
    symbol: str
    last: float
    turnover24h: float


class ImpulseClient:
    def __init__(self, api_key: str, api_secret: str, *, demo: bool,
                 category: str = "linear") -> None:
        self._session = HTTP(api_key=api_key, api_secret=api_secret,
                             demo=demo, recv_window=10000)
        self._category = category
        self._instr: dict[str, InstrumentInfo] = {}

    def tickers(self) -> list[Ticker]:
        try:
            resp = self._session.get_tickers(category=self._category)
        except Exception:
            log.exception("get_tickers")
            return []
        out = []
        for r in resp.get("result", {}).get("list") or []:
            try:
                out.append(Ticker(
                    symbol=r.get("symbol", ""),
                    last=float(r.get("lastPrice") or 0),
                    turnover24h=float(r.get("turnover24h") or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

    def tape_and_cluster(self, symbol: str, side: str, *,
                         window_sec: int) -> tuple[Tape, Cluster]:
        try:
            resp = self._session.get_public_trade_history(
                category=self._category, symbol=symbol, limit=200)
        except Exception:
            log.exception("recent-trade %s", symbol)
            return Tape(0.0, 0.0), Cluster(0.0)
        now_ms = int(time.time() * 1000)
        cut = now_ms - window_sec * 1000
        sides: list[tuple[str, float]] = []
        prices: list[tuple[float, float]] = []
        for r in resp.get("result", {}).get("list") or []:
            try:
                ts = int(r.get("time") or 0)
                if ts < cut:
                    continue
                px = float(r.get("price") or 0)
                sz = float(r.get("size") or 0)
                usd = px * sz
                sides.append((r.get("side") or "", usd))
                prices.append((px, usd))
            except (TypeError, ValueError):
                continue
        return tape_from_prints(sides), cluster_from_prints(prices, side)

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
            max_mkt_order_qty=float(lf.get("maxMktOrderQty") or 0),
            min_notional=float(lf.get("minNotionalValue") or 0),
        )
        self._instr[symbol] = info
        return info

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

    def wallet_equity(self) -> float:
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
        except Exception:
            log.exception("get_wallet_balance")
            return 0.0
        for acc in resp.get("result", {}).get("list") or []:
            for coin in acc.get("coin") or []:
                if coin.get("coin") == "USDT":
                    return float(coin.get("equity") or 0)
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

    def last_closed_trade(self, symbol: str, *,
                          not_before_ts: int = 0) -> ClosedTrade | None:
        """Последняя закрытая сделка по символу с фактическими ценами.

        `closedPnl` у Bybit уже net — с комиссиями и фандингом, поэтому это
        источник правды по результату, в отличие от расчёта (exit-entry)*qty.
        https://bybit-exchange.github.io/docs/v5/position/close-pnl

        `not_before_ts` отсекает чужую или устаревшую запись: на общем демо
        по тому же символу мог торговать другой бот, и его закрытие нам
        приписывать нельзя.
        """
        try:
            resp = self._session.get_closed_pnl(
                category=self._category, symbol=symbol, limit=1)
        except Exception:
            log.exception("get_closed_pnl %s", symbol)
            return None
        if resp.get("retCode") not in (0, None):
            return None
        items = (resp.get("result", {}) or {}).get("list") or []
        if not items:
            return None
        row = items[0]
        try:
            updated = int(row.get("updatedTime") or 0) // 1000
            entry = float(row.get("avgEntryPrice") or 0)
            exit_px = float(row.get("avgExitPrice") or 0)
            pnl = float(row.get("closedPnl") or 0)
        except (TypeError, ValueError):
            return None
        if entry <= 0 or exit_px <= 0:
            return None
        if not_before_ts and updated < not_before_ts:
            log.info("%s closed_pnl старше входа — не наша запись", symbol)
            return None
        return ClosedTrade(entry_price=entry, exit_price=exit_px, pnl=pnl,
                           updated_ts=updated)

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self._session.set_leverage(
                category=self._category, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            if "110043" not in str(e) and "not modified" not in str(e).lower():
                log.warning("set_leverage %s: %s", symbol, e)

    def fmt_qty(self, symbol: str, qty: float) -> str:
        info = self.instrument(symbol)
        step = info.qty_step if info else 0.0
        if step and step > 0:
            qty = math.floor(qty / step) * step
            return f"{qty:.{_qty_decimals(step)}f}"
        return repr(qty)

    def fmt_px(self, symbol: str, px: float) -> str:
        info = self.instrument(symbol)
        tick = info.tick_size if info else 0.0
        if tick and tick > 0:
            px = math.floor(px / tick) * tick
            d = _qty_decimals(tick)
            return f"{px:.{d}f}"
        return f"{px:.6f}"

    def market(self, *, symbol: str, side: str, qty: float,
               order_link_id: str, reduce_only: bool = False,
               sl: float | None = None, tp: float | None = None) -> dict:
        # CScalp: «рынок» = лимитка в край стакана.
        # https://fsr-develop.ru/vidy-zajavok-na-bybit
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
        if sl and tp:
            params["stopLoss"] = self.fmt_px(symbol, sl)
            params["takeProfit"] = self.fmt_px(symbol, tp)
            params["tpslMode"] = "Full"
            params["slOrderType"] = "Market"
            params["tpOrderType"] = "Market"
        try:
            resp = self._session.place_order(**params)
        except Exception as e:
            log.exception("place_order %s", symbol)
            return {"ok": False, "error": str(e)}
        if resp.get("retCode") not in (0, None):
            return {"ok": False, "error": resp.get("retMsg", "")}
        return {"ok": True, "result": resp.get("result")}
