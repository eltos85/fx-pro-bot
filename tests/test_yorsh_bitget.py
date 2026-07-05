"""Юнит-тесты yorsh_bot M2: Bitget JSON-парсинг, seq/pseq, reinit-запрос.

Все цели — чистая детерминированная логика (без сети/WS/биржи). Тестируем
``_on_text``/``_on_books``/``_on_trade`` на синтетических JSON-сообщениях
по структуре из официальной доки Bitget (Depth Channel / Trades Channel).
Live WS/REST в sandbox не проверяются.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from yorsh_bot.exchanges.bitget import BitgetSpotClient
from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade


# ─── helpers ─────────────────────────────────────────────────────────────

class _Collector:
    """Собирает события из колбэков."""
    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self.diffs: list[DepthDiff] = []
        self.snaps: list[BookSnapshot] = []
        self.health: list[tuple[str, str | None]] = []

    async def on_trade(self, t: Trade) -> None: self.trades.append(t)
    async def on_diff(self, d: DepthDiff) -> None: self.diffs.append(d)
    async def on_snap(self, s: BookSnapshot) -> None: self.snaps.append(s)
    async def on_health(self, ev: str, det: str | None) -> None:
        self.health.append((ev, det))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


def _make_client(symbols=("BTCUSDT",), **kw) -> tuple[BitgetSpotClient, _Collector]:
    c = _Collector()
    client = BitgetSpotClient(
        list(symbols), on_trade=c.on_trade, on_diff=c.on_diff,
        on_snapshot=c.on_snap, on_health=c.on_health, **kw)
    return client, c


# ─── constructor / limits ────────────────────────────────────────────────

def test_bitget_constructor_limit():
    # 26 символов × 2 канала = 52 > 50 (recommended) → ValueError
    with pytest.raises(ValueError):
        BitgetSpotClient([f"S{i}USDT" for i in range(26)])
    # 25 × 2 = 50 — ок
    BitgetSpotClient([f"S{i}USDT" for i in range(25)])


# ─── parse: ignore non-data frames ───────────────────────────────────────

def test_bitget_pong_ignored():
    client, c = _make_client()
    asyncio.run(client._on_text("pong"))
    assert c.trades == [] and c.diffs == [] and c.snaps == []


def test_bitget_subscribe_ack_ignored():
    client, c = _make_client()
    asyncio.run(client._on_text(json.dumps({
        "event": "subscribe",
        "arg": {"instType": "SPOT", "channel": "books", "instId": "BTCUSDT"}})))
    assert c.snaps == []


def test_bitget_non_json_ignored():
    client, c = _make_client()
    asyncio.run(client._on_text("not json"))
    assert c.trades == [] and c.diffs == []


# ─── parse: books snapshot / update ──────────────────────────────────────

def test_bitget_books_snapshot():
    """WS books action=snapshot → BookSnapshot source=ws_books, seq из data[0]."""
    client, c = _make_client()
    msg = {
        "action": "snapshot",
        "arg": {"instType": "SPOT", "channel": "books", "instId": "BTCUSDT"},
        "data": [{
            "asks": [["26274.9", "0.0009"], ["26275.0", "0.0500"]],
            "bids": [["26274.8", "0.0009"], ["26274.7", "0.0027"]],
            "seq": 123, "pseq": 0, "ts": "1695710946294",
        }],
        "ts": 1695710946294,
    }
    asyncio.run(client._on_text(json.dumps(msg)))
    assert len(c.snaps) == 1
    snap = c.snaps[0]
    assert snap.exchange == "bitget"
    assert snap.symbol == "BTCUSDT"
    assert snap.source == "ws_books"
    assert snap.seq == 123
    assert snap.bids == [("26274.8", 0.0009), ("26274.7", 0.0027)]
    assert snap.asks == [("26274.9", 0.0009), ("26275.0", 0.0500)]
    assert abs(snap.ts_exch - 1695710946.294) < 1e-3


def test_bitget_books_update():
    """WS books action=update → DepthDiff с seq/pseq из data[0]."""
    client, c = _make_client()
    msg = {
        "action": "update",
        "arg": {"instType": "SPOT", "channel": "books", "instId": "BTCUSDT"},
        "data": [{
            "asks": [["26275.0", "0.0"]],
            "bids": [["26274.8", "1.5"]],
            "seq": 130, "pseq": 129, "ts": "1695710946400",
        }],
        "ts": 1695710946400,
    }
    asyncio.run(client._on_text(json.dumps(msg)))
    assert len(c.diffs) == 1
    d = c.diffs[0]
    assert d.exchange == "bitget"
    assert d.seq == 130 and d.prev_seq == 129
    assert d.bids == [("26274.8", 1.5)]
    assert d.asks == [("26275.0", 0.0)]


def test_bitget_books_empty_data_ignored():
    client, c = _make_client()
    msg = {"action": "snapshot",
           "arg": {"instType": "SPOT", "channel": "books", "instId": "BTCUSDT"},
           "data": [], "ts": 1}
    asyncio.run(client._on_text(json.dumps(msg)))
    assert c.snaps == [] and c.diffs == []


# ─── parse: trade ────────────────────────────────────────────────────────

def test_bitget_trade_snapshot_and_update():
    client, c = _make_client()
    snap_msg = {
        "action": "snapshot",
        "arg": {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"},
        "data": [{"ts": "1695709835822", "price": "26293.4", "size": "0.0013",
                  "side": "buy", "tradeId": "1000000000"}],
        "ts": 1695711090682,
    }
    update_msg = {
        "action": "update",
        "arg": {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"},
        "data": [{"ts": "1695709835900", "price": "26294.0", "size": "0.002",
                  "side": "sell", "tradeId": "1000000001"}],
        "ts": 1695711090700,
    }
    asyncio.run(client._on_text(json.dumps(snap_msg)))
    asyncio.run(client._on_text(json.dumps(update_msg)))
    assert len(c.trades) == 2
    t0, t1 = c.trades
    assert t0.side == "buy" and t0.price == 26293.4 and t0.size == 0.0013
    assert t1.side == "sell" and t1.price == 26294.0
    assert t0.exchange == "bitget" and t0.symbol == "BTCUSDT"
    assert abs(t0.ts_exch - 1695709835.822) < 1e-3


def test_bitget_trade_unknown_side_normalized():
    client, c = _make_client()
    msg = {"action": "update",
           "arg": {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"},
           "data": [{"ts": "1695709835822", "price": "1", "size": "1",
                     "side": "??", "tradeId": "x"}], "ts": 1}
    asyncio.run(client._on_text(json.dumps(msg)))
    assert c.trades[0].side == "unknown"


# ─── request_reinit ──────────────────────────────────────────────────────

def test_bitget_request_reinit():
    client, _ = _make_client()
    client.request_reinit("btcusdt")
    assert "BTCUSDT" in client._resubscribe  # noqa: SLF001
