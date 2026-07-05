"""Юнит-тесты yorsh_bot M5: prints + scanner + daily report.

Synthetic trades/spurts/densities. Повторяемость против Пуассон-нуля,
кластеризация принтов, привязка к density, daily-сводка.
"""
from __future__ import annotations

import datetime as dt

import pytest

from yorsh_bot.analysis.prints import (
    Spurt, SpurtDetector, cluster_prints_by_size,
)
from yorsh_bot.analysis.yorsh_scanner import (
    YorshScanner, chi2_cdf, repeat_frequency_pvalue,
)
from yorsh_bot.config.settings import YorshSettings
from yorsh_bot.exchanges.base import Trade
from yorsh_bot.report.daily import build_report
from yorsh_bot.state.db import YorshDB


def _trade(ts, price, size, side="buy"):
    return Trade("mexc", "TEST", ts_exch=ts, ts_local=ts,
                 price=price, size=size, side=side)


# ─── chi2 / repeat-frequency ─────────────────────────────────────────────

def test_chi2_cdf_known_values():
    # chi2(2) CDF at 2.0 ≈ 0.264 (exponential with mean 2)
    assert abs(chi2_cdf(2.0, 2) - (1 - pow(0.367879, 1))) < 0.01
    # CDF(0) = 0, CDF(large) → 1
    assert chi2_cdf(0.0, 5) == 0.0
    assert chi2_cdf(100.0, 5) > 0.999


def test_repeat_frequency_regular_series_low_p():
    """Равноотстоящие прострелы → интервалы const → var≈0 → p<0.05 (regular)."""
    ts = [0, 10, 20, 30, 40]
    p = repeat_frequency_pvalue(ts)
    assert p is not None
    assert p < 0.05


def test_repeat_frequency_poisson_series_high_p():
    """Нерегулярные (пуассон-подобные) интервалы → p>0.05 (не regular)."""
    # интервалы [1,5,2,8,3,7] — дисперсия >> mean → Q right-tail → p~0.9
    ts = [0, 1, 6, 8, 16, 19, 26]
    p = repeat_frequency_pvalue(ts)
    assert p is not None
    assert p > 0.05


def test_repeat_frequency_too_few_returns_none():
    assert repeat_frequency_pvalue([0, 1, 2]) is None   # 2 интервала < 3


# ─── cluster_prints_by_size ──────────────────────────────────────────────

def test_cluster_prints_groups_same_size():
    prints = [_trade(0, 100, 10), _trade(1, 100, 11), _trade(2, 100, 50)]
    clusters = cluster_prints_by_size(prints)
    assert len(clusters) == 2
    # кластер «одинаковых» — 10 и 11 (в пределах 20%)
    same = [c for c in clusters if any(p.size == 10 for p in c)]
    assert len(same[0]) == 2


def test_cluster_prints_empty():
    assert cluster_prints_by_size([]) == []


# ─── SpurtDetector ───────────────────────────────────────────────────────

def test_spurt_detector_emits_on_amplitude():
    out: list[Spurt] = []
    s = YorshSettings(spurt_min_amplitude_pct=2.0)
    det = SpurtDetector("mexc", "TEST", s, on_spurt=out.append, window_ms=60_000)
    # rising buy-prints 100 → 102.5 (2.5%)
    for i, px in enumerate([100.0, 100.5, 101.2, 102.5]):
        det.apply_trade(_trade(i, px, 5.0, side="buy"))
    assert len(out) == 1
    sp = out[0]
    assert sp.direction == "up"
    assert sp.amplitude_pct >= 2.0
    assert sp.trigger_prints   # all buys
    assert sp.trigger_cluster_size == 5.0


def test_spurt_detector_no_emit_below_amplitude():
    out: list[Spurt] = []
    s = YorshSettings(spurt_min_amplitude_pct=2.0)
    det = SpurtDetector("mexc", "TEST", s, on_spurt=out.append, window_ms=60_000)
    for i, px in enumerate([100.0, 100.3, 100.8, 101.0]):   # 1% < 2%
        det.apply_trade(_trade(i, px, 5.0, side="buy"))
    assert out == []


def test_spurt_detector_down_direction():
    out: list[Spurt] = []
    s = YorshSettings(spurt_min_amplitude_pct=2.0)
    det = SpurtDetector("mexc", "TEST", s, on_spurt=out.append, window_ms=60_000)
    for i, px in enumerate([100.0, 99.0, 98.0, 97.5]):   # -2.5%
        det.apply_trade(_trade(i, px, 5.0, side="sell"))
    assert len(out) == 1
    assert out[0].direction == "down"


# ─── YorshScanner full evaluate ─────────────────────────────────────────

def _seed_density(db, exchange, symbol, price, t0, t1, verdict="genuine"):
    return db.insert_density(
        exchange=exchange, symbol=symbol, side="ask", price=price,
        first_seen=t0, last_seen=t1, peak_size=50.0, verdict=verdict,
        persistence_sec=120.0, partial_fill_vol=10.0)


def test_scanner_passes_and_writes_candidate(tmp_path):
    db = YorshDB(str(tmp_path))
    # density активна на момент прострелов, рядом по цене
    _seed_density(db, "mexc", "ABC", price=100.0, t0=0.0, t1=200.0)
    s = YorshSettings()
    # 5 прострелов равноотстоящих (regular), триггеры одного размера
    spurts = []
    for i in range(5):
        sp = Spurt("mexc", "ABC", ts=10 + i * 10, direction="up",
                   amplitude_pct=3.0, duration_ms=500,
                   trigger_prints=[_trade(10 + i * 10, 100.0, 7.0, "buy")],
                   trigger_cluster_size=7.0,
                   start_price=100.0, end_price=103.0)
        spurts.append(sp)
    sc = YorshScanner(db)
    res = sc.evaluate("mexc", "ABC", spurts, day_span_sec=100.0)
    assert res.passed
    assert res.regularity_pvalue is not None and res.regularity_pvalue < 0.05
    # кандидат записан
    cands = db.active_candidates()
    assert len(cands) == 1
    assert cands[0]["symbol"] == "ABC"
    # все прострелы записаны в spurt_events
    rows = db.conn.execute("SELECT * FROM spurt_events WHERE symbol='ABC'").fetchall()
    assert len(rows) == 5
    assert all(r["passed_filters"] == 1 for r in rows)
    assert rows[0]["density_id"] is not None


def test_scanner_fails_without_density(tmp_path):
    db = YorshDB(str(tmp_path))
    spurts = [Spurt("mexc", "XYZ", ts=10 + i * 10, direction="up",
                    amplitude_pct=3.0, duration_ms=500,
                    trigger_prints=[_trade(10 + i * 10, 100.0, 7.0, "buy")],
                    trigger_cluster_size=7.0,
                    start_price=100.0, end_price=103.0)
              for i in range(5)]
    sc = YorshScanner(db)
    res = sc.evaluate("mexc", "XYZ", spurts, day_span_sec=100.0)
    assert not res.passed   # нет density → fail
    # но прострелы всё равно записаны
    rows = db.conn.execute("SELECT passed_filters FROM spurt_events "
                          "WHERE symbol='XYZ'").fetchall()
    assert len(rows) == 5 and all(r["passed_filters"] == 0 for r in rows)
    assert db.active_candidates() == []


def test_scanner_fails_without_regularity(tmp_path):
    """Пуассон-подобные интервалы → p>0.05 → fail (даже с density)."""
    db = YorshDB(str(tmp_path))
    _seed_density(db, "mexc", "QQQ", price=100.0, t0=0.0, t1=200.0)
    ts_list = [0, 1, 6, 8, 16, 19, 26]   # нерегулярно
    spurts = [Spurt("mexc", "QQQ", ts=t, direction="up", amplitude_pct=3.0,
                    duration_ms=500,
                    trigger_prints=[_trade(t, 100.0, 7.0, "buy")],
                    trigger_cluster_size=7.0,
                    start_price=100.0, end_price=103.0) for t in ts_list]
    sc = YorshScanner(db)
    res = sc.evaluate("mexc", "QQQ", spurts, day_span_sec=30.0)
    assert not res.passed
    assert res.regularity_pvalue is not None and res.regularity_pvalue > 0.05


# ─── daily report ────────────────────────────────────────────────────────

def test_daily_report_seeded(tmp_path):
    db = YorshDB(str(tmp_path))
    # один passed-кандидат + пару прострелов за «вчера»
    today = dt.date.today()
    yesterday = today - dt.timedelta(days=1)
    y_start = dt.datetime.combine(yesterday, dt.time.min,
                                  tzinfo=dt.timezone.utc).timestamp()
    db.upsert_candidate(exchange="mexc", symbol="ABC",
                        first_detected=y_start + 10,
                        last_detected=y_start + 20,
                        spurts_per_day=5.0, regularity_pvalue=0.01,
                        print_cluster_size=7.0)
    db.insert_spurt(exchange="mexc", symbol="ABC", ts=y_start + 10,
                    direction="up", amplitude_pct=3.0, duration_ms=500,
                    trigger_print_size=7.0, passed_filters=1)
    db.insert_spurt(exchange="mexc", symbol="ABC", ts=y_start + 20,
                    direction="up", amplitude_pct=2.5, duration_ms=400,
                    trigger_print_size=7.0, passed_filters=1,
                    revert_ms=1500)
    report = build_report(db, notional_usd=10.0)
    assert "ёрш daily report" in report
    assert "ABC" in report
    assert "UPPER BOUND" in report
    # upper bound = (3.0 + 2.5)/100 * 10 = 0.55
    assert "$0.55" in report
    assert "median amplitude" in report
    assert "median revert_ms" in report
