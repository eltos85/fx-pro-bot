"""Bybit REST-клиент flowzone_bot (обёртка над pybit HTTP).

Изолирован от других ботов. Под канон flowzone:
- LIMIT вход в зоне (канон: «put a limit order here» после подтверждения),
- reduce-only MARKET выход (надёжное закрытие),
- округление qty/price под lot/tick фильтры (иначе 10001 «invalid»).

API: https://bybit-exchange.github.io/docs/v5/order/create-order
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger("flowzone_bot.client")


def _as_float(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _qty_decimals(step: float) -> int:
    """Число знаков после запятой в шаге лота."""
    if step <= 0:
        return 8
    d = f"{step:.10f}".rstrip("0")
    return len(d.split(".")[1]) if "." in d else 0


@dataclass
class InstrumentInfo:
    symbol: str
    qty_step: float
    min_order_qty: float
    tick_size: float


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealised_pnl: float
    mark_price: float


class FlowzoneBybitClient:
    def __init__(self, api_key: str, api_secret: str, *, demo: bool = True,
                 category: str = "linear") -> None:
        self._session = HTTP(api_key=api_key, api_secret=api_secret,
                             demo=demo, recv_window=10000)
        self._category = category
        self._instr: dict[str, InstrumentInfo] = {}

    # ─── instruments ─────────────────────────────────────────────────────

    def instrument(self, symbol: str) -> InstrumentInfo | None:
        if symbol in self._instr:
            return self._instr[symbol]
        try:
            resp = self._session.get_instruments_info(
                category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_instruments_info %s failed", symbol)
            return None
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            return None
        it = items[0]
        lf = it.get("lotSizeFilter", {}) or {}
        pf = it.get("priceFilter", {}) or {}
        try:
            info = InstrumentInfo(
                symbol=symbol,
                qty_step=float(lf.get("qtyStep", "0.001")),
                min_order_qty=float(lf.get("minOrderQty", "0")),
                tick_size=float(pf.get("tickSize", "0.01")),
            )
        except (ValueError, TypeError):
            return None
        self._instr[symbol] = info
        return info

    def get_kline(self, symbol: str, interval: str, limit: int = 200) -> list[list]:
        """Свечи (для свинг-структуры/сессионных границ/universe RVOL). list DESC
        (новые сверху), элемент: [startTime, open, high, low, close, volume,
        turnover]. Офдок: https://bybit-exchange.github.io/docs/v5/market/kline"""
        try:
            resp = self._session.get_kline(
                category=self._category, symbol=symbol,
                interval=interval, limit=limit)
        except Exception:
            log.exception("get_kline %s %s failed", symbol, interval)
            return []
        return resp.get("result", {}).get("list", []) or []

    def get_tickers(self) -> list[dict]:
        """24h-снапшот по всем инструментам категории (для авто-селектора).
        Офдок: https://bybit-exchange.github.io/docs/v5/market/tickers"""
        try:
            resp = self._session.get_tickers(category=self._category)
        except Exception:
            log.exception("get_tickers failed")
            return []
        return resp.get("result", {}).get("list", []) or []

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self.instrument(symbol)
        step = info.qty_step if info else 0.001
        if step <= 0:
            return qty
        return round(math.floor(qty / step) * step, _qty_decimals(step))

    def fmt_qty(self, symbol: str, qty: float) -> str:
        """qty → строка ровно по точности шага лота (иначе Bybit 10001)."""
        info = self.instrument(symbol)
        step = info.qty_step if info else 0.0
        if step and step > 0:
            qty = math.floor(qty / step) * step
            return f"{qty:.{_qty_decimals(step)}f}"
        return repr(qty)

    def round_price(self, symbol: str, price: float) -> float:
        info = self.instrument(symbol)
        tick = info.tick_size if info else 0.01
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 10)

    # ─── account ─────────────────────────────────────────────────────────

    def wallet_equity(self) -> float:
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
        except Exception:
            log.exception("get_wallet_balance failed")
            return 0.0
        for acc in resp.get("result", {}).get("list", []) or []:
            for coin in acc.get("coin", []) or []:
                if coin.get("coin") == "USDT":
                    try:
                        return float(coin.get("equity", 0) or 0)
                    except (ValueError, TypeError):
                        return 0.0
        return 0.0

    def get_position(self, symbol: str) -> Position | None:
        """None = запрос не удался; Position(size=0) = позиции нет."""
        try:
            resp = self._session.get_positions(
                category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_positions %s failed", symbol)
            return None
        if resp.get("retCode") not in (0, None):
            log.warning("get_positions retCode=%s msg=%s",
                        resp.get("retCode"), resp.get("retMsg"))
            return None
        items = resp.get("result", {}).get("list", []) or []
        for p in items:
            try:
                size = float(p.get("size", 0) or 0)
                return Position(
                    symbol=symbol,
                    side=p.get("side", "") if size > 0 else "",
                    size=size,
                    entry_price=float(p.get("avgPrice", 0) or 0),
                    unrealised_pnl=float(p.get("unrealisedPnl", 0) or 0),
                    mark_price=float(p.get("markPrice", 0) or 0),
                )
            except (ValueError, TypeError):
                continue
        return Position(symbol=symbol, side="", size=0.0,
                        entry_price=0.0, unrealised_pnl=0.0, mark_price=0.0)

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._session.set_leverage(
                category=self._category, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage))
            return True
        except Exception as e:
            msg = str(e).lower()
            if "not modified" in msg or "110043" in msg:
                return True
            log.warning("set_leverage %s %dx failed: %s", symbol, leverage, e)
            return False

    # ─── orders ──────────────────────────────────────────────────────────

    def place_entry(self, *, symbol: str, side: str, qty: float,
                    order_link_id: str, order_type: str,
                    limit_price: float | None = None,
                    sl_price: float | None = None,
                    tp_price: float | None = None) -> dict:
        """Вход. order_type: 'limit' (канон — лимитка в зоне) | 'market'."""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "qty": self.fmt_qty(symbol, qty),
            "orderLinkId": order_link_id,
        }
        if order_type in ("limit", "post_only_limit"):
            if limit_price is None:
                return {"ok": False, "error": "limit_price required for limit"}
            params["orderType"] = "Limit"
            params["price"] = str(limit_price)
            if order_type == "post_only_limit":
                params["timeInForce"] = "PostOnly"
        else:
            params["orderType"] = "Market"
        if sl_price is not None:
            params["stopLoss"] = str(sl_price)
        if tp_price is not None:
            params["takeProfit"] = str(tp_price)
        return self._submit(params)

    def cancel_order(self, symbol: str, order_link_id: str) -> dict:
        try:
            resp = self._session.cancel_order(
                category=self._category, symbol=symbol,
                orderLinkId=order_link_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": resp.get("retCode") in (0, None), "raw": resp}

    def order_status(self, symbol: str, order_link_id: str) -> str | None:
        """orderStatus: New/PartiallyFilled/Filled/Cancelled/Rejected/..."""
        try:
            resp = self._session.get_open_orders(
                category=self._category, symbol=symbol,
                orderLinkId=order_link_id)
            items = resp.get("result", {}).get("list", []) or []
            if items:
                return items[0].get("orderStatus")
            resp2 = self._session.get_order_history(
                category=self._category, symbol=symbol,
                orderLinkId=order_link_id, limit=1)
            items2 = resp2.get("result", {}).get("list", []) or []
            if items2:
                return items2[0].get("orderStatus")
        except Exception:
            log.exception("order_status %s failed", symbol)
        return None

    def set_trading_stop(self, symbol: str, *, sl_price: float | None = None,
                         tp_price: float | None = None) -> dict:
        """Переставить биржевые SL/TP открытой позиции (Full-mode, one-way).
        Офдок: https://bybit-exchange.github.io/docs/v5/position/trading-stop"""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": 0,
        }
        if sl_price is not None:
            params["stopLoss"] = str(sl_price)
        if tp_price is not None:
            params["takeProfit"] = str(tp_price)
        try:
            resp = self._session.set_trading_stop(**params)
        except Exception as e:
            log.exception("set_trading_stop %s failed: %s", symbol, params)
            return {"ok": False, "error": f"exception: {e}", "params": params}
        ret_code = resp.get("retCode")
        if ret_code not in (0, None, 34040):  # 34040 = not modified (идемпотентно)
            log.warning("set_trading_stop retCode=%s msg=%s params=%s",
                        ret_code, resp.get("retMsg"), params)
            return {"ok": False,
                    "error": f"retCode={ret_code} {resp.get('retMsg')}",
                    "params": params, "raw": resp}
        return {"ok": True, "raw": resp}

    def close_market(self, symbol: str, side: str, qty: float,
                     order_link_id: str) -> dict:
        opposite = "Sell" if side == "Buy" else "Buy"
        params = {
            "category": self._category,
            "symbol": symbol,
            "side": opposite,
            "orderType": "Market",
            "qty": self.fmt_qty(symbol, qty),
            "orderLinkId": order_link_id,
            "reduceOnly": True,
        }
        return self._submit(params)

    def closed_pnl_detail(self, symbol: str, *, order_id: str | None = None,
                          qty: float | None = None, since_ms: int | None = None,
                          near_ms: int | None = None,
                          until_ms: int | None = None,
                          entry_price: float | None = None,
                          entry_tol: float = 1e-5,
                          max_pages: int = 10) -> dict | None:
        """Запись о закрытии нашей сделки: {pnl, exit, order_id, created}.
        ``closedPnl`` уже net. Матч по отпечатку (entry_price+qty приоритет, иначе
        orderId, иначе qty+ближайший createdTime). Неоднозначность → None (не
        выдумываем — порча статы хуже пропуска).
        Источник: https://bybit-exchange.github.io/docs/v5/position/close-pnl"""
        base: dict = {"category": self._category, "symbol": symbol, "limit": 100}
        if since_ms is not None:
            base["startTime"] = int(since_ms - 5000)
        if until_ms is not None:
            base["endTime"] = int(until_ms)
        if "startTime" in base and "endTime" in base:
            min_start = base["endTime"] - (7 * 24 * 3600 - 3600) * 1000
            base["startTime"] = max(base["startTime"], min_start)
        items: list = []
        cursor = None
        for _ in range(max(1, max_pages)):
            params = dict(base)
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self._session.get_closed_pnl(**params)
            except Exception:
                log.exception("get_closed_pnl %s failed", symbol)
                return None
            lst = resp.get("result", {}).get("list", []) or []
            items += lst
            cursor = resp.get("result", {}).get("nextPageCursor")
            if not cursor or not lst:
                break
        if not items:
            return None
        chosen = None
        if order_id:
            chosen = next((it for it in items
                           if str(it.get("orderId", "")) == order_id), None)
        if chosen is None and entry_price and entry_price > 0 and qty and qty > 0:
            tol_q = max(qty * 0.02, 1e-9)
            cands = []
            for it in items:
                cs = _as_float(it.get("closedSize"))
                ep = _as_float(it.get("avgEntryPrice"))
                if cs is None or ep is None or abs(cs - qty) > tol_q:
                    continue
                if abs(ep - entry_price) / entry_price <= entry_tol:
                    cands.append(it)
            if len(cands) == 1:
                chosen = cands[0]
            elif len(cands) > 1:
                log.warning("closed_pnl %s: неоднозначность entry=%s qty=%s "
                            "(%d кандидатов) — НЕ атрибутирую",
                            symbol, entry_price, qty, len(cands))
                return None
        if chosen is None and entry_price is None and qty and qty > 0:
            tol = max(qty * 0.02, 1e-9)
            cands = [it for it in items
                     if (cs := _as_float(it.get("closedSize"))) is not None
                     and abs(cs - qty) <= tol]
            if cands:
                if near_ms is not None:
                    chosen = min(cands, key=lambda it: abs(
                        (_as_float(it.get("createdTime")) or 0) - near_ms))
                else:
                    chosen = cands[0]
        if chosen is None:
            log.warning("closed_pnl %s: нет совпадения (order_id=%s qty=%s "
                        "entry=%s)", symbol, order_id, qty, entry_price)
            return None
        return {
            "pnl": _as_float(chosen.get("closedPnl")),
            "exit": _as_float(chosen.get("avgExitPrice")),
            "order_id": str(chosen.get("orderId", "")),
            "created": _as_float(chosen.get("createdTime")),
        }

    def closed_pnl_position(self, symbol: str, *, qty: float,
                            since_ms: int, until_ms: int,
                            entry_price: float | None = None,
                            entry_tol: float = 1e-5,
                            qty_tol_frac: float = 0.05,
                            max_pages: int = 10) -> dict | None:
        """Суммарный net по ВСЕЙ позиции (частичные закрытия + остаток).

        Bybit пишет отдельную ``closedPnl``-запись на каждое частичное закрытие
        позиции: один и тот же ``avgEntryPrice``, но разные ``closedSize``.
        Точечный матч ``closed_pnl_detail`` по ``closedSize ≈ qty`` такие сделки
        не ловит (ни одна запись не равна полному объёму) → сделка зависает
        provisional с завышенной оценкой. Здесь собираем ВСЕ записи символа в
        окне ``[since_ms, until_ms]``, фильтруем по ``avgEntryPrice`` (если задан)
        и суммируем ``closedPnl`` ТОЛЬКО если ``Σ closedSize ≈ qty`` — т.е. вся
        позиция собрана. Иначе ``None`` (не выдумываем — `no-data-fitting`).

        Окно изолирует записи именно этой позиции: правило «один сетап на
        символ» + cooldown не дают двум flowzone-сделкам по символу пересечься
        во времени. ``closedPnl`` уже net (комиссии + funding).
        Источник: https://bybit-exchange.github.io/docs/v5/position/close-pnl"""
        if qty <= 0:
            return None
        base: dict = {"category": self._category, "symbol": symbol, "limit": 100,
                      "startTime": int(since_ms), "endTime": int(until_ms)}
        min_start = base["endTime"] - (7 * 24 * 3600 - 3600) * 1000
        base["startTime"] = max(base["startTime"], min_start)
        items: list = []
        cursor = None
        for _ in range(max(1, max_pages)):
            params = dict(base)
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self._session.get_closed_pnl(**params)
            except Exception:
                log.exception("get_closed_pnl(position) %s failed", symbol)
                return None
            lst = resp.get("result", {}).get("list", []) or []
            items += lst
            cursor = resp.get("result", {}).get("nextPageCursor")
            if not cursor or not lst:
                break
        sum_pnl = 0.0
        sum_qty = 0.0
        sum_exit_val = 0.0
        n = 0
        for it in items:
            # только торговые закрытия: funding/settlement (Settle,
            # SessionSettlePnL, MovePosition) не входят в realized сделки и не
            # должны искажать матч по объёму. Офдок closed-pnl: поле execType.
            if str(it.get("execType", "Trade")) not in ("Trade", "BustTrade"):
                continue
            cs = _as_float(it.get("closedSize"))
            if cs is None or cs <= 0:
                continue
            if entry_price and entry_price > 0:
                ep = _as_float(it.get("avgEntryPrice"))
                if ep is None or abs(ep - entry_price) / entry_price > entry_tol:
                    continue
            pnl = _as_float(it.get("closedPnl"))
            if pnl is None:
                continue
            ex = _as_float(it.get("avgExitPrice")) or 0.0
            sum_pnl += pnl
            sum_qty += cs
            sum_exit_val += ex * cs
            n += 1
        if n == 0 or abs(sum_qty - qty) > max(qty * qty_tol_frac, 1e-9):
            return None
        return {
            "pnl": sum_pnl,
            "exit": (sum_exit_val / sum_qty) if sum_qty > 0 else None,
            "count": n,
        }

    def closed_pnl(self, symbol: str, *, order_id: str | None = None,
                   qty: float | None = None, since_ms: int | None = None,
                   near_ms: int | None = None, until_ms: int | None = None,
                   entry_price: float | None = None) -> float | None:
        """net closedPnl нашей сделки (тонкая обёртка над closed_pnl_detail)."""
        d = self.closed_pnl_detail(symbol, order_id=order_id, qty=qty,
                                   since_ms=since_ms, near_ms=near_ms,
                                   until_ms=until_ms, entry_price=entry_price)
        return d["pnl"] if d else None

    def _submit(self, params: dict) -> dict:
        try:
            resp = self._session.place_order(**params)
        except Exception as e:
            log.exception("place_order exception: %s", params)
            return {"ok": False, "error": f"exception: {e}", "params": params}
        ret_code = resp.get("retCode")
        if ret_code not in (0, None):
            log.warning("place_order retCode=%s msg=%s params=%s",
                        ret_code, resp.get("retMsg"), params)
            return {"ok": False,
                    "error": f"retCode={ret_code} {resp.get('retMsg')}",
                    "params": params, "raw": resp}
        return {"ok": True, "result": resp.get("result"), "raw": resp}
