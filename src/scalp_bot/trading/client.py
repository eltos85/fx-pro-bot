"""Bybit REST-клиент scalp_bot (обёртка над pybit HTTP).

Изолирован от ai_trader/bybit_bot. Минимум под скальп:
- post-only LIMIT вход (maker — дёшево, см. settings.entry_order_type),
- reduce-only MARKET выход (надёжное закрытие по тайм-стопу),
- округление qty/price под lot/tick фильтры (иначе 10001 «invalid»).

API: https://bybit-exchange.github.io/docs/v5/order/create-order
post-only (timeInForce=PostOnly) — мейкер-гарантия: если ордер пересечёт
спред, биржа его отменит, а не исполнит как taker.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger("scalp_bot.client")


def _as_float(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _fee_sum(item: dict) -> float | None:
    """Round-turn комиссия записи close-pnl: openFee + closeFee.

    Оба поля официальные и приходят строками; если ни одного нет — None
    (не подменяем нулём, иначе «комиссии не было» неотличимо от «не знаем»).
    https://bybit-exchange.github.io/docs/v5/position/close-pnl
    """
    parts = [_as_float(item.get(k)) for k in ("openFee", "closeFee")]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


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


class ScalpBybitClient:
    def __init__(self, api_key: str, api_secret: str, *, demo: bool = True,
                 category: str = "linear") -> None:
        self._session = HTTP(api_key=api_key, api_secret=api_secret,
                             demo=demo, recv_window=10000)
        self._category = category
        self._instr: dict[str, InstrumentInfo] = {}
        # Кэш множества stock-перпов (get_instruments_info с пагинацией — дорого).
        # symbolType меняется редко (листинги/делистинги), TTL 1ч достаточно.
        self._stock_syms: set[str] | None = None
        self._stock_syms_ts: float = 0.0

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
        """HTF-свечи для трендового фильтра (EMA200 1H). list DESC (новые сверху),
        элемент: [startTime, open, high, low, close, volume, turnover].
        Офдок: https://bybit-exchange.github.io/docs/v5/market/kline"""
        try:
            resp = self._session.get_kline(
                category=self._category, symbol=symbol,
                interval=interval, limit=limit)
        except Exception:
            log.exception("get_kline %s %s failed", symbol, interval)
            return []
        return resp.get("result", {}).get("list", []) or []

    def get_funding_interval(self, symbol: str) -> int | None:
        """fundingInterval символа в МИНУТАХ (480=8ч / 240=4ч / 60=1ч). Нужен,
        чтобы не открываться перед списанием по РЕАЛЬНОМУ графику символа (ALLO/
        LAB — 4ч, а не 8ч). Офдок (поле fundingInterval):
        https://bybit-exchange.github.io/docs/v5/market/instrument"""
        try:
            resp = self._session.get_instruments_info(
                category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_instruments_info(funding) %s failed", symbol)
            return None
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            return None
        fi = items[0].get("fundingInterval")
        try:
            return int(fi) if fi else None
        except (ValueError, TypeError):
            return None

    def get_tickers(self) -> list[dict]:
        """24h-снапшот по всем инструментам категории (для авто-селектора
        вселенной). Офдок: https://bybit-exchange.github.io/docs/v5/market/tickers
        Поля: lastPrice, highPrice24h, lowPrice24h, turnover24h, bid1/ask1Price."""
        try:
            resp = self._session.get_tickers(category=self._category)
        except Exception:
            log.exception("get_tickers failed")
            return []
        return resp.get("result", {}).get("list", []) or []

    _STOCK_TYPE_TTL_SEC = 3600.0

    # symbolType, недоступные скальпу. По обоим Bybit требует отдельного
    # Trading Terms (ErrCode 110126), которого demo-API принять не даёт:
    #   stock     — перпы на акции/ETF (SKHYNIXUSDT, SOXLUSDT, AAPLUSDT, ...)
    #   commodity — перпы на сырьё (CLUSDT, BZUSDT, XAUUSDT, XAGUSDT)
    # Плюс оба класса торгуются по сессиям реальных бирж (KRX/NYSE/NYMEX),
    # а не 24/7 крипто-флоу — это ломает скальп-логику (свипы/CVD/плотности).
    # Крипто-перпы приходят с symbolType=None либо "innovation" — их не трогаем.
    _NON_CRYPTO_SYMBOL_TYPES = frozenset({"stock", "commodity"})

    def non_crypto_type_symbols(self) -> set[str]:
        """Множество linear-символов, неторгуемых на demo (см.
        ``_NON_CRYPTO_SYMBOL_TYPES``).

        Пагинация обязательна: на Bybit >500 linear-символов, без cursor
        API вернёт первую страницу и символы после 500 будут пропущены
        (правило stats-collection.mdc: incomplete data → неверный вывод).
        Офдок (поле symbolType, cursor):
        https://bybit-exchange.github.io/docs/v5/market/instrument

        Кэшируется на ``_STOCK_TYPE_TTL_SEC`` (1ч): листинги редки, селектор
        крутится каждые ``universe_refresh_sec``. fail-open: при ошибке API
        возвращаем пустое множество (не блокируем вселенную)."""
        now = time.time()
        if (self._stock_syms is not None
                and now - self._stock_syms_ts < self._STOCK_TYPE_TTL_SEC):
            return self._stock_syms
        out: set[str] = set()
        cursor = ""
        try:
            while True:
                kw: dict = {"category": self._category, "limit": 1000}
                if cursor:
                    kw["cursor"] = cursor
                resp = self._session.get_instruments_info(**kw)
                res = resp.get("result", {}) or {}
                for it in res.get("list", []) or []:
                    stype = (it.get("symbolType") or "").lower()
                    if stype in self._NON_CRYPTO_SYMBOL_TYPES:
                        s = it.get("symbol") or ""
                        if s:
                            out.add(s)
                cursor = res.get("nextPageCursor") or ""
                if not cursor:
                    break
        except Exception:
            log.exception("get_instruments_info(non-crypto) failed")
            return out  # частичный/пустой — fail-open
        self._stock_syms = out
        self._stock_syms_ts = now
        return out

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self.instrument(symbol)
        step = info.qty_step if info else 0.001
        if step <= 0:
            return qty
        return round(math.floor(qty / step) * step, _qty_decimals(step))

    def fmt_qty(self, symbol: str, qty: float) -> str:
        """qty → строка ровно по точности шага лота (без float-мусора,
        иначе Bybit ErrCode 10001 «Qty invalid»)."""
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
                    tp_price: float | None = None,
                    tpsl_mode: str | None = None) -> dict:
        """Вход. order_type: 'post_only_limit' | 'market'.

        ``tpsl_mode='Partial'`` нужен, когда на символе уже висит ЧУЖОЙ лот
        (общий one-way счёт с другим ботом). Офдок place-order:
        https://bybit-exchange.github.io/docs/v5/order/create-order —
        «Partial: partial position tp/sl (as there is no size option, so it
        will create tp/sl orders with the qty you actually fill)». То есть
        брекеты привязываются к НАШЕМУ филлу, а не ко всей позиции символа.
        По умолчанию (None) Bybit создаёт Full-брекеты на весь лот — это
        корректно только когда мы на символе одни.
        """
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "qty": self.fmt_qty(symbol, qty),
            "orderLinkId": order_link_id,
        }
        if order_type == "post_only_limit":
            if limit_price is None:
                return {"ok": False, "error": "limit_price required for post_only_limit"}
            params["orderType"] = "Limit"
            params["price"] = str(limit_price)
            params["timeInForce"] = "PostOnly"
        else:
            params["orderType"] = "Market"
        if sl_price is not None:
            params["stopLoss"] = str(sl_price)
        if tp_price is not None:
            params["takeProfit"] = str(tp_price)
        if tpsl_mode is not None and (sl_price is not None
                                      or tp_price is not None):
            params["tpslMode"] = tpsl_mode
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
        """Переставить биржевые SL/TP открытой позиции (Full-mode: на весь
        размер; обе цены строками, positionIdx=0 — one-way mode).

        Нужен для P-3 (audit 2026-06-10, A-2): MARKET-вход наливается со
        слиппеджем, и брекеты, выставленные в place_entry от пре-филл
        референса, дают реальный $-риск ≠ расчётному. После реального VWAP
        входа executor сдвигает SL/TP на дельту слиппеджа этим вызовом.
        Офдок: https://bybit-exchange.github.io/docs/v5/position/trading-stop
        (POST /v5/position/trading-stop, tpslMode=Full модифицирует
        существующие TP/SL-ордера позиции)."""
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
            # 34040 «Not modified». Офдок ошибок: https://bybit-exchange.github.io/
            # docs/v5/error — «34040 | Not modified. Indicates you already set this
            # TP/SL value or you didn't pass a required parameter» — ДВА смысла.
            # Офдок эндпоинта: https://bybit-exchange.github.io/docs/v5/position/
            # trading-stop — required=true ТОЛЬКО category/symbol/tpslMode/
            # positionIdx; takeProfit/stopLoss — optional. Этот метод ВСЕГДА
            # отправляет все 4 required-поля (захардкожены выше) → ветка «не передан
            # обязательный параметр» структурно невозможна → 34040 здесь означает
            # ТОЛЬКО «TP/SL уже равны отправляемым» = идемпотентный no-op (защита
            # на позиции УЖЕ стоит). Считаем успехом, НЕ логируем как ERROR.
            # pybit выбрасывает InvalidRequestError(status_code=34040) вместо
            # возврата retCode (поведение pybit, не дока). Иначе be-lock
            # (manage_levels) и rebracket получали бы ok=False, не фиксировали
            # _be_locked и повторяли одинаковый запрос каждый тик → шум
            # traceback'ами (live 2026-06-29: #2743 BTCUSDT ~1 req/сек).
            if getattr(e, "status_code", None) == 34040 or "34040" in str(e):
                log.debug("set_trading_stop %s: 34040 not modified (no-op) "
                          "params=%s", symbol, params)
                return {"ok": True, "no_op": True, "params": params}
            log.exception("set_trading_stop %s failed: %s", symbol, params)
            return {"ok": False, "error": f"exception: {e}", "params": params}
        ret_code = resp.get("retCode")
        # 34040 "not modified" — цены уже такие; считаем успехом (идемпотентно)
        if ret_code not in (0, None, 34040):
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
        """Запись о закрытии ИМЕННО нашей сделки: {pnl, exit, order_id, created}.

        Bybit ``closedPnl`` уже net (= cumExitValue − cumEntryValue − openFee
        − closeFee, проверено по офдоку). Ответ get_closed_pnl НЕ содержит
        orderLinkId и не фильтруется по нашему id; ``orderId`` в записи — это id
        ЗАКРЫВАЮЩЕГО ордера (для биржевых TP/SL его генерит биржа, мы узнаём его
        только из realtime-WS, которого при рестарте не было). Поэтому для
        restart-сирот матчим по «отпечатку» сделки:

        • **entry_price + qty (приоритет)** — ``avgEntryPrice`` уникален у каждой
          сделки (проверено: наш entry == биржевой avgEntryPrice точь-в-точь).
          Жёсткий допуск ``entry_tol`` отсекает чужие сделки того же размера
          (видели коллизию на 0.004%). НЕОДНОЗНАЧНОСТЬ (>1 кандидата в допуске) →
          возвращаем None (не выдумываем — порча статы хуже пропуска).
        • orderId — если знаем (наш reduce-only close).
        • qty + ближайший createdTime — legacy-фолбэк, когда entry_price не задан.

        ``ts_close`` у сирот ВРЁТ (= момент обнаружения после рестарта, не реальное
        закрытие), поэтому окно берём широкое [since=ts_open, until=ts_close+w] с
        пагинацией; entry_price отбирает нужную запись независимо от времени.
        Bybit: endTime − startTime ≤ 7 дней.
        Источник: https://bybit-exchange.github.io/docs/v5/position/close-pnl
        """
        base: dict = {"category": self._category, "symbol": symbol, "limit": 100}
        if since_ms is not None:
            base["startTime"] = int(since_ms - 5000)
        if until_ms is not None:
            base["endTime"] = int(until_ms)
        # Bybit-лимит окна: endTime − startTime ≤ 7д. Подрезаем startTime.
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
        # 1) точный матч по orderId нашего reduce-only закрытия
        if order_id:
            chosen = next((it for it in items
                           if str(it.get("orderId", "")) == order_id), None)
        # 2) отпечаток: avgEntryPrice == наш entry (+ closedSize ≈ qty)
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
                            "(%d кандидатов в допуске) — НЕ атрибутирую",
                            symbol, entry_price, qty, len(cands))
                return None
        # 3) legacy-фолбэк (entry_price не задан): qty + ближайший createdTime
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
                        "entry=%s) — не атрибутирую", symbol, order_id, qty,
                        entry_price)
            return None
        # openFee/closeFee — официальные поля ответа close-pnl; closedPnl уже
        # net (= gross − openFee − closeFee), поэтому комиссию храним отдельно
        # как самостоятельную метрику издержек, а не вычитаем повторно.
        # https://bybit-exchange.github.io/docs/v5/position/close-pnl
        return {
            "pnl": _as_float(chosen.get("closedPnl")),
            "exit": _as_float(chosen.get("avgExitPrice")),
            "fees": _fee_sum(chosen),
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
        позиции: один ``avgEntryPrice``, разные ``closedSize``. Точечный матч
        ``closed_pnl_detail`` по ``closedSize ≈ qty`` такие сделки не ловит (ни
        одна запись не равна полному объёму) → сделка зависает provisional с
        завышенной оценкой. Здесь собираем ВСЕ записи символа в окне, фильтруем
        по ``avgEntryPrice`` (допуск ``entry_tol`` — разделяет соседние reload'ы)
        и суммируем ``closedPnl`` ТОЛЬКО если ``Σ closedSize ≈ qty`` (вся позиция
        собрана). Иначе ``None`` (не выдумываем — `no-data-fitting`).

        Funding/settlement (``execType`` Settle/SessionSettlePnL/MovePosition) в
        матч по объёму НЕ входят. ``closedPnl`` уже net (комиссии + funding).
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
        sum_fees = 0.0
        fees_known = False
        n = 0
        for it in items:
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
            fee = _fee_sum(it)
            if fee is not None:
                sum_fees += fee
                fees_known = True
            n += 1
        if n == 0 or abs(sum_qty - qty) > max(qty * qty_tol_frac, 1e-9):
            return None
        return {
            "pnl": sum_pnl,
            "exit": (sum_exit_val / sum_qty) if sum_qty > 0 else None,
            "fees": sum_fees if fees_known else None,
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
