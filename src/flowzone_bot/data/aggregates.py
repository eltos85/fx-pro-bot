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

from flowzone_bot.analysis.session import session_start_ts


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
    # Per-SESSION footprint-профиль (контекст аукциона, STRATEGY §2): якорь —
    # старт текущего London/NY окна. idx корзины → (buy, sell). Цена корзины =
    # idx × vp_bucket_size. 0/{} = профиль не активен (вне сессии / нет bucket).
    vp_bucket_size: float = 0.0
    vp_buckets: dict[int, tuple[float, float]] = field(default_factory=dict)
    # Unix-якорь per-session профиля (старт текущего session-окна). None — вне
    # активной сессии (профиль не строим, контекст = BALANCE/unknown).
    vp_session_start: float | None = None
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
        session_windows: list[tuple[float, float]] | None = None,
        print_store=None,
        now: callable = time.monotonic,
        wall_now: callable = time.time,
    ) -> None:
        self.symbol = symbol
        self._trade_window = trade_window_sec
        self._ob_levels = ob_levels
        self._max_age = max_age_sec
        self._vp_bucket_size = vp_bucket_size
        self._session_windows = list(session_windows or [])
        self._print_store = print_store
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
        # Per-SESSION footprint-профиль (контекст §2): якорь — старт текущего
        # London/NY окна (session.session_start_ts). Инкрементальный, чтобы не
        # хранить миллионы тиков. Сбрасывается при смене session-якоря (новое
        # окно / выход из сессии).
        self._vp_session_start: float | None = None
        self._vp: dict[int, list[float]] = {}

    def set_vp_bucket_size(self, size: float) -> None:
        """Установить ширину корзины VP (price units). Вызывается из main после
        получения tick_size инструмента; сбрасывает накопленный профиль (старые
        корзины несовместимы с новым размером)."""
        with self._lock:
            if size != self._vp_bucket_size:
                self._vp_bucket_size = size
                self._vp = {}
                self._vp_session_start = None

    def set_session_windows(self, windows: list[tuple[float, float]]) -> None:
        with self._lock:
            self._session_windows = list(windows or [])
            self._vp = {}
            self._vp_session_start = None

    # ─── Writers (из ws-потока) ──────────────────────────────────────────

    def on_trade(self, price: float, size: float, side: str) -> None:
        """publicTrade: side = taker side. Сохраняем КАЖДЫЙ принт целиком
        (footprint), не схлопывая — нужно для delta-by-price и big-trades.
        Принт также persist-ится в БД (print_store) для per-swing профиля (A2)."""
        now = self._now()
        wall = self._wall_now()
        with self._lock:
            self._trades.append(TradePrint(now, price, size, side))
            self._last_price = price
            self._last_update = now
            self._accum_vp_locked(price, size, side, wall)
            self._evict_locked(now)
        if self._print_store is not None:
            try:
                self._print_store.ingest(wall, self.symbol, price, size, side)
            except Exception:
                # print_store не должен ронять поток сделок; ошибка логируется
                # в store. Здесь глушим — торговый поток важнее persist-а.
                pass

    def _accum_vp_locked(self, price: float, size: float, side: str,
                         wall: float) -> None:
        """Инкрементально докинуть объём сделки в per-SESSION footprint-профиль.

        Якорь = старт текущего session-окна (London/NY). Вне сессии профиль НЕ
        строим (контекст = BALANCE — не торгуем). При смене якоря профиль
        сбрасывается (новое окно = новый аукцион)."""
        if self._vp_bucket_size <= 0 or price <= 0 or not self._session_windows:
            return
        anchor = session_start_ts(wall, self._session_windows)
        if anchor is None:
            # вышли из сессии — сброс старого профиля
            if self._vp:
                self._vp = {}
                self._vp_session_start = None
            return
        if anchor != self._vp_session_start:
            self._vp_session_start = anchor
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
                vp_session_start=self._vp_session_start,
                bids=list(self._bids),
                asks=list(self._asks),
            )

    def _evict_locked(self, now: float) -> None:
        cut = now - self._trade_window
        while self._trades and self._trades[0].ts < cut:
            self._trades.popleft()
