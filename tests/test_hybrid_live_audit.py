"""Тесты аудита живого hybrid_bot: арифметика бенчмарка, сопоставление причин,
накопитель снапшотов. Сети и ключей не требуют.

Проверяется то, что молча соврало бы в цифрах: пропуск funding-записей, выбор
первого лота для холда, окно сопоставления с БД, порог гейта §8.1.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sqlite3

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" \
    / "hybrid_live_audit.py"
_spec = importlib.util.spec_from_file_location("hybrid_live_audit", _PATH)
assert _spec and _spec.loader
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _fill(ts: int, side: str, qty: float, px: float, fee: float = 0.0,
          etype: str = "Trade") -> dict:
    return {"execTime": str(ts), "side": side, "execQty": str(qty),
            "execPrice": str(px), "execFee": str(fee), "execType": etype,
            "execValue": str(qty * px), "orderLinkId": "hybrid_x"}


# ─── бенчмарк холда ──────────────────────────────────────────────────────

def test_hold_takes_the_first_buy_not_the_cheapest():
    """Холд — это «купил и держу», поэтому берётся первая по времени покупка."""
    fills = [_fill(2_000, "Buy", 0.08, 2000.0, 0.088),
             _fill(3_000, "Buy", 0.08, 1900.0, 0.083)]
    hold = audit.hold_benchmark(fills, [], mark=2100.0)
    assert hold is not None
    assert hold["entry"] == pytest.approx(2000.0)
    assert hold["gross"] == pytest.approx((2100.0 - 2000.0) * 0.08)
    assert hold["total"] == pytest.approx(8.0 - 0.088)


def test_hold_uses_the_real_entry_fee_when_available():
    fills = [_fill(1_000, "Buy", 1.0, 100.0, fee=0.07)]
    hold = audit.hold_benchmark(fills, [], mark=110.0)
    assert hold["fee"] == pytest.approx(0.07)


def test_hold_falls_back_to_taker_when_fee_is_missing():
    fills = [_fill(1_000, "Buy", 1.0, 100.0, fee=0.0)]
    hold = audit.hold_benchmark(fills, [], mark=110.0)
    assert hold["fee"] == pytest.approx(100.0 * audit.TAKER_FEE)


def test_hold_pays_funding_on_the_full_lot():
    """Холд держит лот всё время, поэтому funding считается по его объёму."""
    fills = [_fill(1_000, "Buy", 1.0, 100.0, fee=0.05)]
    fundings = [{"execTime": "2000", "execFee": "0.02", "execValue": "20.0",
                 "markPrice": "100.0", "execType": "Funding"}]
    hold = audit.hold_benchmark(fills, fundings, mark=110.0)
    # ставка = 0.02/20 = 0.001, на лоте 1.0 по цене 100 → 0.1
    assert hold["funding"] == pytest.approx(0.1)
    assert hold["total"] == pytest.approx(10.0 - 0.05 - 0.1)


def test_funding_before_the_entry_is_not_charged_to_hold():
    fills = [_fill(5_000, "Buy", 1.0, 100.0, fee=0.05)]
    fundings = [{"execTime": "1000", "execFee": "0.02", "execValue": "20.0",
                 "markPrice": "100.0", "execType": "Funding"}]
    hold = audit.hold_benchmark(fills, fundings, mark=100.0)
    assert hold["funding"] == pytest.approx(0.0)


def test_no_benchmark_without_a_buy():
    assert audit.hold_benchmark([_fill(1, "Sell", 1.0, 100.0)], [],
                                mark=100.0) is None
    assert audit.hold_benchmark([], [], mark=100.0) is None


def test_no_benchmark_without_a_mark_price():
    fills = [_fill(1_000, "Buy", 1.0, 100.0)]
    assert audit.hold_benchmark(fills, [], mark=0.0) is None


# ─── причины закрытий из БД ──────────────────────────────────────────────

def _make_db(path: str, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts_open REAL, ts_close REAL, symbol TEXT, side TEXT, "
                "qty REAL, entry REAL, exit REAL, sl REAL, tp REAL, "
                "score INTEGER, reasons TEXT, mode TEXT, strategy TEXT, "
                "status TEXT, pnl_usd REAL, fees_usd REAL, close_reason TEXT)")
    for ts_close, qty, pnl, reason in rows:
        con.execute("INSERT INTO trades (ts_open, ts_close, symbol, side, qty, "
                    "entry, exit, sl, tp, score, reasons, mode, strategy, "
                    "status, pnl_usd, fees_usd, close_reason) VALUES "
                    "(?,?,'ETHUSDT','long',?,2000,2120,2000,0,0,?, 'live',"
                    "'hybrid_fix_from_avg','closed',?,0.18,?)",
                    (ts_close - 3600, ts_close, qty, reason, pnl, reason))
    con.commit()
    con.close()


def test_reason_comes_from_the_nearest_record(tmp_path):
    path = str(tmp_path / "hybrid_bot.sqlite")
    _make_db(path, [(1_000_000.0, 0.08, 9.4, "fix_threshold"),
                    (1_000_600.0, 0.08, -3.1, "trend_flat")])
    rows = audit.db_reasons(path, "ETHUSDT", 0.0)
    assert len(rows) == 2
    assert audit.match_reason(1_000_010_000, 0.08, rows) == "fix_threshold"
    assert audit.match_reason(1_000_590_000, 0.08, rows) == "trend_flat"


def test_reason_is_unknown_when_nothing_is_close_enough(tmp_path):
    path = str(tmp_path / "hybrid_bot.sqlite")
    _make_db(path, [(1_000_000.0, 0.08, 9.4, "fix_threshold")])
    rows = audit.db_reasons(path, "ETHUSDT", 0.0)
    far_ms = int((1_000_000.0 + audit.MATCH_WINDOW_SEC + 60) * 1000)
    assert audit.match_reason(far_ms, 0.08, rows) == "?"


def test_reason_ignores_records_with_another_volume(tmp_path):
    path = str(tmp_path / "hybrid_bot.sqlite")
    _make_db(path, [(1_000_000.0, 0.08, 9.4, "fix_threshold")])
    rows = audit.db_reasons(path, "ETHUSDT", 0.0)
    assert audit.match_reason(1_000_010_000, 0.50, rows) == "?"


def test_missing_database_is_not_an_error(tmp_path):
    assert audit.db_reasons(str(tmp_path / "nope.sqlite"), "ETHUSDT", 0.0) == []
    assert audit.db_reasons("", "ETHUSDT", 0.0) == []


# ─── гейт выборки ────────────────────────────────────────────────────────

def test_gate_needs_both_events_and_time():
    assert "НЕ набрана" in audit.gate_status(99, 30.0)
    assert "НЕ набрана" in audit.gate_status(120, 13.0)
    assert "выборка набрана" in audit.gate_status(100, 14.0)


# ─── накопитель ──────────────────────────────────────────────────────────

def _row() -> dict:
    return {k: 0 for k in audit.SNAPSHOT_FIELDS
            if k not in ("ts_utc", "window_days")}


def test_snapshot_writes_header_once(tmp_path):
    path = str(tmp_path / "audit.csv")
    audit.append_snapshot(path, [_row()], ts_utc="2026-08-20T10:00:00Z",
                          window_days=7)
    audit.append_snapshot(path, [_row()], ts_utc="2026-08-20T14:00:00Z",
                          window_days=7)
    lines = pathlib.Path(path).read_text().strip().splitlines()
    assert lines[0].split(",") == audit.SNAPSHOT_FIELDS
    assert len(lines) == 3


def test_snapshot_rotates_file_on_schema_change(tmp_path):
    path = tmp_path / "audit.csv"
    path.write_text("ts_utc,symbol\n2026-01-01T00:00:00Z,ETHUSDT\n")
    rotated = audit.append_snapshot(str(path), [_row()],
                                    ts_utc="2026-08-20T10:00:00Z",
                                    window_days=7)
    assert rotated and pathlib.Path(rotated).exists()
    assert path.read_text().splitlines()[0].split(",") == audit.SNAPSHOT_FIELDS


def test_unknown_field_raises_instead_of_dropping_a_column(tmp_path):
    row = _row()
    row["новая_метрика"] = 1
    with pytest.raises(ValueError):
        audit.append_snapshot(str(tmp_path / "audit.csv"), [row],
                              ts_utc="2026-08-20T10:00:00Z", window_days=7)


def test_median_of_even_and_odd_samples():
    assert audit._median([]) == 0.0
    assert audit._median([1.0, 3.0, 2.0]) == pytest.approx(2.0)
    assert audit._median([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
