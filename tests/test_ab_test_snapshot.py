"""Тесты для scripts/ab_test_snapshot.py.

Покрытие: схема БД, парсинг дат, парсинг closedPnl записей, инкрементальный
sync (без реальной сети), fuzzy-матч стратегий с positions, пересчёт
hold_minutes, CRUD волн, срезы отчёта (включая 'overall excl. recovered').
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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
    side: str = "Sell",  # closing side (инверсия от open); дефолт: позиция была LONG → закрытие Sell
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


def _ms_to_iso(ms: int) -> str:
    """Ms → ISO-строка с суффиксом +00:00 (формат, в котором бот пишет opened_at)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _make_bot_positions_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Создать minimal bybit_stats.sqlite с реальной схемой positions."""
    path = tmp_path / "bybit_stats.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty TEXT NOT NULL,
            entry_price REAL NOT NULL,
            order_id TEXT,
            strategy TEXT NOT NULL DEFAULT 'ensemble',
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO positions (symbol, side, qty, entry_price, order_id, "
            "strategy, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row["symbol"], row["side"], row["qty"], row["entry_price"],
                row.get("order_id", ""), row["strategy"],
                row["opened_at"], row.get("closed_at"),
            ),
        )
    conn.commit()
    conn.close()
    return path


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

    def test_sync_leaves_hold_null_until_enriched(self, tmp_path: Path) -> None:
        """Без stats_db sync НЕ должен ставить hold (раньше считался как created→updated,
        что некорректно: это время исполнения закрывающего ордера, не hold позиции)."""
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 10 * 60_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=None)
        row = conn.execute(
            "SELECT hold_minutes, opened_at_ms FROM closed_trades WHERE order_id='o1'"
        ).fetchone()
        assert row["hold_minutes"] is None
        assert row["opened_at_ms"] is None


class TestStrategyEnrich:
    def test_fuzzy_match_inverts_side_and_fills_hold(self, tmp_path: Path) -> None:
        """closed.side=Sell ⇔ positions.side=Buy (закрытие лонга).

        После fuzzy-матча должна проставиться strategy и opened_at_ms,
        а hold_minutes пересчитается как (updated − opened)/60000.
        """
        open_ms = DEFAULT_EPOCH_MS + 3_600_000
        close_ms = open_ms + 17 * 60_000  # 17 min hold
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "BTCUSDT",
            "side": "Buy",           # long
            "qty": "0.01",
            "entry_price": 60000.0,
            "strategy": "scalp_vwap",
            "opened_at": _ms_to_iso(open_ms),
            "closed_at": _ms_to_iso(close_ms),
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_1", symbol="BTCUSDT", side="Sell",  # закрытие LONG
                qty=0.01, entry=60000.0,
                created_ms=close_ms - 100, updated_ms=close_ms,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)

        row = conn.execute(
            "SELECT strategy, opened_at_ms, hold_minutes "
            "FROM closed_trades WHERE order_id='close_ord_1'"
        ).fetchone()
        assert row["strategy"] == "scalp_vwap"
        assert row["opened_at_ms"] is not None
        # Допуск ±1 сек (ISO/julianday round-trip в секундах).
        assert abs(int(row["opened_at_ms"]) - open_ms) < 1000
        assert pytest.approx(row["hold_minutes"], abs=0.05) == 17.0

    def test_fuzzy_match_short_position(self, tmp_path: Path) -> None:
        """Закрытие шорта: closed.side=Buy ↔ positions.side=Sell."""
        open_ms = DEFAULT_EPOCH_MS + 3_600_000
        close_ms = open_ms + 5 * 60_000
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "ETHUSDT", "side": "Sell", "qty": "0.1",
            "entry_price": 3000.0, "strategy": "scalp_statarb",
            "opened_at": _ms_to_iso(open_ms),
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_2", symbol="ETHUSDT", side="Buy",
                qty=0.1, entry=3000.0,
                created_ms=close_ms, updated_ms=close_ms,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        row = conn.execute(
            "SELECT strategy FROM closed_trades WHERE order_id='close_ord_2'"
        ).fetchone()
        assert row["strategy"] == "scalp_statarb"

    def test_fuzzy_match_entry_price_tolerance(self, tmp_path: Path) -> None:
        """Entry price расходится на 0.05% (в пределах 0.1%) — матч должен пройти."""
        open_ms = DEFAULT_EPOCH_MS + 3_600_000
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "SOLUSDT", "side": "Buy", "qty": "1.0",
            "entry_price": 150.00, "strategy": "scalp_volspike",
            "opened_at": _ms_to_iso(open_ms),
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_3", symbol="SOLUSDT", side="Sell",
                qty=1.0, entry=150.07,  # ≈0.047% расхождение
                created_ms=open_ms + 60_000, updated_ms=open_ms + 60_000,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        row = conn.execute(
            "SELECT strategy FROM closed_trades WHERE order_id='close_ord_3'"
        ).fetchone()
        assert row["strategy"] == "scalp_volspike"

    def test_fuzzy_match_entry_price_too_far(self, tmp_path: Path) -> None:
        """Entry price расходится на 1% (вне допуска 0.1%) — матч не должен пройти."""
        open_ms = DEFAULT_EPOCH_MS + 3_600_000
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "SOLUSDT", "side": "Buy", "qty": "1.0",
            "entry_price": 150.00, "strategy": "scalp_volspike",
            "opened_at": _ms_to_iso(open_ms),
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_4", symbol="SOLUSDT", side="Sell",
                qty=1.0, entry=151.5,  # 1% расхождение
                created_ms=open_ms + 60_000, updated_ms=open_ms + 60_000,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        row = conn.execute(
            "SELECT strategy FROM closed_trades WHERE order_id='close_ord_4'"
        ).fetchone()
        assert row["strategy"] == "unknown"

    def test_fuzzy_match_picks_nearest_opened_at(self, tmp_path: Path) -> None:
        """Если две открытия под один закрывающий — берём ближайшую по времени."""
        close_ms = DEFAULT_EPOCH_MS + 10_000_000
        far_open = close_ms - 20 * 60 * 60 * 1000   # 20 часов назад
        near_open = close_ms - 5 * 60_000            # 5 мин назад
        stats_path = _make_bot_positions_db(tmp_path, [
            {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
             "entry_price": 60000.0, "strategy": "scalp_old",
             "opened_at": _ms_to_iso(far_open), "closed_at": _ms_to_iso(far_open + 1000)},
            {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
             "entry_price": 60000.0, "strategy": "scalp_new",
             "opened_at": _ms_to_iso(near_open)},
        ])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_near", symbol="BTCUSDT", side="Sell",
                qty=0.01, entry=60000.0,
                created_ms=close_ms, updated_ms=close_ms,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        row = conn.execute(
            "SELECT strategy FROM closed_trades WHERE order_id='close_ord_near'"
        ).fetchone()
        assert row["strategy"] == "scalp_new"

    def test_fuzzy_match_opened_after_closed_is_rejected(self, tmp_path: Path) -> None:
        """Позиция, которая была открыта ПОСЛЕ закрытия — не кандидат."""
        close_ms = DEFAULT_EPOCH_MS + 10_000_000
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
            "entry_price": 60000.0, "strategy": "scalp_future",
            "opened_at": _ms_to_iso(close_ms + 60_000),  # после закрытия
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "close_ord_x", symbol="BTCUSDT", side="Sell",
                qty=0.01, entry=60000.0,
                created_ms=close_ms, updated_ms=close_ms,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        row = conn.execute(
            "SELECT strategy FROM closed_trades WHERE order_id='close_ord_x'"
        ).fetchone()
        assert row["strategy"] == "unknown"

    def test_enrich_idempotent(self, tmp_path: Path) -> None:
        """Повторный вызов enrich_strategy не меняет уже сматченное."""
        open_ms = DEFAULT_EPOCH_MS + 3_600_000
        close_ms = open_ms + 60_000
        stats_path = _make_bot_positions_db(tmp_path, [{
            "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
            "entry_price": 60000.0, "strategy": "scalp_vwap",
            "opened_at": _ms_to_iso(open_ms),
        }])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "o1", symbol="BTCUSDT", side="Sell", qty=0.01, entry=60000.0,
                created_ms=close_ms, updated_ms=close_ms,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        first = enrich_strategy(conn, stats_path)  # был вызов в sync, сейчас уже 0
        assert first == 0

    def test_enrich_noop_when_stats_missing(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "ab.sqlite")
        base = DEFAULT_EPOCH_MS + 3600_000
        session = _FakeSession([[
            _make_closed_pnl_item("o1", created_ms=base, updated_ms=base + 60_000),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=tmp_path / "none.sqlite")
        updated = enrich_strategy(conn, tmp_path / "none.sqlite")
        assert updated == 0
        row = conn.execute("SELECT strategy FROM closed_trades WHERE order_id='o1'").fetchone()
        assert row["strategy"] == "unknown"

    def test_existing_db_schema_migrates(self, tmp_path: Path) -> None:
        """Старая БД без opened_at_ms должна быть мигрирована автоматически."""
        path = tmp_path / "ab.sqlite"
        old_conn = sqlite3.connect(str(path))
        old_conn.executescript(
            """
            CREATE TABLE closed_trades (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL,
                avg_entry_price REAL,
                avg_exit_price REAL,
                closed_pnl REAL,
                exec_type TEXT,
                created_time_ms INTEGER,
                updated_time_ms INTEGER NOT NULL,
                leverage REAL,
                order_link_id TEXT,
                strategy TEXT,
                hold_minutes REAL,
                raw_json TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE waves (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
                start_utc TEXT NOT NULL, end_utc TEXT, commit_hash TEXT,
                description TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE sync_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        old_conn.commit()
        old_conn.close()

        conn = open_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(closed_trades)").fetchall()}
        assert "opened_at_ms" in cols


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
        """Seed с matched stats_db — чтобы hold_minutes были известны и buckets строились."""
        base = DEFAULT_EPOCH_MS + 3600_000

        def closed(ms: int) -> int:
            return ms  # close ms

        def opened(ms: int, hold_min: int) -> int:
            return ms - hold_min * 60_000

        rows = [
            {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
             "entry_price": 60000.0, "strategy": "scalp_vwap",
             "opened_at": _ms_to_iso(opened(base, 3))},
            {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.02",
             "entry_price": 60100.0, "strategy": "scalp_vwap",
             "opened_at": _ms_to_iso(opened(base + 600_000, 20))},
            {"symbol": "ETHUSDT", "side": "Buy", "qty": "0.03",
             "entry_price": 3000.0, "strategy": "scalp_statarb",
             "opened_at": _ms_to_iso(opened(base + 1_200_000, 90))},
        ]
        stats_path = _make_bot_positions_db(tmp_path, rows)
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "o1", symbol="BTCUSDT", side="Sell", qty=0.01, entry=60000.0,
                pnl=2.0, created_ms=base, updated_ms=closed(base),
            ),
            _make_closed_pnl_item(
                "o2", symbol="BTCUSDT", side="Sell", qty=0.02, entry=60100.0,
                pnl=-1.5, created_ms=base + 600_000, updated_ms=closed(base + 600_000),
            ),
            _make_closed_pnl_item(
                "o3", symbol="ETHUSDT", side="Sell", qty=0.03, entry=3000.0,
                pnl=0.5, created_ms=base + 1_200_000, updated_ms=closed(base + 1_200_000),
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
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
        # Теперь стратегии должны быть сматчены, не 'unknown'.
        assert "scalp_vwap" in md
        assert "scalp_statarb" in md

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

    def test_overall_excludes_recovered(self, tmp_path: Path) -> None:
        """Если есть позиции с strategy='recovered', в Overall появляется отдельный срез."""
        base = DEFAULT_EPOCH_MS + 3_600_000
        stats_path = _make_bot_positions_db(tmp_path, [
            {"symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
             "entry_price": 60000.0, "strategy": "scalp_vwap",
             "opened_at": _ms_to_iso(base - 60_000)},
            {"symbol": "ETHUSDT", "side": "Buy", "qty": "0.1",
             "entry_price": 3000.0, "strategy": "recovered",
             "opened_at": _ms_to_iso(base + 600_000 - 60_000)},
        ])
        conn = open_db(tmp_path / "ab.sqlite")
        session = _FakeSession([[
            _make_closed_pnl_item(
                "o1", symbol="BTCUSDT", side="Sell", qty=0.01, entry=60000.0,
                pnl=1.0, created_ms=base, updated_ms=base,
            ),
            _make_closed_pnl_item(
                "o2", symbol="ETHUSDT", side="Sell", qty=0.1, entry=3000.0,
                pnl=-3.0, created_ms=base + 600_000, updated_ms=base + 600_000,
            ),
        ]])
        sync_closed_pnl(conn, session=session, category="linear", stats_db_path=stats_path)
        md = render_report(conn, since=None, until=None, wave_id=None)
        assert "Overall (excl. `recovered`)" in md
        assert "1 подхваченных" in md
        # В overall полный — PnL -$2 (1 - 3), в excl. — PnL +$1.
        assert "-$2.00" in md
        assert "$1.00" in md


class TestMoney:
    def test_money_format(self) -> None:
        assert _money(0) == "$0.00"
        assert _money(1.234) == "$1.23"
        assert _money(-0.5, prec=3) == "-$0.500"
