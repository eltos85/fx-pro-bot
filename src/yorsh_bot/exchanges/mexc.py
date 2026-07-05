"""MEXC spot WS+REST коллектор (milestone M1).

Реализация по официальной доке (api-docs.mdc — все константы со ссылкой):
- WS endpoint: wss://wbs-api.mexc.com/ws
  (https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/)
- ≤30 подписок на соединение; соединение ≤24ч; без подписки — disconnect
  через 30с; без потока — через 60с; PING keepalive `{"method":"PING"}`.
- Каналы:
  - trades: `spot@public.aggre.deals.v3.api.pb@100ms@{SYMBOL}`
    (https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/trade-streams)
  - depth:  `spot@public.aggre.depth.v3.api.pb@100ms@{SYMBOL}` —
    fromVersion/toVersion, процедура поддержания локальной книги:
    https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book
- REST snapshot: https://api.mexc.com/api/v3/depth?symbol=&limit=5000
  → {lastUpdateId, bids:[[price,qty],...], asks:[[price,qty],...]}.
- Данные — protobuf (.pb), парсер в exchanges/mexc_pb.py.

Reconnect — exponential backoff (параметры из доки: ≤24h жизни соединения →
рестарт неизбежен; backoff capped, не выдуманный — см. BUILDLOG).

Без приватных ключей и торговых вызовов (Фаза 1 = data-only).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade
from yorsh_bot.exchanges.mexc_pb import parse_wrapper

log = logging.getLogger("yorsh_bot.mexc")

# ─── Константы подключения (офиц. дока) ──────────────────────────────────
# https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/
WS_URL = "wss://wbs-api.mexc.com/ws"
MAX_SUBSCRIPTIONS_PER_CONN = 30       # офиц. лимит
CONN_MAX_LIFETIME_SEC = 24 * 3600     # соединение валидно ≤24ч
NO_SUB_DISCONNECT_SEC = 30            # без подписки — disconnect через 30с
NO_DATA_DISCONNECT_SEC = 60           # без потока — disconnect через 60с
PING_INTERVAL_SEC = 20                # < NO_DATA_DISCONNECT, чтобы держать alive

# https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/trade-streams
TRADE_CHANNEL = "spot@public.aggre.deals.v3.api.pb@100ms@{sym}"
# https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book
DEPTH_CHANNEL = "spot@public.aggre.depth.v3.api.pb@100ms@{sym}"
# REST snapshot
REST_DEPTH_URL = "https://api.mexc.com/api/v3/depth"
REST_DEPTH_LIMIT = 5000               # офиц. процедура step 3

# Reconnect backoff (capped exponential). 24h-лимит соединения = рестарт
# неизбежен; backoff не выдуманный — защита от server-side throttle
# (по аналогии с cTrader BUILDLOG 2026-05-07: max attempts не бесконечный).
RECONNECT_INITIAL_SEC = 1.0
RECONNECT_MAX_SEC = 60.0
RECONNECT_RESET_AFTER_SEC = 600.0     # успех ≥10мин → счётчик attempts в 0

# Колбэки: awaitable, получают распарсенные события.
TradeCb = Callable[[Trade], Awaitable[None]]
DiffCb = Callable[[DepthDiff], Awaitable[None]]
SnapshotCb = Callable[[BookSnapshot], Awaitable[None]]
HealthCb = Callable[[str, str | None], Awaitable[None]]


def _split_channel(channel: str) -> tuple[str | None, str | None]:
    """Из канала `spot@public.aggre.<kind>.v3.api.pb@...@{SYMBOL}` достать kind/sym."""
    parts = channel.split("@")
    # parts: [spot, public, aggre, deals|depth, v3, api.pb, 100ms, SYMBOL]
    if len(parts) < 8:
        return None, None
    kind = parts[3]   # deals | depth
    sym = parts[-1]
    return kind, sym


class MexcSpotClient:
    """WS-коллектор MEXC spot (trades + incremental depth) + REST snapshot.

    Один клиент = одно WS-соединение. При числе символов > MAX_SUBSCRIPTIONS
    коллектор поднимает несколько соединений (менеджер — в app/main, M3).
    """

    def __init__(self, symbols: list[str], *,
                 on_trade: TradeCb | None = None,
                 on_diff: DiffCb | None = None,
                 on_snapshot: SnapshotCb | None = None,
                 on_health: HealthCb | None = None,
                 snapshot_every_sec: float = 300.0) -> None:
        if len(symbols) > MAX_SUBSCRIPTIONS_PER_CONN:
            raise ValueError(
                f"MEXC: {len(symbols)} symbols > {MAX_SUBSCRIPTIONS_PER_CONN} "
                "per connection (подними второе соединение)")
        self.symbols = [s.upper() for s in symbols]
        self.on_trade = on_trade
        self.on_diff = on_diff
        self.on_snapshot = on_snapshot
        self.on_health = on_health
        self.snapshot_every_sec = snapshot_every_sec

    # ─── публичный запуск ────────────────────────────────────────────────
    async def run(self) -> None:
        """Бесконечный цикл с reconnect (capped exponential backoff)."""
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
                log.warning("MEXC disconnect: %s; reconnect через %.1fs (attempt %d)",
                            e, backoff, attempts)
                if self.on_health:
                    await self.on_health("reconnect",
                                         f"attempt={attempts} backoff={backoff:.1f}")
                await asyncio.sleep(backoff)

    # ─── одно соединение ─────────────────────────────────────────────────
    async def _run_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                await self._subscribe(ws)
                if self.on_health:
                    await self.on_health("connected", None)
                # PING-таск + snapshot-таск + read-петля
                ping_t = asyncio.create_task(self._ping_loop(ws))
                snap_t = asyncio.create_task(self._snapshot_loop(ws))
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._on_bytes(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            # PONG-ответ / SUBSCRIPTION-ack приходят текстом.
                            log.debug("MEXC text: %s", msg.data[:120])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    ping_t.cancel()
                    snap_t.cancel()
                    await asyncio.gather(ping_t, snap_t, return_exceptions=True)

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        params: list[str] = []
        for sym in self.symbols:
            params.append(TRADE_CHANNEL.format(sym=sym))
            params.append(DEPTH_CHANNEL.format(sym=sym))
        # офиц. лимит 30 подписок; у нас 2 на символ → ≤15 символов на коннект.
        sub = {"method": "SUBSCRIPTION", "params": params}
        await ws.send_str(json.dumps(sub))
        log.info("MEXC subscribed: %d channels for %d symbols",
                 len(params), len(self.symbols))

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """PING keepalive — иначе сервер отключит через 60с без потока."""
        # https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send_str(json.dumps({"method": "PING"}))
        except asyncio.CancelledError:
            return

    async def _snapshot_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Периодическая REST-запись снапшота (для replay-ленты Фазы 2)."""
        try:
            while True:
                await asyncio.sleep(self.snapshot_every_sec)
                for sym in self.symbols:
                    try:
                        snap = await self._fetch_snapshot(sym)
                        if self.on_snapshot:
                            await self.on_snapshot(snap)
                    except Exception as e:  # noqa: BLE001
                        log.warning("MEXC snapshot %s failed: %s", sym, e)
                        if self.on_health:
                            await self.on_health("snapshot", f"{sym}: {e}")
        except asyncio.CancelledError:
            return

    # ─── REST snapshot ────────────────────────────────────────────────────
    async def _fetch_snapshot(self, symbol: str) -> BookSnapshot:
        # https://api.mexc.com/api/v3/depth?symbol=&limit=5000
        async with aiohttp.ClientSession() as s:
            async with s.get(REST_DEPTH_URL, params={
                    "symbol": symbol, "limit": REST_DEPTH_LIMIT}) as r:
                r.raise_for_status()
                data = await r.json()
        ts = time.time()
        return BookSnapshot(
            exchange="mexc", symbol=symbol, ts_exch=ts, ts_local=ts,
            bids=[(float(p), float(q)) for p, q in data.get("bids", [])],
            asks=[(float(p), float(q)) for p, q in data.get("asks", [])],
            seq=int(data.get("lastUpdateId", 0)) or None,
            payload=data,
        )

    # ─── parse ────────────────────────────────────────────────────────────
    async def _on_bytes(self, data: bytes) -> None:
        try:
            w = parse_wrapper(data)
        except Exception as e:  # noqa: BLE001
            log.warning("MEXC protobuf parse failed: %s", e)
            if self.on_health:
                await self.on_health("parse_error", str(e))
            return
        body = w.WhichOneof("body")
        ts_local = time.time()
        send_time_ms = w.send_time if w.send_time else None
        ts_exch = (send_time_ms / 1000.0) if send_time_ms else ts_local
        sym = w.symbol or _split_channel(w.channel)[1]
        if sym is None:
            return
        if body == "public_aggre_depths":
            await self._on_depth(w, sym, ts_exch, ts_local)
        elif body == "public_aggre_deals":
            await self._on_deals(w, sym, ts_exch, ts_local)

    async def _on_depth(self, w: Any, sym: str,
                        ts_exch: float, ts_local: float) -> None:
        d = w.public_aggre_depths
        try:
            from_v = int(d.from_version) if d.from_version else None
            to_v = int(d.to_version) if d.to_version else None
        except ValueError:
            from_v, to_v = None, None
        diff = DepthDiff(
            exchange="mexc", symbol=sym, ts_exch=ts_exch, ts_local=ts_local,
            bids=[(b.price, float(b.quantity)) for b in d.bids],
            asks=[(a.price, float(a.quantity)) for a in d.asks],
            seq=to_v, prev_seq=from_v,
            payload={"from_version": from_v, "to_version": to_v,
                     "event_type": d.event_type},
        )
        if self.on_diff:
            await self.on_diff(diff)

    async def _on_deals(self, w: Any, sym: str,
                        ts_exch: float, ts_local: float) -> None:
        dl = w.public_aggre_deals
        for it in dl.deals:
            try:
                tt = int(it.trade_type)
            except ValueError:
                tt = 0
            side = "buy" if tt == 1 else "sell" if tt == 2 else "unknown"
            t_exch = (it.time / 1000.0) if it.time else ts_exch
            tr = Trade(
                exchange="mexc", symbol=sym, ts_exch=t_exch, ts_local=ts_local,
                price=float(it.price), size=float(it.quantity), side=side,
                payload={"trade_type": tt},
            )
            if self.on_trade:
                await self.on_trade(tr)
