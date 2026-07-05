"""Юнит-тесты yorsh_bot M1: orderbook (MEXC range-version), recorder, protobuf.

Все цели — чистая детерминированная логика (без сети/WS/биржи).
Формат тестовых diff'ов — по структуре из официальной доки MEXC
(https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/
how-to-properly-maintain-a-local-copy-of-the-order-book).
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
import time

import pytest

from yorsh_bot.data.orderbook import LocalOrderBook
from yorsh_bot.data.recorder import RawRecorder
from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade
from yorsh_bot.exchanges.mexc_pb import _classes


# ─── helpers ─────────────────────────────────────────────────────────────

def _snap(symbol="BTCUSDT", seq=100,
          bids=((30000.0, 1.0),), asks=((30001.0, 2.0),)) -> BookSnapshot:
    return BookSnapshot(exchange="mexc", symbol=symbol, ts_exch=0.0,
                       ts_local=0.0,
                       bids=list(bids), asks=list(asks), seq=seq)


def _diff(symbol="BTCUSDT", from_v=101, to_v=101,
          bids=(), asks=(), ts_local=0.0) -> DepthDiff:
    # MEXC: prev_seq=from_version, seq=to_version
    return DepthDiff(exchange="mexc", symbol=symbol, ts_exch=ts_local,
                    ts_local=ts_local,
                    bids=list(bids), asks=list(asks),
                    seq=to_v, prev_seq=from_v)


# ─── orderbook: MEXC range-version procedure ─────────────────────────────

def test_orderbook_apply_snapshot_then_diff():
    ob = LocalOrderBook("mexc", "BTCUSDT", version_mode="range")
    ob.apply_snapshot(_snap(seq=100,
                           bids=((30000.0, 1.0), (29999.0, 0.5)),
                           asks=((30001.0, 2.0),)))
    assert ob.last_version == 100
    assert ob.best_bid_ask() == (30000.0, 30001.0)
    # next diff: from_version == last+1 == 101
    applied = ob.apply_diff(_diff(from_v=101, to_v=101,
                                 asks=((30001.0, 0.0),)))  # remove ask level
    assert applied is True
    assert 30001.0 not in ob.asks
    assert ob.last_version == 101


def test_orderbook_needs_reinit_on_gap():
    """MEXC step 5: from_version > last_version + 1 → discontinuous → reinit."""
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_snapshot(_snap(seq=100))
    # from_version=105, last=100 → 105 > 101 → reinit
    assert ob.needs_reinit(_diff(from_v=105, to_v=105)) is True
    # apply_diff при needs_reinit — не применяет
    applied = ob.apply_diff(_diff(from_v=105, to_v=105,
                                 asks=((30001.0, 0.0),)))
    assert applied is False
    assert ob.last_version == 100   # не сдвинулся


def test_orderbook_stale_diff_skipped():
    """MEXC step 4: to_version <= last_version → устаревший, пропускаем (не reinit)."""
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_snapshot(_snap(seq=100))
    assert ob.needs_reinit(_diff(from_v=99, to_v=100)) is False  # to<=last
    applied = ob.apply_diff(_diff(from_v=99, to_v=100,
                                 asks=((30001.0, 5.0),)))
    assert applied is False
    assert ob.asks[30001.0] == 2.0   # не перезаписан устаревшим


def test_orderbook_sequential_versions():
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_snapshot(_snap(seq=100, bids=((100.0, 1.0),)),
                     )
    ob.apply_diff(_diff(from_v=101, to_v=101, bids=((100.0, 2.0),)))
    ob.apply_diff(_diff(from_v=102, to_v=102, bids=((100.0, 3.0),)))
    assert ob.bids[100.0] == 3.0
    assert ob.last_version == 102
    # gap
    assert ob.needs_reinit(_diff(from_v=110, to_v=110)) is True


def test_orderbook_size_zero_removes_level():
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_snapshot(_snap(seq=100, bids=((100.0, 1.0), (99.0, 2.0))))
    ob.apply_diff(_diff(from_v=101, to_v=101, bids=((100.0, 0.0),)))
    assert 100.0 not in ob.bids
    assert ob.bids[99.0] == 2.0


def test_orderbook_pre_snapshot_buffer():
    """MEXC step 1-2: диффы до snapshot кешируются, применяются после."""
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_diff(_diff(from_v=98, to_v=99,
                       bids=((100.0, 5.0),)))  # буфер
    assert ob.last_version is None
    assert ob.best_bid_ask() == (None, None)
    # snapshot с last_update_id=99 → buffered diff (to=99) устаревший, пропустится
    ob.apply_snapshot(_snap(seq=99, bids=((100.0, 1.0),)))
    assert ob.bids[100.0] == 1.0   # из snapshot, не из устаревшего буфера


def test_orderbook_snapshot_drops_zero_size_levels():
    ob = LocalOrderBook("mexc", "BTCUSDT")
    ob.apply_snapshot(_snap(seq=100, bids=((100.0, 0.0), (99.0, 2.0))))
    assert 100.0 not in ob.bids
    assert ob.bids[99.0] == 2.0


def test_orderbook_seq_mode_param():
    """Bitget seq-режим (M2): prev_seq=pseq, seq=seq; gap = pseq != last."""
    ob = LocalOrderBook("bitget", "BTCUSDT", version_mode="seq")
    ob.apply_snapshot(_snap(seq=10))
    # pseq=10 == last → ok
    assert ob.needs_reinit(_diff(from_v=10, to_v=11)) is False
    # pseq=9 != last=10 → gap
    assert ob.needs_reinit(_diff(from_v=9, to_v=11)) is True


# ─── recorder: partitioning / rotation / retention / cap ─────────────────

def test_recorder_partition_path_and_write():
    with tempfile.TemporaryDirectory() as d:
        rec = RawRecorder(d, exchange="mexc", retention_days=30,
                         max_gb=20.0)
        ts = time.time()   # свежий → не попадёт под retention
        gm = time.gmtime(ts)
        date = f"{gm.tm_year:04d}-{gm.tm_mon:02d}-{gm.tm_mday:02d}"
        hour = f"{gm.tm_hour:02d}"
        rec.write_trade(Trade(exchange="mexc", symbol="BTCUSDT",
                             ts_exch=ts, ts_local=ts, price=100.0,
                             size=0.5, side="buy"))
        rec.write_diff(_diff(symbol="BTCUSDT", from_v=1, to_v=1,
                            bids=((100.0, 1.0),), ts_local=ts))
        rec.close()
        path = os.path.join(d, "raw", "mexc", "BTCUSDT", date,
                           f"{hour}.jsonl.gz")
        assert os.path.exists(path), f"expected {path}"
        with gzip.open(path, "rt") as f:
            lines = [json.loads(l) for l in f]
        assert len(lines) == 2
        assert lines[0]["type"] == "trade"
        assert lines[1]["type"] == "diff"
        assert lines[0]["symbol"] == "BTCUSDT"
        assert lines[0]["price"] == 100.0


def test_recorder_hourly_rotation():
    with tempfile.TemporaryDirectory() as d:
        rec = RawRecorder(d, exchange="mexc")
        # ts1: 2026-07-05 10:30 UTC, ts2: 11:45 UTC
        ts1 = 1783294200.0  # approx; проверим по gmtime
        gm1 = time.gmtime(ts1)
        ts2 = ts1 + 4500   # +1h15m → след. час
        gm2 = time.gmtime(ts2)
        assert (gm1.tm_hour != gm2.tm_hour) or (gm1.tm_mday != gm2.tm_mday)
        rec.write_trade(Trade(exchange="mexc", symbol="ABCUSDT",
                             ts_exch=ts1, ts_local=ts1, price=1, size=1,
                             side="buy"))
        rec.write_trade(Trade(exchange="mexc", symbol="ABCUSDT",
                             ts_exch=ts2, ts_local=ts2, price=2, size=2,
                             side="sell"))
        rec.close()
        date1 = f"{gm1.tm_year:04d}-{gm1.tm_mon:02d}-{gm1.tm_mday:02d}"
        date2 = f"{gm2.tm_year:04d}-{gm2.tm_mon:02d}-{gm2.tm_mday:02d}"
        p1 = os.path.join(d, "raw", "mexc", "ABCUSDT", date1,
                         f"{gm1.tm_hour:02d}.jsonl.gz")
        p2 = os.path.join(d, "raw", "mexc", "ABCUSDT", date2,
                         f"{gm2.tm_hour:02d}.jsonl.gz")
        assert os.path.exists(p1) and os.path.exists(p2)


def test_recorder_retention_removes_old_partitions():
    with tempfile.TemporaryDirectory() as d:
        rec = RawRecorder(d, exchange="mexc", retention_days=7, max_gb=20.0)
        events = []
        rec._health = lambda ev, det: events.append((ev, det))  # noqa: SLF001
        # создадим старую партицию (30 дней назад) вручную
        old_ts = time.time() - 30 * 86400
        gm = time.gmtime(old_ts)
        date = f"{gm.tm_year:04d}-{gm.tm_mon:02d}-{gm.tm_mday:02d}"
        old_dir = os.path.join(d, "raw", "mexc", "OLDUSDT", date)
        os.makedirs(old_dir)
        with gzip.open(os.path.join(old_dir, "00.jsonl.gz"), "wt") as f:
            f.write('{"x":1}\n')
        # свежая запись → триггерит retention на _rotate
        rec.write_trade(Trade(exchange="mexc", symbol="NEWUSDT",
                             ts_exch=time.time(), ts_local=time.time(),
                             price=1, size=1, side="buy"))
        rec.close()
        assert not os.path.exists(old_dir), "old partition must be deleted"
        assert any(ev == "retention" for ev, _ in events)


def test_recorder_cap_removes_oldest_when_over_limit():
    with tempfile.TemporaryDirectory() as d:
        # cap = 0.000001 GB ≈ 1KB → любая запись переполнит
        rec = RawRecorder(d, exchange="mexc", retention_days=365,
                         max_gb=0.000001)
        events = []
        rec._health = lambda ev, det: events.append((ev, det))  # noqa: SLF001
        # старая партиция с большим файлом
        old_ts = time.time() - 10 * 86400
        gm = time.gmtime(old_ts)
        date_old = f"{gm.tm_year:04d}-{gm.tm_mon:02d}-{gm.tm_mday:02d}"
        old_dir = os.path.join(d, "raw", "mexc", "BIGUSDT", date_old)
        os.makedirs(old_dir)
        with gzip.open(os.path.join(old_dir, "00.jsonl.gz"), "wb") as f:
            f.write(os.urandom(5000))   # несжимаемые → > cap после gzip
        # свежая запись (другая дата) → триггерит cap-проверку
        rec.write_trade(Trade(exchange="mexc", symbol="CAPUSDT",
                             ts_exch=time.time(), ts_local=time.time(),
                             price=1, size=1, side="buy"))
        rec.close()
        # старая партиция удалена под cap
        assert not os.path.exists(old_dir)


# ─── protobuf round-trip (MEXC schema) ───────────────────────────────────

def test_pb_depth_roundtrip():
    cls = _classes()
    W = cls["PushDataV3ApiWrapper"]
    Depths = cls["PublicAggreDepthsV3Api"]
    d = Depths()
    d.from_version = "12345"
    d.to_version = "12346"
    d.event_type = "update"
    a = d.asks.add(); a.price = "30000.50"; a.quantity = "0.1"
    b = d.bids.add(); b.price = "30000.00"; b.quantity = "0.2"
    w = W()
    w.channel = "spot@public.aggre.depth.v3.api.pb@100ms@BTCUSDT"
    w.symbol = "BTCUSDT"
    w.send_time = 1778158574304
    w.public_aggre_depths.CopyFrom(d)
    w2 = W.FromString(w.SerializeToString())
    assert w2.WhichOneof("body") == "public_aggre_depths"
    assert w2.symbol == "BTCUSDT"
    d2 = w2.public_aggre_depths
    assert d2.from_version == "12345"
    assert d2.to_version == "12346"
    assert [(a.price, a.quantity) for a in d2.asks] == [("30000.50", "0.1")]
    assert [(b.price, b.quantity) for b in d2.bids] == [("30000.00", "0.2")]


def test_pb_deals_roundtrip():
    cls = _classes()
    W = cls["PushDataV3ApiWrapper"]
    Deals = cls["PublicAggreDealsV3Api"]
    dl = Deals()
    dl.event_type = "spot@public.aggre.deals.v3.api.pb@100ms"
    it = dl.deals.add()
    it.price = "94.75"; it.quantity = "0.108"; it.trade_type = 2
    it.time = 1778158574194
    w = W(); w.symbol = "MXUSDT"; w.public_aggre_deals.CopyFrom(dl)
    w2 = W.FromString(w.SerializeToString())
    assert w2.WhichOneof("body") == "public_aggre_deals"
    deal = w2.public_aggre_deals.deals[0]
    assert deal.price == "94.75"
    assert deal.trade_type == 2
    assert deal.time == 1778158574194


def test_pb_empty_body_parses():
    """Обёртка без body (ACK/pong) — парсится, oneof=None."""
    cls = _classes()
    W = cls["PushDataV3ApiWrapper"]
    w = W(); w.channel = "ack"
    w2 = W.FromString(w.SerializeToString())
    assert w2.WhichOneof("body") is None
    assert w2.channel == "ack"
