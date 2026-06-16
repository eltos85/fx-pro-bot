"""Агрегаты микроструктуры по символу для flowzone_bot.

Питается из Bybit public WS (``market_stream.py``). Потокобезопасно:
WS-callback пишет из ws-потока pybit, main-loop читает через ``snapshot()``
под ``threading.Lock``.

КЛЮЧЕВОЕ ОТЛИЧИЕ ОТ scalp_bot (TASKSPEC §5): scalp схлопывает каждую сделку в
кумулятивный CVD по времени. Канон flowzone требует profil ДЕЛЬТЫ ПО ЦЕНЕ
(delta-at-price / footprint) и размер КАЖДОЙ сделки (big trades). Поэтому здесь
храним сырые тиковые принты (``TradePrint``): цена, размер, сторона агрессора,
время. На них Volume Profile engine (фаза 2) строит профиль по корзинам цен, а
big-trades detector (фаза 3) считает percentile размера.

Источник правды о потоке — Bybit ``publicTrade`` (taker side ``S``):
https://bybit-exchange.github.io/docs/v5/websocket/public/trade
ob_imbalance (доп-фактор, не главный триггер абсорбции) — ``orderbook.50``:
https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TradePrint:
    """Один исполненный тик (footprint-атом). ``side`` = сторона АГРЕССОРА
    (Bybit ``S``: Buy = агрессивная покупка, Sell = агрессивная продажа)."""
    ts: float
    price: float
    size: float
    side: str  # "Buy" | "Sell"

    @property
    def signed_delta(self) -> float:
        """+size для агрессивной покупки, −size для агрессивной продажи."""
        return self.size if self.side.upper() == "BUY" else -self.size


@dataclass
class SymbolSnapshot:
    """Иммутабельный срез состояния для оценки контекста/зон/триггера."""
    symbol: str
    ts: float
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    ob_imbalance: float | None  # bid_vol/(bid_vol+ask_vol), top-N
    trades: list[TradePrint]    # тиковые принты за trade_window_sec
    stale: bool                 # True если данных давно не было
    # Дневной footprint-профиль (инкрементальный): idx корзины → (buy, sell)
    # объём. Цена корзины = idx × vp_bucket_size. 0/{} = профиль выключен
    # (vp_bucket_size не задан, напр. observe без REST-инструмента).
    vp_bucket_size: float = 0.0
    vp_buckets: dict[int, tuple[float, float]] = field(default_factory=dict)
    # Top-N уровни стакана (цена, объём). bids убыв. по цене, asks возр.
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)


class SymbolState:
    """Потокобезопасное rolling-состояние одного символа."""

    def __init__(
        self,
        symbol: str,
        *,
        trade_window_sec: float = 300.0,
        ob_levels: int = 25,
        max_age_sec: float = 30.0,
        vp_bucket_size: float = 0.0,
        now: callable = time.monotonic,
        wall_now: callable = time.time,
    ) -> None:
        self.symbol = symbol
        self._trade_window = trade_window_sec
        self._ob_levels = ob_levels
        self._max_age = max_age_sec
        self._vp_bucket_size = vp_bucket_size
        self._now = now
        self._wall_now = wall_now
        self._lock = threading.Lock()

        self._trades: deque[TradePrint] = deque()
        self._last_price: float | None = None
        self._best_bid: float | None = None
        self._best_ask: float | None = None
        self._ob_imbalance: float | None = None
        self._bids: list[tuple[float, float]] = []
        self._asks: list[tuple[float, float]] = []
        self._last_update: float = -1e18
        # Дневной footprint-профиль (idx → [buy, sell]), якорь — UTC-день
        # (канон «Dly Vol. Profile», STRATEGY §6.3). Инкрементальный, чтобы не
        # хранить миллионы тиков для сессионного VP.
        self._vp_day: int = -1
        self._vp: dict[int, list[float]] = {}

    def set_vp_bucket_size(self, size: float) -> None:
        """Установить ширину корзины VP (price units). Вызывается из main после
        получения tick_size инструмента; сбрасывает накопленный профиль (старые
        корзины несовместимы с новым размером)."""
        with self._lock:
            if size != self._vp_bucket_size:
                self._vp_bucket_size = size
                self._vp = {}
                self._vp_day = -1

    # ─── Writers (из ws-потока) ──────────────────────────────────────────

    def on_trade(self, price: float, size: float, side: str) -> None:
        """publicTrade: side = taker side. Сохраняем КАЖДЫЙ принт целиком
        (footprint), не схлопывая — нужно для delta-by-price и big-trades."""
        now = self._now()
        with self._lock:
            self._trades.append(TradePrint(now, price, size, side))
            self._last_price = price
            self._last_update = now
            self._accum_vp_locked(price, size, side)
            self._evict_locked(now)

    def _accum_vp_locked(self, price: float, size: float, side: str) -> None:
        """Инкрементально докинуть объём сделки в дневной footprint-профиль."""
        if self._vp_bucket_size <= 0 or price <= 0:
            return
        day = int(self._wall_now() // 86400)
        if day != self._vp_day:
            self._vp_day = day
            self._vp = {}
        idx = int(price / self._vp_bucket_size)
        bucket = self._vp.get(idx)
        if bucket is None:
            bucket = [0.0, 0.0]
            self._vp[idx] = bucket
        if side.upper() == "BUY":
            bucket[0] += size
        else:
            bucket[1] += size

    def on_orderbook(self, bids: list[tuple[float, float]],
                     asks: list[tuple[float, float]]) -> None:
        now = self._now()
        with self._lock:
            if bids:
                self._best_bid = bids[0][0]
            if asks:
                self._best_ask = asks[0][0]
            self._bids = list(bids[: self._ob_levels])
            self._asks = list(asks[: self._ob_levels])
            bid_vol = sum(sz for _, sz in self._bids)
            ask_vol = sum(sz for _, sz in self._asks)
            total = bid_vol + ask_vol
            self._ob_imbalance = (bid_vol / total) if total > 0 else None
            self._last_update = now

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
                trades=list(self._trades),
                stale=(now - self._last_update) > self._max_age,
                vp_bucket_size=self._vp_bucket_size,
                vp_buckets={i: (b[0], b[1]) for i, b in self._vp.items()},
                bids=list(self._bids),
                asks=list(self._asks),
            )

    def _evict_locked(self, now: float) -> None:
        cut = now - self._trade_window
        while self._trades and self._trades[0].ts < cut:
            self._trades.popleft()
