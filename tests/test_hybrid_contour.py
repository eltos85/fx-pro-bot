"""Учёт контура H-HYBRID: реплей книги и декомпозиция лота.

Проверяется арифметика УЧЁТА (scripts/hybrid_contour_pnl.py), не торговая
логика: контур на shared one-way лоте нельзя посчитать сложением двух БД, и
ошибка в реплее тихо искажает все производные метрики.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "hybrid_contour_pnl.py")
_spec = importlib.util.spec_from_file_location("hybrid_contour_pnl", _PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    pytest.skip("измеритель контура не найден", allow_module_level=True)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _fill(ts, side, qty, px, *, link="", stop="", etype="Trade"):
    return {"execTime": str(ts), "side": side, "execQty": str(qty),
            "execPrice": str(px), "orderLinkId": link, "stopOrderType": stop,
            "execType": etype, "execFee": "0", "execValue": str(qty * px)}


def test_actor_attribution_by_link_prefix():
    assert mod._actor(_fill(1, "Buy", 1, 100, link="swing_abc"))[0] == "core"
    assert mod._actor(_fill(1, "Buy", 1, 100,
                            link="daytrend_abc"))[0] == "core"
    assert mod._actor(_fill(1, "Buy", 1, 100,
                            link="scalp_ETHUSDT_1"))[0] == "tactic"
    # Биржевой брекет ставится через set_trading_stop — linkId пустой.
    assert mod._actor(_fill(1, "Sell", 1, 100,
                            stop="StopLoss"))[0] == "bracket"


def test_core_and_tactic_split_pro_rata():
    """Ядро 3 @ 100 + тактика 1 @ 100, выход всего лота по 110.

    Лот однороден по цене, поэтому +$40 должны разделиться 3:1 — ядру $30,
    тактике $10.
    """
    fills = [
        _fill(1_000, "Buy", 3, 100, link="swing_a"),
        _fill(2_000, "Buy", 1, 100, link="scalp_ETHUSDT_1"),
        _fill(3_000, "Sell", 4, 110, stop="StopLoss"),
    ]
    book = mod._replay_book(fills)
    assert book["realized"] == pytest.approx(40.0)
    assert book["realized_core"] == pytest.approx(30.0)
    assert book["realized_tac"] == pytest.approx(10.0)
    assert book["size"] == pytest.approx(0.0)


def test_core_closed_by_bracket_counts_as_forced():
    """Ядро закрыл чужой брекет — это принудительная реализация ядра."""
    fills = [
        _fill(1_000, "Buy", 2, 100, link="swing_a"),
        _fill(2_000, "Sell", 2, 105, stop="StopLoss"),
    ]
    book = mod._replay_book(fills)
    assert book["forced_core_n"] == 1
    assert book["volunt_core_n"] == 0
    assert book["forced_core_pnl"] == pytest.approx(10.0)


def test_core_closed_by_horizon_counts_as_voluntary():
    fills = [
        _fill(1_000, "Buy", 2, 100, link="swing_a"),
        _fill(2_000, "Sell", 2, 105, link="swing_b"),
    ]
    book = mod._replay_book(fills)
    assert book["volunt_core_n"] == 1
    assert book["forced_core_n"] == 0


def test_reentry_gap_measured_against_lock():
    """Перезаход дороже лока — отрицательная стоимость для long."""
    fills = [
        _fill(1_000, "Buy", 2, 100, link="swing_a"),
        _fill(2_000, "Sell", 2, 110, stop="StopLoss"),
        _fill(3_000, "Buy", 2, 111, link="swing_b"),
    ]
    book = mod._replay_book(fills)
    assert len(book["reentries"]) == 1
    re = book["reentries"][0]
    assert re["gap_pct"] == pytest.approx((111 / 110 - 1) * 100)
    assert re["gap_usd"] == pytest.approx((110 - 111) * 2)


def test_aligned_replay_drops_tail_of_earlier_position():
    """Первая нога окна — закрытие позиции, открытой раньше.

    Реплей с нуля открыл бы фантомный short и исказил всё. Выравнивание должно
    отбросить хвост и сойтись с фактическим остатком биржи.
    """
    fills = [
        _fill(500, "Sell", 1.75, 1900, stop="StopLoss"),  # хвост прошлой
        _fill(1_000, "Buy", 3.0, 2000, link="swing_a"),
    ]
    book = mod._replay_aligned(fills, signed_pos=3.0, pos_avg=2000.0)
    assert book["aligned"] is True
    assert book["skipped"] == 1
    assert book["size"] == pytest.approx(3.0)
    assert book["avg"] == pytest.approx(2000.0)


def test_aligned_replay_reports_failure_when_book_cannot_match():
    fills = [_fill(1_000, "Buy", 1.0, 100, link="swing_a")]
    book = mod._replay_aligned(fills, signed_pos=99.0, pos_avg=100.0)
    assert book["aligned"] is False


def test_time_share_counts_from_first_core_entry():
    """Знаменатель времени — первый вход ядра, а не начало окна.

    Иначе доля «ядро в рынке» зависит от --days: период до появления ядра
    попадал бы в «вне рынка».
    """
    fills = [
        _fill(0, "Buy", 1, 100, link="scalp_x"),  # тактика за 10с до ядра
        _fill(10_000, "Buy", 2, 100, link="swing_a"),
        _fill(20_000, "Sell", 3, 101, stop="StopLoss"),
    ]
    book = mod._replay_book(fills)
    assert book["span_sec"] == pytest.approx(10.0)
    assert book["core_sec"] == pytest.approx(10.0)


def test_funding_rows_are_not_trades():
    """Funding-запись несёт execQty = размер позиции; в книгу попасть не должна.

    https://bybit-exchange.github.io/docs/v5/enum#exectype
    """
    assert "Funding" in mod.FUNDING_EXEC_TYPES
    assert "Funding" not in mod.TRADE_EXEC_TYPES


def _snap_row(symbol="ETHUSDT"):
    row = {f: 0 for f in mod.SNAPSHOT_FIELDS
           if f not in ("ts_utc", "window_days")}
    row["symbol"] = symbol
    return row


def test_snapshot_writes_header_once_and_appends(tmp_path):
    path = str(tmp_path / "snap.csv")
    for i in range(2):
        assert mod._append_snapshot(path, [_snap_row()],
                                    ts_utc="2026-08-20T1%d:00:00Z" % i,
                                    window_days=7.0) is None
    lines = open(path).read().strip().splitlines()
    assert lines[0].split(",") == mod.SNAPSHOT_FIELDS
    assert len(lines) == 3
    assert all(len(ln.split(",")) == len(mod.SNAPSHOT_FIELDS) for ln in lines)


def test_snapshot_rotates_file_when_schema_changed(tmp_path):
    """Накопитель со старым (коротким) заголовком нельзя дописывать.

    Реальный случай: файл на VPS был создан до добавления метрик
    декомпозиции, dict стал шире заголовка — dict-writer сдвинул бы колонки.
    """
    path = str(tmp_path / "snap.csv")
    with open(path, "w") as fh:
        fh.write("ts_utc,window_days,symbol,realized_net\n")
        fh.write("2026-08-19T10:00:00Z,3.0,ETHUSDT,12.5\n")

    rotated = mod._append_snapshot(path, [_snap_row()],
                                   ts_utc="2026-08-20T10:00:00Z",
                                   window_days=7.0)
    assert rotated is not None
    assert open(rotated).read().count("2026-08-19") == 1

    lines = open(path).read().strip().splitlines()
    assert lines[0].split(",") == mod.SNAPSHOT_FIELDS
    assert len(lines) == 2
    assert len(lines[1].split(",")) == len(mod.SNAPSHOT_FIELDS)


def test_snapshot_raises_on_field_outside_schema(tmp_path):
    """Новая метрика без правки схемы должна падать, а не теряться молча."""
    path = str(tmp_path / "snap.csv")
    row = _snap_row()
    row["brand_new_metric"] = 1
    with pytest.raises(ValueError):
        mod._append_snapshot(path, [row], ts_utc="2026-08-20T10:00:00Z",
                             window_days=7.0)


def test_snapshot_schema_matches_report_keys():
    """Схема CSV должна совпадать с тем, что возвращает `_report_symbol`.

    Расхождение уронило бы cron-прогон (extrasaction="raise") или оставило
    пустую колонку — оба варианта обнаруживаются только через сутки.
    """
    import ast

    tree = ast.parse(open(_PATH).read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_report_symbol")
    ret = next(n for n in ast.walk(fn)
               if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
    keys = [k.value for k in ret.value.keys if isinstance(k, ast.Constant)]
    assert keys == [f for f in mod.SNAPSHOT_FIELDS
                    if f not in ("ts_utc", "window_days")]


def test_snapshot_noop_on_empty_rows(tmp_path):
    """Пустой прогон (нет данных по символам) не должен создавать файл."""
    path = str(tmp_path / "snap.csv")
    assert mod._append_snapshot(path, [], ts_utc="2026-08-20T10:00:00Z",
                                window_days=7.0) is None
    assert not os.path.exists(path)
