"""Агрегаты микроструктуры по символу (CVD, стакан, funding, ликвидации).

Питается из Bybit public WS (``market_stream.py``). Потокобезопасно:
WS-callback пишет из ws-потока pybit, main-loop читает через ``snapshot()``
под ``threading.Lock``.

Метрики (research basis):
- CVD (Cumulative Volume Delta): сумма агрессивного buy − sell по
  ``publicTrade`` (taker side ``S``). Kalena 2026, coinxsight 2026 —
  основной orderflow-сигнал; дивергенция цена↔CVD = поглощение.
- Order-book imbalance: bid_vol/(bid_vol+ask_vol) по топ-N уровням
  ``orderbook.50``. Bookmap 2026 — дисбаланс предвосхищает импульс.
- Funding/OI: ``tickers`` (fundingRate, openInterest, markPrice).
- Liquidations: ``allLiquidation`` (side/size/price), Bybit native free
  (https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class CvdSample:
    ts: float
    price: float
    cvd: float


@dataclass
class LiqEvent:
    ts: float
    side: str  # "Buy" = шорт ликвидирован (forced buy); "Sell" = лонг ликвидирован
    size_usd: float
    price: float


@dataclass
class SymbolSnapshot:
    """Иммутабельный срез состояния для оценки сигналов."""
    symbol: str
    ts: float
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    ob_imbalance: float | None  # bid_vol/(bid_vol+ask_vol), top-N
    funding_rate: float | None
    open_interest: float | None
    cvd_samples: list[CvdSample]
    liq_events: list[LiqEvent]  # за последнее окно
    stale: bool  # True если данных давно не было


class SymbolState:
    """Потокобезопасное rolling-состояние одного символа."""

    def __init__(
        self,
        symbol: str,
        *,
        cvd_window_sec: float = 180.0,
        liq_window_sec: float = 60.0,
        ob_levels: int = 25,
        max_age_sec: float = 30.0,
        now: callable = time.monotonic,
    ) -> None:
        self.symbol = symbol
        self._cvd_window = cvd_window_sec
        self._liq_window = liq_window_sec
        self._ob_levels = ob_levels
        self._max_age = max_age_sec
        self._now = now
        self._lock = threading.Lock()

        self._cvd_cum = 0.0
        self._cvd: deque[CvdSample] = deque()
        self._liqs: deque[LiqEvent] = deque()
        self._last_price: float | None = None
        self._best_bid: float | None = None
        self._best_ask: float | None = None
        self._ob_imbalance: float | None = None
        self._funding: float | None = None
        self._oi: float | None = None
        self._last_update: float = -1e18

    # ─── Writers (из ws-потока) ──────────────────────────────────────────

    def on_trade(self, price: float, size: float, side: str) -> None:
        """publicTrade: side = taker side. Buy = агрессивная покупка."""
        now = self._now()
        with self._lock:
            delta = size if side.upper() == "BUY" else -size
            self._cvd_cum += delta
            self._cvd.append(CvdSample(now, price, self._cvd_cum))
            self._last_price = price
            self._last_update = now
            self._evict_locked(now)

    def on_orderbook(self, bids: list[tuple[float, float]],
                     asks: list[tuple[float, float]]) -> None:
        now = self._now()
        with self._lock:
            if bids:
                self._best_bid = bids[0][0]
            if asks:
                self._best_ask = asks[0][0]
            bid_vol = sum(sz for _, sz in bids[: self._ob_levels])
            ask_vol = sum(sz for _, sz in asks[: self._ob_levels])
            total = bid_vol + ask_vol
            self._ob_imbalance = (bid_vol / total) if total > 0 else None
            self._last_update = now

    def on_ticker(self, funding_rate: float | None,
                  open_interest: float | None,
                  mark_price: float | None) -> None:
        now = self._now()
        with self._lock:
            if funding_rate is not None:
                self._funding = funding_rate
            if open_interest is not None:
                self._oi = open_interest
            if mark_price is not None and self._last_price is None:
                self._last_price = mark_price
            self._last_update = now

    def on_liquidation(self, side: str, size: float, price: float) -> None:
        now = self._now()
        with self._lock:
            self._liqs.append(LiqEvent(now, side, size * price, price))
            self._last_update = now
            self._evict_locked(now)

    # ─── Reader ──────────────────────────────────────────────────────────

    def snapshot(self) -> SymbolSnapshot:
        now = self._now()
        with self._lock:
            self._evict_locked(now)
            return SymbolSnapshot(
                symbol=self.symbol,
                ts=now,
                last_price=self._last_price,
                best_bid=self._best_bid,
                best_ask=self._best_ask,
                ob_imbalance=self._ob_imbalance,
                funding_rate=self._funding,
                open_interest=self._oi,
                cvd_samples=list(self._cvd),
                liq_events=list(self._liqs),
                stale=(now - self._last_update) > self._max_age,
            )

    def _evict_locked(self, now: float) -> None:
        cvd_cut = now - self._cvd_window
        while self._cvd and self._cvd[0].ts < cvd_cut:
            self._cvd.popleft()
        liq_cut = now - self._liq_window
        while self._liqs and self._liqs[0].ts < liq_cut:
            self._liqs.popleft()
