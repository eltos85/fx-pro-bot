"""Bybit V5 клиент для AI Arena (Nof1 Alpha Arena clone).

Обёртка над `pybit.unified_trading.HTTP`. Полностью изолирован от
`ai_trader.trading.client` и `bybit_bot.trading.client` (см. правило
`strategy-guard.mdc`: «ЗАПРЕЩЕНО импортировать fx_pro_bot.* из bybit_bot.*
и наоборот» — то же касается остальных ботов).

Минимальный набор операций для Nof1-style цикла:
- ``get_klines``                  — исторические свечи (3m / 4h)
- ``get_ticker``                  — last price + funding
- ``get_open_interest``           — Nof1: OI latest + 20×5min avg
- ``get_funding_rate_history``    — Nof1: funding rate history
- ``get_instrument_info``         — qty_step / tick_size фильтры
- ``get_wallet_balance``          — equity для cash/margin
- ``get_positions``               — открытые позиции с unrealised PnL
- ``set_leverage``                — перед place_order
- ``place_order``                 — market + опц. SL/TP
- ``close_position``              — reduce-only ордер

Ссылки на API-доку (правило `api-docs.mdc`):
- https://bybit-exchange.github.io/docs/v5/intro
- https://bybit-exchange.github.io/docs/v5/market/open-interest
- https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


@dataclass
class Bar:
    ts: int  # unix ms (start of bar)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    symbol: str
    side: str  # "Buy" / "Sell" / "None"
    size: float
    entry_price: float
    leverage: float
    unrealised_pnl: float
    position_value: float
    liquidation_price: float


@dataclass
class Ticker:
    symbol: str
    last_price: float
    bid: float
    ask: float
    funding_rate: float
    volume_24h: float
    price_change_pct_24h: float


@dataclass
class InstrumentInfo:
    """Лот-/цена-фильтры Bybit для конкретного инструмента.

    Несоблюдение `qty_step` / `tick_size` → Bybit отвергает ордер с
    ErrCode 10001 «Qty invalid» / «Price invalid».
    """

    symbol: str
    qty_step: float
    min_order_qty: float
    max_order_qty: float
    tick_size: float


@dataclass
class OpenInterestPoint:
    """Одна точка из get_open_interest (V5)."""

    ts: int
    open_interest: float


class AiArenaBybitClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        demo: bool = True,
        category: str = "linear",
    ) -> None:
        # recv_window=10000 — рекомендация Bybit V5 для high-latency
        # клиентов (https://bybit-exchange.github.io/docs/v5/guide).
        self._session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            demo=demo,
            recv_window=10000,
        )
        self._category = category
        self._instr_cache: dict[str, InstrumentInfo] = {}

    # ─── Market data ─────────────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str = "3", limit: int = 50) -> list[Bar]:
        """interval Bybit format: '1','3','5','15','30','60','120','240','D','W'.

        Nof1 layout: интервал="3" для intraday × 50, интервал="240" для 4h × 50.
        Возвращает сортированный по времени массив (oldest → newest).
        """
        try:
            resp = self._session.get_kline(
                category=self._category,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
        except Exception:
            log.exception("get_klines %s %s failed", symbol, interval)
            return []
        items = resp.get("result", {}).get("list", []) or []
        bars: list[Bar] = []
        for row in items:
            try:
                bars.append(
                    Bar(
                        ts=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (ValueError, IndexError, TypeError):
                continue
        bars.sort(key=lambda b: b.ts)
        return bars

    def get_ticker(self, symbol: str) -> Ticker | None:
        try:
            resp = self._session.get_tickers(category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_ticker %s failed", symbol)
            return None
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            return None
        t = items[0]
        try:
            return Ticker(
                symbol=t.get("symbol", symbol),
                last_price=float(t.get("lastPrice", 0) or 0),
                bid=float(t.get("bid1Price", 0) or 0),
                ask=float(t.get("ask1Price", 0) or 0),
                funding_rate=float(t.get("fundingRate", 0) or 0),
                volume_24h=float(t.get("volume24h", 0) or 0),
                price_change_pct_24h=float(t.get("price24hPcnt", 0) or 0) * 100,
            )
        except (ValueError, TypeError):
            log.exception("ticker parse failed %s: %s", symbol, t)
            return None

    def get_open_interest(
        self, symbol: str, interval_time: str = "5min", limit: int = 20
    ) -> list[OpenInterestPoint]:
        """Open Interest history.

        Bybit V5 endpoint: `/v5/market/open-interest`.
        intervalTime values: 5min | 15min | 30min | 1h | 4h | 1d.

        Nof1 Alpha Arena использует 20×5min для расчёта OI latest + average.
        Возвращает массив, отсортированный oldest → newest.
        """
        try:
            resp = self._session.get_open_interest(
                category=self._category,
                symbol=symbol,
                intervalTime=interval_time,
                limit=limit,
            )
        except Exception:
            log.exception("get_open_interest %s failed", symbol)
            return []
        items = resp.get("result", {}).get("list", []) or []
        out: list[OpenInterestPoint] = []
        for row in items:
            try:
                out.append(
                    OpenInterestPoint(
                        ts=int(row.get("timestamp", 0)),
                        open_interest=float(row.get("openInterest", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        out.sort(key=lambda p: p.ts)
        return out

    def get_funding_rate_history(self, symbol: str, limit: int = 8) -> list[float]:
        """История funding rate (последние N значений).

        Bybit V5 endpoint: `/v5/market/funding/history`.
        Bybit funding schedule: 00:00 / 08:00 / 16:00 UTC (3 раза в сутки),
        поэтому limit=8 ≈ ~2.5 дня funding-снапшотов.

        Возвращает список fundingRate как float (oldest → newest).
        """
        try:
            resp = self._session.get_funding_rate_history(
                category=self._category,
                symbol=symbol,
                limit=limit,
            )
        except Exception:
            log.exception("get_funding_rate_history %s failed", symbol)
            return []
        items = resp.get("result", {}).get("list", []) or []
        # Bybit возвращает newest → oldest; нормализуем oldest → newest.
        rates: list[tuple[int, float]] = []
        for row in items:
            try:
                rates.append(
                    (
                        int(row.get("fundingRateTimestamp", 0) or 0),
                        float(row.get("fundingRate", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        rates.sort(key=lambda x: x[0])
        return [r for _, r in rates]

    def get_instrument_info(self, symbol: str) -> InstrumentInfo | None:
        if symbol in self._instr_cache:
            return self._instr_cache[symbol]
        try:
            resp = self._session.get_instruments_info(
                category=self._category, symbol=symbol,
            )
        except Exception:
            log.exception("get_instruments_info %s failed", symbol)
            return None
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            log.warning("instruments-info empty for %s", symbol)
            return None
        item = items[0]
        lf = item.get("lotSizeFilter", {}) or {}
        pf = item.get("priceFilter", {}) or {}
        try:
            info = InstrumentInfo(
                symbol=symbol,
                qty_step=float(lf.get("qtyStep", "0.001")),
                min_order_qty=float(lf.get("minOrderQty", "0")),
                max_order_qty=float(lf.get("maxOrderQty", "1e18")),
                tick_size=float(pf.get("tickSize", "0.01")),
            )
        except (ValueError, TypeError) as exc:
            log.warning("instruments-info parse failed %s: %s", symbol, exc)
            return None
        self._instr_cache[symbol] = info
        log.info(
            "Instrument %s: qty_step=%s min_qty=%s tick=%s",
            symbol, info.qty_step, info.min_order_qty, info.tick_size,
        )
        return info

    # ─── Account / positions ─────────────────────────────────────────────

    def get_wallet_balance(self) -> tuple[float, float]:
        """Возвращает (equity, available_cash) в USDT.

        equity — total account value (используется для total_return_pct).
        available_cash — то, что не залочено в позициях (используется
        для notional-cap расчёта).
        """
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
        except Exception:
            log.exception("get_wallet_balance failed")
            return (0.0, 0.0)
        accounts = resp.get("result", {}).get("list", []) or []
        for acc in accounts:
            for coin in acc.get("coin", []) or []:
                if coin.get("coin") == "USDT":
                    try:
                        equity = float(coin.get("equity", 0) or 0)
                        avail = float(
                            coin.get("availableToWithdraw")
                            or coin.get("walletBalance")
                            or 0
                        )
                        return (equity, avail)
                    except (ValueError, TypeError):
                        return (0.0, 0.0)
        return (0.0, 0.0)

    def get_positions(self, symbol: str | None = None) -> list[Position] | None:
        """Открытые позиции.

        - ``[]`` — API ответил, открытых позиций нет.
        - ``None`` — запрос не удался (network/DNS/non-zero retCode).
          Caller обязан различать эти случаи (см. инцидент 2026-05-07
          в `BUILDLOG_AI_TRADER.md` про false-close при API outage).
        """
        try:
            params: dict = {"category": self._category, "settleCoin": "USDT"}
            if symbol:
                params["symbol"] = symbol
            resp = self._session.get_positions(**params)
        except Exception:
            log.exception("get_positions failed")
            return None
        ret_code = resp.get("retCode")
        if ret_code not in (0, None):
            log.warning(
                "get_positions non-zero retCode: code=%s msg=%s",
                ret_code, resp.get("retMsg", ""),
            )
            return None
        items = resp.get("result", {}).get("list", []) or []
        out: list[Position] = []
        for p in items:
            try:
                size = float(p.get("size", 0) or 0)
                if size <= 0:
                    continue
                out.append(
                    Position(
                        symbol=p.get("symbol", ""),
                        side=p.get("side", ""),
                        size=size,
                        entry_price=float(p.get("avgPrice", 0) or 0),
                        leverage=float(p.get("leverage", 1) or 1),
                        unrealised_pnl=float(p.get("unrealisedPnl", 0) or 0),
                        position_value=float(p.get("positionValue", 0) or 0),
                        liquidation_price=float(p.get("liqPrice", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        return out

    # ─── Orders / leverage ───────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._session.set_leverage(
                category=self._category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            return True
        except Exception as e:
            msg = str(e)
            if "leverage not modified" in msg.lower() or "110043" in msg:
                return True  # уже стоит то же значение, не ошибка
            log.warning("set_leverage %s %dx failed: %s", symbol, leverage, e)
            return False

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_link_id: str,
        sl_price: float | None = None,
        tp_price: float | None = None,
        reduce_only: bool = False,
    ) -> dict:
        """Market-ордер с опциональными SL/TP.

        side: 'Buy' / 'Sell'
        order_link_id: префикс 'arena_' для нашей идентификации.

        Возвращает dict:
        - При успехе: ``{"ok": True, "result": <bybit result>, "raw": <resp>}``
        - При ошибке: ``{"ok": False, "error": <message>, "params": <params>}``
        """
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "orderLinkId": order_link_id,
            "reduceOnly": reduce_only,
        }
        if sl_price is not None and not reduce_only:
            params["stopLoss"] = str(sl_price)
        if tp_price is not None and not reduce_only:
            params["takeProfit"] = str(tp_price)
        try:
            resp = self._session.place_order(**params)
        except Exception as e:
            log.exception("place_order exception: %s", params)
            return {"ok": False, "error": f"exception: {e}", "params": params}
        ret_code = resp.get("retCode")
        ret_msg = resp.get("retMsg", "")
        if ret_code not in (0, None):
            log.warning(
                "place_order non-zero retCode: code=%s msg=%s params=%s",
                ret_code, ret_msg, params,
            )
            return {
                "ok": False,
                "error": f"bybit retCode={ret_code} msg={ret_msg}",
                "params": params,
                "raw": resp,
            }
        return {"ok": True, "result": resp.get("result"), "raw": resp}

    def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict:
        """Reduce-only ордер с противоположной стороной."""
        opposite = "Sell" if side == "Buy" else "Buy"
        return self.place_order(
            symbol=symbol,
            side=opposite,
            qty=qty,
            order_link_id=link_id,
            reduce_only=True,
        )
