"""Тесты для scripts/ab_test_snapshot.py.

Покрытие: схема БД, парсинг дат, парсинг closedPnl записей, инкрементальный
sync (без реальной сети), маппинг стратегий, CRUD волн, основные срезы отчёта.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.ab_test_snapshot import (
    DEFAULT_EPOCH_MS,
    _money,
    _parse_date_ms,
    _parse_trade,
    add_wave,
    enrich_strategy,
    list_waves,
    meta_get,
    meta_set,
    open_db,
    parse_add_wave_spec,
    render_report,
    sync_closed_pnl,
)


# --- Вспомогательные ----------------------------------------------------------


def _make_closed_pnl_item(
    order_id: str,
    *,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.01,
    entry: float = 60000.0,
    exitp: float = 60200.0,
    pnl: float = 1.5,
    created_ms: int = 1_744_000_000_000,
    updated_ms: int = 1_744_000_180_000,
    order_link_id: str = "",
) -> dict:
    return {
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "avgEntryPrice": str(entry),
        "avgExitPrice": str(exitp),
        "closedPnl": str(pnl),
        "execType": "Trade",
        "createdTime": str(created_ms),
        "updatedTime": str(updated_ms),
        "leverage": "5",
        "orderLinkId": order_link_id,
    }


class _FakeSession:
    """Имитирует pybit.HTTP: get_closed_pnl с пагинацией по cursor."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def get_closed_pnl(self, **params) -> dict:
        self.calls.append(params)
        cursor = params.get("cursor", "")
        idx = int(cursor) if cursor else 0
        if idx >= len(self._pages):
            return {"result": {"list": [], "nextPageCursor": ""}}
        items = self._pages[idx]
        next_cursor = str(idx + 1) if idx + 1 < len(self._pages) else ""
        return {"result": {"list": items, "nextPageCursor": next_cursor}}


# --- Тесты --------------------------------------------------------------------


class TestDbSchema:
    def test_open_db_creates_tables(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"closed_trades", "waves", "sync_meta"}.issubset(tables)

    def test_open_db_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "ab.sqlite"
        open_db(path).close()
        conn = open_db(path)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM closed_trades"
        ).fetchone()
        assert row["n"] == 0

    def test_meta_roundtrip(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        assert meta_get(conn, "key") is None
        meta_set(conn, "key", "value1")
        assert meta_get(conn, "key") == "value1"
        meta_set(conn, "key", "value2")
        assert meta_get(conn, "key") == "value2"


class TestDateParsing:
    def test_parse_date_ms_date_only(self) -> None:
        ms = _parse_date_ms("2026-04-11")
        assert ms is not None and ms > 0
        ms_eod = _parse_date_ms("2026-04-11", end_of_day=True)
        assert ms_eod is not None and ms_eod > ms

    def test_parse_date_ms_datetime(self) -> None:
        assert _parse_date_ms("2026-04-11T13:00") is not None
        assert _parse_date_ms("2026-04-11 13:00:00") is not None

    def test_parse_date_ms_none(self) -> None:
        assert _parse_date_ms(None) is None
        assert _parse_date_ms("") is None

    def test_parse_date_ms_invalid(self) -> None:
        with pytest.raises(ValueError):
            _parse_date_ms("not-a-date")


class TestTradeParsing:
    def test_parse_trade_ok(self) -> None:
        item = _make_closed_pnl_item("o1", order_link_id="vwap_BTCUSDT_1")
        t = _parse_trade(item)
        assert t is not None
        assert t.order_id == "o1"
        assert t.symbol == "BTCUSDT"
        assert t.closed_pnl == 1.5
        assert t.order_link_id == "vwap_BTCUSDT_1"
        assert t.updated_time_ms > t.created_time_ms

    def test_parse_trade_missing_order_id(self) -> None:
        assert _parse_trade({"symbol": "BTC"}) is None


class TestSyncIncremental:
    def test_sync_inserts_and_dedup(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        page1 = [
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 60_000),
            _make_closed_pnl_item("o2", created_ms=base + 120_000, updated_ms=base + 180_000),
        ]
        session = _FakeSession([page1])
        added = sync_closed_pnl(conn, session=session, category="linear", stats_db_path=None)
        assert added == 2
        last = meta_get(conn, "last_fetched_end_ms")
        assert last is not None and int(last) == base + 180_000

        session2 = _FakeSession([page1])
        added2 = sync_closed_pnl(conn, session=session2, category="linear", stats_db_path=None)
        assert added2 == 0

        session3 = _FakeSession(
            [[_make_closed_pnl_item("o3", created_ms=base + 300_000, updated_ms=base + 360_000)]]
        )
        added3 = sync_closed_pnl(conn, session=session3, category="linear", stats_db_path=None)
        assert added3 == 1
        n = conn.execute("SELECT COUNT(*) AS n FROM closed_trades").fetchone()["n"]
        assert n == 3

    def test_sync_computes_hold_minutes(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 10 * 60_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=None)
        row = conn.execute("SELECT hold_minutes FROM closed_trades WHERE order_id='o1'").fetchone()
        assert pytest.approx(row["hold_minutes"], rel=1e-6) == 10.0


class TestStrategyEnrich:
    def test_enrich_maps_strategy_from_stats_db(self, tmp_path: Path) -> None:
        stats_path = tmp_path / "bybit_stats.sqlite"
        stats_conn = sqlite3.connect(str(stats_path))
        stats_conn.executescript(
            "CREATE TABLE positions (order_id TEXT, strategy TEXT); "
            "INSERT INTO positions VALUES ('o1', 'scalp_vwap'); "
            "INSERT INTO positions VALUES ('o2', 'scalp_statarb');"
        )
        stats_conn.commit()
        stats_conn.close()

        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 60_000),
            _make_closed_pnl_item("o2", created_ms=base + 120_000, updated_ms=base + 180_000),
            _make_closed_pnl_item("o3", created_ms=base + 240_000, updated_ms=base + 300_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)

        rows = {
            r["order_id"]: r["strategy"]
            for r in conn.execute(
                "SELECT order_id, strategy FROM closed_trades"
            ).fetchall()
        }
        assert rows == {"o1": "scalp_vwap", "o2": "scalp_statarb", "o3": "unknown"}

    def test_enrich_noop_when_stats_missing(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 60_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=tmp_path / "none.sqlite")
        updated = enrich_strategy(conn, tmp_path / "none.sqlite")
        assert updated == 0


class TestWaves:
    def test_add_wave_and_update(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        wid = add_wave(conn, {"name": "baseline", "start": "2026-04-11T13:00", "desc": "first"})
        assert wid > 0
        same = add_wave(conn, {"name": "baseline", "start": "2026-04-11T14:00", "desc": "moved"})
        assert same == wid
        rows = list_waves(conn)
        assert len(rows) == 1
        assert rows[0]["start_utc"] == "2026-04-11T14:00"
        assert rows[0]["description"] == "moved"

    def test_add_wave_requires_fields(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        with pytest.raises(ValueError):
            add_wave(conn, {"name": "x"})
        with pytest.raises(ValueError):
            add_wave(conn, {"start": "2026-04-11T13:00"})

    def test_parse_add_wave_spec(self) -> None:
        spec = parse_add_wave_spec("name=foo;start=2026-04-11;desc=hello world")
        assert spec == {"name": "foo", "start": "2026-04-11", "desc": "hello world"}


class TestReport:
    def _seed(self, tmp_path: Path) -> sqlite3.Connection:
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", symbol="BTCUSDT", pnl=2.0,
                                  created_ms=base, updated_ms=base + 3 * 60_000),
            _make_closed_pnl_item("o2", symbol="BTCUSDT", pnl=-1.5,
                                  created_ms=base + 600_000, updated_ms=base + 600_000 + 20 * 60_000),
            _make_closed_pnl_item("o3", symbol="ETHUSDT", pnl=0.5,
                                  created_ms=base + 1200_000, updated_ms=base + 1200_000 + 90 * 60_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=None)
        return conn

    def test_render_report_contains_sections(self, tmp_path: Path) -> None:
        conn = self._seed(tmp_path)
        md = render_report(conn, since=None, until=None, wave_id=None)
        for section in [
            "## Overall",
            "## По волнам",
            "## По дням (UTC)",
            "## По символам",
            "## По стратегиям",
            "## По часам (UTC)",
            "## По длительности удержания",
        ]:
            assert section in md
        assert "Сделок | 3" in md
        assert "BTCUSDT" in md
        assert "ETHUSDT" in md

    def test_render_report_wave_filter(self, tmp_path: Path) -> None:
        conn = self._seed(tmp_path)
        add_wave(conn, {
            "name": "only_one",
            "start": "2020-01-01",
            "end": "2020-01-02",
        })
        md = render_report(conn, since=None, until=None, wave_id=1)
        assert "Нет сделок в указанном окне" in md

    def test_render_report_hold_buckets(self, tmp_path: Path) -> None:
        conn = self._seed(tmp_path)
        md = render_report(conn, since=None, until=None, wave_id=None)
        assert "0-5m" in md
        assert "5-15m" in md or "15-60m" in md
        assert "60m+" in md


class TestMoney:
    def test_money_format(self) -> None:
        assert _money(0) == "$0.00"
        assert _money(1.234) == "$1.23"
        assert _money(-0.5, prec=3) == "-$0.500"
