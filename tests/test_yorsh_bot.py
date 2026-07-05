"""Юнит-тесты yorsh_bot: settings, схема БД, изоляция импортов (M0).

Все цели — чистая детерминированная логика (без сети/WS/биржи). M0:
smoke-тесты скелета.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

from yorsh_bot.config.settings import YorshSettings, load_settings
from yorsh_bot.state.db import YorshDB


# ─── settings ────────────────────────────────────────────────────────────

def test_settings_defaults():
    s = YorshSettings()
    assert s.data_dir == "/data"
    assert s.exchanges == "mexc,bitget"
    assert s.max_symbols_per_exchange == 50
    assert s.universe_refresh_hours == 6.0
    assert s.min_24h_volume_usd == 10_000.0
    assert s.max_24h_volume_usd == 2_000_000.0
    assert s.raw_retention_days == 30
    assert s.raw_max_gb == 20.0
    assert s.density_kratnosti == 5.0
    assert s.density_min_persistence_sec == 60.0
    assert s.spurt_min_amplitude_pct == 2.0
    assert s.symbols_static == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("YORSH_DATA_DIR", "/tmp/yorsh_test")
    monkeypatch.setenv("YORSH_EXCHANGES", "mexc")
    monkeypatch.setenv("YORSH_DENSITY_KRATNOSTI", "7.5")
    monkeypatch.setenv("YORSH_SPURT_MIN_AMPLITUDE_PCT", "1.5")
    monkeypatch.setenv("YORSH_SYMBOLS_STATIC", "abcusdt,xyzusdt")
    s = load_settings()
    assert s.data_dir == "/tmp/yorsh_test"
    assert s.exchange_list == ["mexc"]
    assert s.density_kratnosti == 7.5
    assert s.spurt_min_amplitude_pct == 1.5
    assert s.static_symbol_list == ["ABCUSDT", "XYZUSDT"]


def test_settings_helpers():
    s = YorshSettings(exchanges="mexc, bitget , ", symbols_static=" a , b ")
    assert s.exchange_list == ["mexc", "bitget"]
    assert s.static_symbol_list == ["A", "B"]
    s2 = YorshSettings(symbols_static="")
    assert s2.static_symbol_list == []


# ─── DB schema ───────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db():
    with tempfile.TemporaryDirectory() as d:
        db = YorshDB(d)
        yield db
        db.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_db_creates_all_tables(tmp_db):
    tables = _tables(tmp_db.conn)
    expected = {"densities", "spurt_events", "candidates", "universe_log",
                "collector_health", "meta"}
    assert expected.issubset(tables)


def test_db_meta_helpers(tmp_db):
    assert tmp_db.meta_get("schema_version") == "1"
    assert tmp_db.meta_get("nope") is None
    tmp_db.meta_set("last_deleted_partition", "mexc/ABCUSDT/2026-07-01")
    assert tmp_db.meta_get("last_deleted_partition") == "mexc/ABCUSDT/2026-07-01"
    # upsert
    tmp_db.meta_set("last_deleted_partition", "mexc/ABCUSDT/2026-07-02")
    assert tmp_db.meta_get("last_deleted_partition") == "mexc/ABCUSDT/2026-07-02"


def test_db_health_and_universe_log(tmp_db):
    tmp_db.log_health(exchange="mexc", event="gap", symbol="ABCUSDT",
                      detail="seq jump 5->8")
    tmp_db.log_universe(exchange="bitget", event="add", symbol="XYZUSDT")
    rows_health = tmp_db.conn.execute(
        "SELECT exchange,event,symbol FROM collector_health").fetchall()
    rows_uni = tmp_db.conn.execute(
        "SELECT exchange,event,symbol FROM universe_log").fetchall()
    assert len(rows_health) == 1
    assert rows_health[0]["event"] == "gap"
    assert len(rows_uni) == 1
    assert rows_uni[0]["symbol"] == "XYZUSDT"


def test_db_idempotent_reopen(tmp_db):
    path = tmp_db.path
    tmp_db.close()
    db2 = YorshDB(os.path.dirname(path))
    assert db2.meta_get("schema_version") == "1"
    db2.close()


def test_db_unique_active_candidate(tmp_db):
    """UNIQUE index на active-кандидата (exchange,symbol) — один active на пару."""
    conn = tmp_db.conn
    conn.execute(
        "INSERT INTO candidates(exchange,symbol,first_detected,last_detected,"
        "status) VALUES ('mexc','ABCUSDT',1,1,'active')")
    conn.commit()
    # второй active на ту же пару — должен упасть по unique-индексу
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidates(exchange,symbol,first_detected,"
            "last_detected,status) VALUES ('mexc','ABCUSDT',2,2,'active')")
        conn.commit()
    # closed на ту же пару — ок
    conn.execute(
        "INSERT INTO candidates(exchange,symbol,first_detected,last_detected,"
        "status) VALUES ('mexc','ABCUSDT',3,3,'closed')")
    conn.commit()


# ─── изоляция импортов (strategy-guard.mdc) ──────────────────────────────

_FORBIDDEN = ("fx_pro_bot", "fx_ai_trader", "scalp_bot", "flowzone_bot",
              "fx_momentum_bot", "bybit_bot", "ai_trader",
              "tradecard_bybit", "tradecard_momentum", "ru_stocks_analyst")


def test_yorsh_bot_does_not_import_other_bots():
    """yorsh_bot не тянет модули других ботов (изоляция).

    Запуск в чистом subprocess — sys.modules основного процесса уже мог
    загрузить чужие пакеты другими тестами, поэтому проверяем изолированно.
    """
    import json
    import subprocess
    code = (
        "import sys, json\n"
        "import yorsh_bot  # noqa: F401\n"
        "import yorsh_bot.app.main  # noqa: F401\n"
        "import yorsh_bot.state.db  # noqa: F401\n"
        "import yorsh_bot.exchanges.base  # noqa: F401\n"
        "loaded = sorted({m.split('.')[0] for m in sys.modules})\n"
        "forbidden = %r\n"
        "leaked = [f for f in forbidden if f in loaded]\n"
        "print(json.dumps({'loaded': loaded, 'leaked': leaked}))\n"
    ) % (_FORBIDDEN,)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    data = json.loads(out.stdout)
    assert not data["leaked"], (
        f"yorsh_bot утёк в чужие пакеты: {data['leaked']}")


def test_yorsh_bot_no_trading_module():
    """Модуля trading/ в пакете нет вообще (Фаза 1 = data-only)."""
    import yorsh_bot
    import_path = yorsh_bot.__path__[0]
    for entry in os.listdir(import_path):
        assert not entry.startswith("trading"), (
            f"trading-модуль не должен существовать в yorsh_bot: {entry}")
