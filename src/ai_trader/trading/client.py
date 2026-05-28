"""Bybit-клиент для AI-Trader.

Обёртка над pybit.unified_trading.HTTP. БЕЗ импортов из bybit_bot —
изолированная экосистема (правило strategy-guard.mdc).

Минимальный набор операций для агентного цикла:
- get_klines: исторические свечи для market context
- get_positions: текущие открытые позиции
- get_wallet_balance: equity для расчёта margin
- get_tickers: последняя цена + funding rate
- place_order: открытие/закрытие с orderLinkId
- set_trading_stop: установка SL/TP на бирже
- set_leverage: установка плеча перед открытием
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


@dataclass
class Bar:
    ts: int  # unix ms
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
    # v0.17: live exchange data для feeding в LLM context (Шаг 2a).
    # Bybit считает unrealised_pnl от ``mark_price``, не от last_price —
    # поэтому именно эти 2 поля показывают реальную картину позиции.
    # ``liq_price`` критичен при leverage: бот видит насколько близок к
    # ликвидации без необходимости вычислять самому.
    mark_price: float = 0.0
    liq_price: float = 0.0


@dataclass
class ClosedPnl:
    """Запись из Bybit ``/v5/position/closed-pnl``.

    v0.18 (2026-05-25): bot ранее писал в БД ``realized_pnl_usd`` как
    ``(exit - entry) × qty`` — это **gross** PnL без trading fee и
    funding settlement. Это занижало убытки и завышало прибыль (за день
    25/05 расхождение составило 41% от реального net-убытка). Теперь
    поверх gross-расчёта мы делаем поправку: дёргаем этот endpoint и
    берём ``closedPnl`` как точное net-значение, которое уже
    учитывает все комиссии и funding на момент закрытия позиции.

    Поля Bybit V5 (только используемые):
    - ``symbol``, ``side``, ``orderLinkId``: для матчинга записи с нашей
      open-позицией в БД (open-ордер ставился с ``ai_open_…`` link_id,
      который сохраняется в Bybit на closed-pnl записи).
    - ``closedSize``: размер закрытой части (для матчинга с qty в БД).
    - ``avgEntryPrice`` / ``avgExitPrice``: средние цены — могут чуть
      отличаться от наших, потому что биржа усредняет для slippage.
    - ``closedPnl``: **net** PnL в USDT (после fee, funding).
    - ``createdTime`` / ``updatedTime``: для time-window сопоставления.
    """
    symbol: str
    side: str
    order_link_id: str
    closed_size: float
    avg_entry_price: float
    avg_exit_price: float
    closed_pnl: float
    created_time_ms: int
    updated_time_ms: int


@dataclass
class Ticker:
    symbol: str
    last_price: float
    bid: float
    ask: float
    funding_rate: float
    volume_24h: float
    price_change_pct_24h: float
    # v0.21 (2026-05-28): next funding settlement timestamp в ms (Bybit
    # ``nextFundingTime`` поле тикера). По умолчанию каждые 8ч —
    # 00:00 / 08:00 / 16:00 UTC. Используется в context.py чтобы
    # показать LLM "до funding осталось N min, expected cost $X".
    # 0 = биржа не вернула (rare) — LLM получит "next: unknown".
    next_funding_time_ms: int = 0


@dataclass
class FundingEvent:
    """v0.21: одно funding settlement из transaction-log.

    Bybit endpoint ``/v5/account/transaction-log`` возвращает поля:
    - ``type=SETTLEMENT`` для funding
    - ``funding`` (string): подписанное USD-значение per settlement.
      Знак: отрицательное = бот заплатил (long при rate>0 / short при
      rate<0), положительное = получил (long при rate<0 / short при rate>0).
    - ``transactionTime`` (ms): момент settlement (всегда 00/08/16 UTC).
    - ``symbol``, ``side``: для матчинга с позицией.

    Funding в ``closedPnl`` НЕ включается, поэтому надо собирать отдельно.
    """
    symbol: str
    side: str  # "Buy" / "Sell"
    funding_usd: float  # signed
    transaction_time_ms: int


@dataclass
class InstrumentInfo:
    """Фильтры лот/цены Bybit для конкретного инструмента.

    Используется чтобы округлять qty под `qty_step` и SL/TP под
    `tick_size` — иначе Bybit отклоняет ордер с ErrCode 10001
    «Qty invalid» / «Price invalid» (см. AUDIT_2026.md).
    """
    symbol: str
    qty_step: float          # шаг кол-ва (XRPUSDT=1, BTCUSDT=0.001 и т.д.)
    min_order_qty: float
    max_order_qty: float
    tick_size: float         # шаг цены


class AiBybitClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        demo: bool = True,
        category: str = "linear",
    ) -> None:
        self._session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            demo=demo,
            recv_window=10000,
        )
        self._category = category
        # in-memory кэш instruments-info (контракты не меняются часто).
        self._instr_cache: dict[str, InstrumentInfo] = {}

    # ─── Market data ─────────────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str = "60", limit: int = 50) -> list[Bar]:
        """interval Bybit format: '1','3','5','15','30','60','120','240','D','W'."""
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
                next_funding_time_ms=int(t.get("nextFundingTime", 0) or 0),
            )
        except (ValueError, TypeError):
            log.exception("ticker parse failed %s: %s", symbol, t)
            return None

    def get_instrument_info(self, symbol: str) -> InstrumentInfo | None:
        """Получить лот/цена-фильтры для symbol с in-memory кэшированием.

        В Bybit V5 `lotSizeFilter.qtyStep` определяет минимальный шаг
        кол-ва (для XRPUSDT linear = 1.0 — целые XRP, для BTCUSDT
        linear = 0.001). Несоблюдение → 10001 «Qty invalid».
        """
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

    def get_wallet_balance(self) -> float:
        """Доступный equity в USDT."""
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
        except Exception:
            log.exception("get_wallet_balance failed")
            return 0.0
        accounts = resp.get("result", {}).get("list", []) or []
        for acc in accounts:
            for coin in acc.get("coin", []) or []:
                if coin.get("coin") == "USDT":
                    try:
                        return float(coin.get("equity", 0) or 0)
                    except (ValueError, TypeError):
                        return 0.0
        return 0.0

    def get_positions(self, symbol: str | None = None) -> list[Position] | None:
        """Возвращает список открытых позиций.

        - ``[]`` — API ответил успешно, открытых позиций нет.
        - ``None`` — запрос не получился (network/DNS/timeout/non-zero retCode).
          Вызывающий код ОБЯЗАН отличать ``None`` от ``[]``: «нет ответа»
          ≠ «нет позиций». Иначе reconcile-логика помечает позиции closed
          при transient outage биржи (см. инцидент 2026-05-07).
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
                ret_code,
                resp.get("retMsg", ""),
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
                        mark_price=float(p.get("markPrice", 0) or 0),
                        liq_price=float(p.get("liqPrice", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        return out

    def get_closed_pnl(
        self,
        symbol: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 50,
    ) -> list[ClosedPnl] | None:
        """Возвращает список Closed PnL записей для symbol.

        Bybit V5 endpoint ``/v5/position/closed-pnl``. По дефолту биржа
        даёт за последние 7 дней; если ``start_ms`` задан — за указанный
        диапазон.

        Возвращает:
        - ``[]`` — API ответил, но записей нет (например, новая пара).
        - ``None`` — API упал / non-zero retCode. Вызывающий код должен
          различать ``None`` (transient outage) от ``[]`` (нет данных) —
          иначе можем перезаписать корректный gross-PnL на 0.

        v0.18 (2026-05-25): метод введён для расчёта net-PnL вместо
        gross. См. dataclass ``ClosedPnl`` для деталей.
        """
        try:
            params: dict = {
                "category": self._category,
                "symbol": symbol,
                "limit": int(limit),
            }
            if start_ms is not None:
                params["startTime"] = int(start_ms)
            if end_ms is not None:
                params["endTime"] = int(end_ms)
            resp = self._session.get_closed_pnl(**params)
        except Exception:
            log.exception("get_closed_pnl failed for %s", symbol)
            return None
        ret_code = resp.get("retCode")
        if ret_code not in (0, None):
            log.warning(
                "get_closed_pnl non-zero retCode: code=%s msg=%s",
                ret_code,
                resp.get("retMsg", ""),
            )
            return None
        items = resp.get("result", {}).get("list", []) or []
        out: list[ClosedPnl] = []
        for it in items:
            try:
                out.append(
                    ClosedPnl(
                        symbol=it.get("symbol", ""),
                        side=it.get("side", ""),
                        order_link_id=it.get("orderLinkId", ""),
                        closed_size=float(it.get("closedSize", 0) or 0),
                        avg_entry_price=float(it.get("avgEntryPrice", 0) or 0),
                        avg_exit_price=float(it.get("avgExitPrice", 0) or 0),
                        closed_pnl=float(it.get("closedPnl", 0) or 0),
                        created_time_ms=int(it.get("createdTime", 0) or 0),
                        updated_time_ms=int(it.get("updatedTime", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        return out

    def get_funding_for_position(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        side: str | None = None,
    ) -> list[FundingEvent] | None:
        """v0.21: вытащить все funding settlements по ``symbol`` в окне ``[start_ms, end_ms]``.

        Идём через ``/v5/account/transaction-log`` с фильтрами
        ``category=linear``, ``type=SETTLEMENT``, ``symbol``,
        ``startTime``/``endTime``. Bybit V5 max ``limit=50`` per page,
        делаем pagination через ``cursor`` пока он возвращается.

        Возвращает:
        - ``[]`` — окно валидное, settlement не было (позиция не пересекла
          00/08/16 UTC, либо ``end_ms ≤ start_ms``).
        - ``None`` — API упал / non-zero retCode. Caller должен оставить
          ``funding_usd=NULL`` и попробовать в следующий reconcile-цикл.
        - список ``FundingEvent`` — все settlement'ы. ``side`` фильтр
          опциональный (если позиция Buy, funding запись тоже side=Buy
          для linear perp; bybit_bot/fx_pro_bot такой match делают по
          side+symbol совпадению).

        ВАЖНО: если в окне было несколько closed позиций по одному
        символу — этот метод вернёт ВСЕ их funding'ы. Caller должен
        правильно сужать ``[start_ms, end_ms]`` под конкретную позицию
        (использовать ``opened_at``..``closed_at + slack``).
        """
        if end_ms <= start_ms:
            return []
        out: list[FundingEvent] = []
        cursor: str | None = None
        max_pages = 20
        page = 0
        while page < max_pages:
            page += 1
            try:
                params: dict = {
                    "category": self._category,
                    "symbol": symbol,
                    "type": "SETTLEMENT",
                    "startTime": int(start_ms),
                    "endTime": int(end_ms),
                    "limit": 50,
                }
                if cursor:
                    params["cursor"] = cursor
                resp = self._session.get_transaction_log(**params)
            except Exception:
                log.exception(
                    "get_transaction_log failed for %s [%d..%d] page=%d",
                    symbol, start_ms, end_ms, page,
                )
                return None
            ret_code = resp.get("retCode")
            if ret_code not in (0, None):
                log.warning(
                    "get_transaction_log non-zero retCode: code=%s msg=%s "
                    "symbol=%s window=[%d..%d]",
                    ret_code, resp.get("retMsg", ""), symbol, start_ms, end_ms,
                )
                return None
            result = resp.get("result", {}) or {}
            items = result.get("list", []) or []
            for it in items:
                try:
                    funding_str = it.get("funding", "0") or "0"
                    funding_val = float(funding_str)
                    if funding_val == 0:
                        continue
                    row_side = it.get("side", "")
                    if side is not None and row_side != side:
                        continue
                    out.append(
                        FundingEvent(
                            symbol=it.get("symbol", symbol),
                            side=row_side,
                            funding_usd=funding_val,
                            transaction_time_ms=int(
                                it.get("transactionTime", 0) or 0
                            ),
                        )
                    )
                except (ValueError, TypeError):
                    continue
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break
        return out

    # ─── Orders / leverage / SL-TP ───────────────────────────────────────

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
        order_link_id: должен начинаться с 'ai_' для нашей идентификации

        Возвращает dict:
        - При успехе: {"ok": True, "result": <bybit result>}
        - При ошибке: {"ok": False, "error": <message>, "params": <params>}

        AUDIT_2026.md P0 fix: ранее возвращался ``None``, что прятало
        реальную причину отказа Bybit (min order size / leverage / margin)
        и попадало в БД как generic «place_order returned None».
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
                ret_code,
                ret_msg,
                params,
            )
            return {
                "ok": False,
                "error": f"bybit retCode={ret_code} msg={ret_msg}",
                "params": params,
                "raw": resp,
            }
        return {"ok": True, "result": resp.get("result"), "raw": resp}

    def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict:
        """Закрыть позицию reduce-only ордером с противоположной стороной.

        Возвращает то же что place_order: {"ok": True/False, ...}.
        """
        opposite = "Sell" if side == "Buy" else "Buy"
        return self.place_order(
            symbol=symbol,
            side=opposite,
            qty=qty,
            order_link_id=link_id,
            reduce_only=True,
        )
