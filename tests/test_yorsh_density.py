"""Юнит-тесты yorsh_bot M4: density-tracker (genuine/spoof/iceberg/move).

Synthetic L2-diff + trade последовательности. Инфраструктурные тесты
механики фильтра (positive/negative жизненные циклы), НЕ подгонка стратегии
(раздел 6 ТЗ). Пороги — из YorshSettings defaults (стартовые точки).
"""
from __future__ import annotations

import time

import pytest

from yorsh_bot.analysis.density import (
    DensityEvent, DensityTracker, make_db_persistor,
)
from yorsh_bot.config.settings import YorshSettings
from yorsh_bot.exchanges.base import DepthDiff, Trade
from yorsh_bot.state.db import YorshDB


def _settings(**over):
    base = dict(density_kratnosti=5.0, density_min_persistence_sec=60.0,
                density_refill_window_sec=30.0, density_move_window_sec=10.0,
                density_approach_ticks=5.0, density_mismatch_ratio=3.0,
                density_gap_close_sec=30.0)
    base.update(over)
    return YorshSettings(**base)


def _diff(ts, bids=(), asks=()):
    return DepthDiff("mexc", "TEST", ts_exch=ts, ts_local=ts,
                     bids=list(bids), asks=list(asks))


def _trade(ts, price, size, side="buy"):
    return Trade("mexc", "TEST", ts_exch=ts, ts_local=ts,
                 price=price, size=size, side=side)


class _Collector:
    """Собирает DensityEvent'ы; фиксирует финальный вердикт close."""
    def __init__(self):
        self.events: list[DensityEvent] = []
        self.closes: list[DensityEvent] = []
    def __call__(self, ev: DensityEvent):
        self.events.append(ev)
        if ev.kind == "close":
            self.closes.append(ev)


def _seed_book(tr, ts, bid_levels, ask_levels, best_bid, best_ask):
    """Заполнить книгу фоновыми уровнями (3 уровня на сторону, размер 1)."""
    bids = [(p, 1.0) for p in bid_levels]
    asks = [(p, 1.0) for p in ask_levels]
    tr.apply_diff(_diff(ts, bids=bids, asks=asks), best_bid, best_ask)


# ─── genuine ─────────────────────────────────────────────────────────────

def test_genuine_persistence_partial_fills_no_move():
    """Плотность стоит >60с, partial fills, не переставляется → genuine.

    Сценарий: ask-стенка 100.50, цена подходит, partial fill (стенка
    остаётся >0), цена откатывается далеко, стенка снимается БЕЗ pull
    (best_ask далеко → не spoof). persistence 75с > 60 → genuine.
    """
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01,
                        on_event=_Collector())
    col = tr._on_event
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    # крупная ask-стенка 100.50 размер 50
    tr.apply_diff(_diff(t0, asks=[(100.50, 50.0)]),
                  best_bid=100.02, best_ask=100.50)
    # цена подошла, partial fill (buy-trade ест ask 100.50), стенка убывает до 40
    tr.apply_trade(_trade(t0 + 30, 100.50, 10.0, side="buy"))
    tr.apply_diff(_diff(t0 + 30, asks=[(100.50, 40.0)]),
                  best_bid=100.49, best_ask=100.50)
    # цена откатилась далеко от стенки
    tr.apply_diff(_diff(t0 + 70, bids=[(100.49, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    # стенка снимается, когда цена ДАЛЕКО (не pull)
    tr.apply_diff(_diff(t0 + 75, asks=[(100.50, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    tr.flush(t0 + 75 + 31)
    verdicts = [c.verdict for c in col.closes]
    assert "genuine" in verdicts, f"closes={verdicts}"


# ─── spoof (pull при подходе цены) ───────────────────────────────────────

def test_spoof_pull_when_price_approaches():
    """Плотность снялась, когда best-price подошла вплотную → spoof."""
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01, on_event=_Collector())
    col = tr._on_event
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    # крупный bid 100.05
    tr.apply_diff(_diff(t0, bids=[(100.05, 50.0)]),
                  best_bid=100.05, best_ask=100.10)
    # цена поднимается к 100.05 (best_bid 100.04 → 100.045, в 5 тиков)
    tr.apply_diff(_diff(t0 + 2, bids=[(100.04, 0.0), (100.045, 1.0)]),
                  best_bid=100.045, best_ask=100.10)
    # плотность снимается при подходе
    tr.apply_diff(_diff(t0 + 3, bids=[(100.05, 0.0)]),
                  best_bid=100.045, best_ask=100.10)
    tr.flush(t0 + 3 + 31)
    closes = col.closes
    assert closes, "no close event"
    assert closes[-1].verdict == "spoof"
    assert closes[-1].pull_count >= 1


# ─── iceberg (refill) ────────────────────────────────────────────────────

def test_iceberg_refill_after_partial_fill():
    """Размер восстанавливается после fill → iceberg."""
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01, on_event=_Collector())
    col = tr._on_event
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    tr.apply_diff(_diff(t0, asks=[(100.20, 50.0)]),
                  best_bid=100.02, best_ask=100.20)
    # partial fill (buy-trade ест ask 100.20) + размер убывает
    tr.apply_trade(_trade(t0 + 5, 100.20, 10.0, side="buy"))
    tr.apply_diff(_diff(t0 + 5, asks=[(100.20, 40.0)]),
                  best_bid=100.02, best_ask=100.20)
    # размер ушёл → vanish
    tr.apply_diff(_diff(t0 + 6, asks=[(100.20, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    # refill: тот же уровень возвращается в окне
    tr.apply_diff(_diff(t0 + 10, asks=[(100.20, 50.0)]),
                  best_bid=100.02, best_ask=100.20)
    # закрываем
    tr.apply_diff(_diff(t0 + 50, asks=[(100.20, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    tr.flush(t0 + 50 + 31)
    closes = col.closes
    assert closes
    assert closes[-1].verdict == "iceberg"
    assert closes[-1].refilled >= 1


def test_iceberg_volume_depth_mismatch():
    """Cumulative traded >> visible peak → iceberg (без refill)."""
    s = _settings(density_gap_close_sec=5.0)
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01, on_event=_Collector())
    col = tr._on_event
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    tr.apply_diff(_diff(t0, asks=[(100.20, 10.0)]),
                  best_bid=100.02, best_ask=100.20)
    # много fills суммарно >> visible (10): 5 трейдов по 10 = 50 (5× peak)
    for i in range(5):
        tr.apply_trade(_trade(t0 + 1 + i, 100.20, 10.0, side="buy"))
    # уровень убывает/снимается (цена НЕ подошла, чтобы не стать spoof)
    tr.apply_diff(_diff(t0 + 3, asks=[(100.20, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    tr.flush(t0 + 3 + 6)
    closes = col.closes
    assert closes
    assert closes[-1].verdict == "iceberg"
    assert closes[-1].partial_fill_vol >= 50.0


# ─── move (прыгает по уровням) ───────────────────────────────────────────

def test_spoof_move_jumps_between_prices():
    """Плотность пропала и всплыла на новой цене в move_window → moved → spoof."""
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01,
                        on_event=_Collector())
    col = tr._on_event
    t0 = 1000.0
    _seed_book(tr, t0, [99.00, 99.01, 99.02], [100.10, 100.11, 100.12],
               best_bid=99.02, best_ask=100.10)
    # крупный bid 99.50 (между фоном и asks, далеко от best_bid=99.02)
    tr.apply_diff(_diff(t0, bids=[(99.50, 50.0)]),
                  best_bid=99.50, best_ask=100.10)
    # снялся, цена далеко (99.02 vs 99.50 = 48 тиков > 5 → не pull)
    tr.apply_diff(_diff(t0 + 1, bids=[(99.50, 0.0)]),
                  best_bid=99.02, best_ask=100.10)
    # всплыла на 99.60 в течение move_window → move
    tr.apply_diff(_diff(t0 + 2, bids=[(99.60, 50.0)]),
                  best_bid=99.60, best_ask=100.10)
    # закрываем новую
    tr.apply_diff(_diff(t0 + 3, bids=[(99.60, 0.0)]),
                  best_bid=99.02, best_ask=100.10)
    tr.flush(t0 + 3 + 6)
    closes = col.closes
    # хотя бы одна плотность маркирована moved → spoof
    moved = [c for c in closes if c.moved > 0]
    assert moved, f"no moved density: {[(c.verdict, c.moved) for c in closes]}"
    assert all(c.verdict == "spoof" for c in moved)


# ─── DB-персистор ────────────────────────────────────────────────────────

def test_db_persistor_insert_update_close(tmp_path):
    db = YorshDB(str(tmp_path))
    persistor = make_db_persistor(db)
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01, on_event=persistor)
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    tr.apply_diff(_diff(t0, asks=[(100.50, 50.0)]),
                  best_bid=100.02, best_ask=100.50)
    tr.apply_trade(_trade(t0 + 30, 100.50, 10.0, side="buy"))
    tr.apply_diff(_diff(t0 + 30, asks=[(100.50, 40.0)]),
                  best_bid=100.49, best_ask=100.50)
    tr.apply_diff(_diff(t0 + 70, bids=[(100.49, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    tr.apply_diff(_diff(t0 + 75, asks=[(100.50, 0.0)]),
                  best_bid=100.02, best_ask=100.10)
    tr.flush(t0 + 75 + 31)
    rows = db.conn.execute(
        "SELECT verdict, peak_size, partial_fill_vol, pull_count, moved "
        "FROM densities WHERE symbol='TEST'").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "genuine"
    assert rows[0]["peak_size"] == 50.0
    assert rows[0]["partial_fill_vol"] == 10.0


def test_db_persistor_spoof_records_pull(tmp_path):
    db = YorshDB(str(tmp_path))
    persistor = make_db_persistor(db)
    s = _settings()
    tr = DensityTracker("mexc", "TEST", s, tick_size=0.01, on_event=persistor)
    t0 = 1000.0
    _seed_book(tr, t0, [100.00, 100.01, 100.02], [100.10, 100.11, 100.12],
               best_bid=100.02, best_ask=100.10)
    tr.apply_diff(_diff(t0, bids=[(100.05, 50.0)]),
                  best_bid=100.05, best_ask=100.10)
    tr.apply_diff(_diff(t0 + 2, bids=[(100.04, 0.0), (100.045, 1.0)]),
                  best_bid=100.045, best_ask=100.10)
    tr.apply_diff(_diff(t0 + 3, bids=[(100.05, 0.0)]),
                  best_bid=100.045, best_ask=100.10)
    tr.flush(t0 + 3 + 31)
    row = db.conn.execute(
        "SELECT verdict, pull_count FROM densities WHERE symbol='TEST'").fetchone()
    assert row["verdict"] == "spoof"
    assert row["pull_count"] >= 1
