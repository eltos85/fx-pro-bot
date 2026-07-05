"""Bitget spot WS+REST коллектор (milestone M2).

Реализация по официальной доке (api-docs.mdc — все константы со ссылкой):
- WS endpoint: wss://ws.bitget.com/v2/ws/public
  (https://www.bitget.com/api-doc/common/websocket-intro)
- Heartbeat: строка ``"ping"`` каждые 30с; сервер отключает через 2мин без
  ping; ≤10 msg/сек; ≤50 каналов/соединение (рекомендация); 240 подписок/час.
- books (SPOT, 200ms push):
  https://www.bitget.com/api-doc/contract/websocket/public/Order-Book-Channel
  первый push ``action:"snapshot"`` (полная книга) → затем ``action:"update"``
  (инкремент). Поля ``seq`` (monotonic) + ``pseq`` (предыдущий seq, ≠0 для
  update). Gap = ``pseq != last_seq`` → reinit (resubscribe — books снова
  стартует со snapshot).
- trade (SPOT): ``action:"snapshot"`` недавних → ``update``;
  ``data:[{ts, price, size, side:"buy"|"sell", tradeId}]``
  (https://www.bitget.com/zh-CN/api-doc/spot/websocket/public/Trades-Channel)
- REST orderbook (периодическая запись для replay-ленты):
  https://api.bitget.com/api/v2/spot/market/orderbook?symbol=&limit=

Данные — JSON (в отличие от MEXC protobuf). Без приватных ключей (Фаза 1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade
from yorsh_bot.exchanges.mexc import (
    HealthCb, SnapshotCb, TradeCb, DiffCb,
)

log = logging.getLogger("yorsh_bot.bitget")

# ─── Константы подключения (офиц. дока) ──────────────────────────────────
# https://www.bitget.com/api-doc/common/websocket-intro
WS_URL = "wss://ws.bitget.com/v2/ws/public"
MAX_CHANNELS_PER_CONN = 50          # рекомендация доки (хард-лимит 1000)
PING_INTERVAL_SEC = 30              # доки: ping каждые 30с
NO_PING_DISCONNECT_SEC = 120        # доки: disconnect через 2мин без ping
MAX_MSG_PER_SEC = 10                # доки: ≤10 msg/сек
SUB_LIMIT_PER_HOUR = 240            # доки: 240 подписок/час/соединение

# REST orderbook (периодическая запись для replay).
REST_ORDERBOOK_URL = "https://api.bitget.com/api/v2/spot/market/orderbook"
REST_ORDERBOOK_LIMIT = 100

INST_TYPE = "SPOT"

# Reconnect backoff (capped exponential) — как у MEXC.
RECONNECT_INITIAL_SEC = 1.0
RECONNECT_MAX_SEC = 60.0
RECONNECT_RESET_AFTER_SEC = 600.0


def _book_args(symbol: str) -> dict[str, str]:
    return {"instType": INST_TYPE, "channel": "books", "instId": symbol}


def _trade_args(symbol: str) -> dict[str, str]:
    return {"instType": INST_TYPE, "channel": "trade", "instId": symbol}


class BitgetSpotClient:
    """WS-коллектор Bitget spot (trades + books) + периодический REST snapshot.

    Один клиент = одно WS-соединение. ``books`` стартует со snapshot →
    локальная книга инициализируется из WS (отдельный REST на старте не нужен).
    При gap (``pseq != last_seq``) — reinit через resubscribe канала books.
    """

    def __init__(self, symbols: list[str], *,
                 on_trade: TradeCb | None = None,
                 on_diff: DiffCb | None = None,
                 on_snapshot: SnapshotCb | None = None,
                 on_health: HealthCb | None = None,
                 snapshot_every_sec: float = 300.0) -> None:
        # 2 канала на символ (books + trade) → ≤25 символов при лимите 50.
        if len(symbols) * 2 > MAX_CHANNELS_PER_CONN:
            raise ValueError(
                f"Bitget: {len(symbols)} symbols × 2 channels > "
                f"{MAX_CHANNELS_PER_CONN} recommended per connection")
        self.symbols = [s.upper() for s in symbols]
        self.on_trade = on_trade
        self.on_diff = on_diff
        self.on_snapshot = on_snapshot
        self.on_health = on_health
        self.snapshot_every_sec = snapshot_every_sec
        # символы, требующие reinit books (gap) — resubscribe дрейнером.
        self._resubscribe: set[str] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    def request_reinit(self, symbol: str) -> None:
        """Пометить символ для resubscribe books (вызывает app/main при gap)."""
        self._resubscribe.add(symbol.upper())

    async def run(self) -> None:
        attempts = 0
        last_ok = 0.0
        while True:
            try:
                await self._run_once()
                if time.time() - last_ok > RECONNECT_RESET_AFTER_SEC:
                    attempts = 0
                last_ok = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempts += 1
                backoff = min(RECONNECT_MAX_SEC,
                              RECONNECT_INITIAL_SEC * (2 ** (attempts - 1)))
                log.warning("Bitget disconnect: %s; reconnect %.1fs (attempt %d)",
                            e, backoff, attempts)
                if self.on_health:
                    await self.on_health("reconnect",
                                         f"attempt={attempts} backoff={backoff:.1f}")
                await asyncio.sleep(backoff)

    async def _run_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                self._ws = ws
                await self._subscribe(ws)
                if self.on_health:
                    await self.on_health("connected", None)
                ping_t = asyncio.create_task(self._ping_loop(ws))
                snap_t = asyncio.create_task(self._snapshot_loop())
                reinit_t = asyncio.create_task(self._reinit_drainer())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_text(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            # доки: JSON; но сервер может слать permessage-deflate
                            await self._on_text(msg.data.decode("utf-8", "ignore"))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    ping_t.cancel()
                    snap_t.cancel()
                    reinit_t.cancel()
                    await asyncio.gather(ping_t, snap_t, reinit_t,
                                         return_exceptions=True)
                    self._ws = None

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        args: list[dict[str, str]] = []
        for sym in self.symbols:
            args.append(_book_args(sym))
            args.append(_trade_args(sym))
        await ws.send_str(json.dumps({"op": "subscribe", "args": args}))
        log.info("Bitget subscribed: %d channels for %d symbols",
                 len(args), len(self.symbols))

    async def _resubscribe_books(self, symbols: set[str]) -> None:
        """Reinit books для символов с gap: unsubscribe + subscribe (fresh snapshot)."""
        if not symbols or self._ws is None:
            return
        args = [_book_args(s) for s in symbols]
        await self._ws.send_str(json.dumps({"op": "unsubscribe", "args": args}))
        await self._ws.send_str(json.dumps({"op": "subscribe", "args": args}))
        if self.on_health:
            await self.on_health("reinit", f"books resubscribe: {sorted(symbols)}")

    async def _reinit_drainer(self) -> None:
        """Дрейнит _resubscribe: периодически делает resubscribe books."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._resubscribe:
                    pending = set(self._resubscribe)
                    self._resubscribe.clear()
                    try:
                        await self._resubscribe_books(pending)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Bitget reinit failed: %s", e)
        except asyncio.CancelledError:
            return

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # https://www.bitget.com/api-doc/common/websocket-intro: string "ping"
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send_str("ping")
        except asyncio.CancelledError:
            return

    async def _snapshot_loop(self) -> None:
        """Периодическая REST-запись orderbook (для replay-ленты Фазы 2)."""
        try:
            while True:
                await asyncio.sleep(self.snapshot_every_sec)
                for sym in self.symbols:
                    try:
                        snap = await self._fetch_snapshot(sym)
                        if self.on_snapshot:
                            await self.on_snapshot(snap)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Bitget snapshot %s failed: %s", sym, e)
                        if self.on_health:
                            await self.on_health("snapshot", f"{sym}: {e}")
        except asyncio.CancelledError:
            return

    async def _fetch_snapshot(self, symbol: str) -> BookSnapshot:
        async with aiohttp.ClientSession() as s:
            async with s.get(REST_ORDERBOOK_URL, params={
                    "symbol": symbol, "limit": REST_ORDERBOOK_LIMIT}) as r:
                r.raise_for_status()
                data = await r.json()
        ts = time.time()
        book = data.get("data", {})
        return BookSnapshot(
            exchange="bitget", symbol=symbol, ts_exch=ts, ts_local=ts,
            bids=[(float(p), float(q)) for p, q in book.get("bids", [])],
            asks=[(float(p), float(q)) for p, q in book.get("asks", [])],
            seq=int(book["ts"]) if book.get("ts") else None,
            payload=data,
        )

    # ─── parse ────────────────────────────────────────────────────────────
    async def _on_text(self, text: str) -> None:
        if text == "pong":
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.debug("Bitget non-json: %r", text[:80])
            return
        # subscribe-ack: {"event":"subscribe","arg":{...}}
        if "event" in msg:
            return
        arg = msg.get("arg") or {}
        channel = arg.get("channel")
        sym = arg.get("instId")
        if sym is None or channel is None:
            return
        action = msg.get("action")
        data = msg.get("data") or []
        if channel == "books":
            await self._on_books(sym, action, data, msg)
        elif channel == "trade":
            await self._on_trade(sym, data)

    async def _on_books(self, sym: str, action: str | None,
                        data: list[Any], msg: dict) -> None:
        if not data:
            return
        item = data[0]
        ts_local = time.time()
        ts_exch = (int(item.get("ts", 0)) / 1000.0) if item.get("ts") else ts_local
        # seq/pseq — внутри data[0] (офиц. пример Depth Channel). checksum
        # удалён в мае 2026 — не используем, верим seq/pseq.
        seq = item.get("seq")
        seq = int(seq) if seq is not None else None
        pseq = item.get("pseq")
        pseq = int(pseq) if pseq is not None else None
        bids = [(b[0], float(b[1])) for b in item.get("bids", [])]
        asks = [(a[0], float(a[1])) for a in item.get("asks", [])]
        if action == "snapshot":
            snap = BookSnapshot(
                exchange="bitget", symbol=sym, ts_exch=ts_exch, ts_local=ts_local,
                bids=bids, asks=asks, seq=seq, source="ws_books",
                payload={"action": "snapshot", "pseq": pseq})
            if self.on_snapshot:
                await self.on_snapshot(snap)
            return
        # action == "update" → инкрементальный дифф
        diff = DepthDiff(
            exchange="bitget", symbol=sym, ts_exch=ts_exch, ts_local=ts_local,
            bids=bids, asks=asks, seq=seq, prev_seq=pseq,
            payload={"action": "update"})
        if self.on_diff:
            await self.on_diff(diff)
        # Gap-detection делает LocalOrderBook.needs_reinit в app/main;
        # при gap caller пишет health="gap" и (Bitget) триггерит resubscribe.

    async def _on_trade(self, sym: str, data: list[Any]) -> None:
        ts_local = time.time()
        for it in data:
            t_exch = (int(it.get("ts", 0)) / 1000.0) if it.get("ts") else ts_local
            side = it.get("side", "unknown").lower()
            if side not in ("buy", "sell"):
                side = "unknown"
            tr = Trade(
                exchange="bitget", symbol=sym, ts_exch=t_exch, ts_local=ts_local,
                price=float(it.get("price", 0.0)),
                size=float(it.get("size", 0.0)), side=side,
                payload={"trade_id": it.get("tradeId")})
            if self.on_trade:
                await self.on_trade(tr)
