"""Фильтры impulse-bot: правила из постов, не подгонка OHLC под вход."""

import pytest

from impulse_bot.app.main import working_capital
from impulse_bot.signals import (
    Burst,
    Cluster,
    Tape,
    clamp_mkt_qty,
    cluster_from_prints,
    detect_burst,
    in_session,
    in_universe,
    should_enter,
    tape_from_prints,
    tape_ok,
)


def test_universe_skips_majors_and_band():
    skip = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert not in_universe("BTCUSDT", 1_000_000, skip=skip, lo=100_000, hi=15e6)
    assert not in_universe("FOOUSDT", 50_000, skip=skip, lo=100_000, hi=15e6)
    assert not in_universe("FOOUSDT", 20_000_000, skip=skip, lo=100_000, hi=15e6)
    assert not in_universe("FOOBTC", 1_000_000, skip=skip, lo=100_000, hi=15e6)
    assert in_universe("FOOUSDT", 1_000_000, skip=skip, lo=100_000, hi=15e6)


def test_burst_needs_usd_and_move():
    # нет удара по обороту
    assert detect_burst("X", 1.0, 100_000, 1.01, 110_000,
                        burst_usd=30_000, move_pct=0.2) is None
    # удар есть, хода нет
    assert detect_burst("X", 1.0, 100_000, 1.001, 140_000,
                        burst_usd=30_000, move_pct=0.2) is None
    up = detect_burst("X", 1.0, 100_000, 1.003, 140_000,
                      burst_usd=30_000, move_pct=0.2)
    assert up is not None and up.side == "Buy"
    dn = detect_burst("X", 1.0, 100_000, 0.997, 140_000,
                      burst_usd=30_000, move_pct=0.2)
    assert dn is not None and dn.side == "Sell"


def test_tape_and_cluster_both_required():
    burst = Burst("X", 0.3, 40_000, "Buy")
    tape = Tape(12_000, 5_000)
    cl = Cluster(0.40)
    assert tape_ok(tape, "Buy", ratio=1.2)
    assert should_enter(burst, tape, cl, tape_ratio=1.2)
    assert not should_enter(burst, Tape(5_000, 12_000), cl, tape_ratio=1.2)
    assert not should_enter(burst, tape, Cluster(0.10), tape_ratio=1.2)
    assert not should_enter(None, tape, cl, tape_ratio=1.2)


def test_cluster_pocket_follows_side():
    prints = [(1.00, 10), (1.01, 10), (1.10, 80)]
    assert cluster_from_prints(prints, "Buy").dir_frac >= 0.30
    assert cluster_from_prints(prints, "Sell").dir_frac < 0.30
    assert tape_from_prints([("Buy", 10), ("Sell", 3)]).buy_usd == 10


def test_telegram_texts():
    from impulse_bot.telegram import TelegramNotifier, esc, fmt_enter, fmt_exit

    assert "&lt;" in esc("<FOO>")
    text = fmt_enter(symbol="AAAUSDT", side="Buy", qty=1.5, px=1.0,
                     sl=0.9975, tp=1.0045)
    assert "[impulse]" in text and "вход" in text and "AAAUSDT" in text
    out = fmt_exit(symbol="AAAUSDT", side="Buy", qty=1.5, entry=1.0,
                   exit_px=1.0045, pnl_usd=0.00675, reason="tp")
    assert "выход" in out and "tp" in out
    assert not TelegramNotifier("", "", enabled=True).active


def test_clamp_mkt_qty_caps_and_keeps_under():
    ok, capped = clamp_mkt_qty(100.0, max_mkt=80.0, min_qty=1.0, step=1.0)
    assert capped and ok == 80.0
    ok2, capped2 = clamp_mkt_qty(50.0, max_mkt=80.0, min_qty=1.0, step=1.0)
    assert not capped2 and ok2 == 50.0
    assert clamp_mkt_qty(0.4, max_mkt=80.0, min_qty=1.0, step=1.0) is None


def test_london_session():
    assert in_session(10, 7, 16)
    assert not in_session(6, 7, 16)
    assert not in_session(16, 7, 16)


def test_working_capital_caps_the_fat_demo_account():
    """Риск 1.5% считается от тысячи, не от $47k."""
    assert working_capital(47600.0, 1000.0) == pytest.approx(1000.0)
    assert working_capital(47600.0, 1000.0) * 0.015 == pytest.approx(15.0)


def test_working_capital_does_not_invent_money_if_wallet_is_smaller():
    assert working_capital(400.0, 1000.0) == pytest.approx(400.0)


def test_working_capital_zero_limit_means_use_the_live_wallet():
    assert working_capital(47600.0, 0.0) == pytest.approx(47600.0)


# ─── Учёт: снимок сигнала и фактические цены ──────────────────────────
# Проверяем только хранение. Торговые решения от этих полей не зависят.


def test_signal_snapshot_survives_close(tmp_path):
    """Снимок сигнала переезжает из positions в trades при закрытии."""
    from impulse_bot.db import ImpulseDB, SignalSnapshot

    db = ImpulseDB(str(tmp_path / "t.sqlite"))
    snap = SignalSnapshot(burst_usd=45_000, move_pct=0.31, tape_buy=12_000,
                          tape_sell=5_000, cluster_frac=0.42,
                          turnover24h=2_500_000)
    db.open_pos("AAAUSDT", "Buy", 10.0, 1.0, 0.9975, 1.0045, "impulse_x",
                signal=snap)
    held = db.owned("AAAUSDT")
    assert held is not None
    assert held["burst_usd"] == pytest.approx(45_000)
    assert held["cluster_frac"] == pytest.approx(0.42)

    db.close_pos("AAAUSDT", 1.004, "time_scratch",
                 exit_real=1.0041, pnl_net=-0.37)
    row = db._db.execute(
        "SELECT burst_usd, move_pct, tape_buy, tape_sell, cluster_frac, "
        "turnover24h, exit_real, pnl_net FROM trades").fetchone()
    assert row[0] == pytest.approx(45_000)
    assert row[1] == pytest.approx(0.31)
    assert row[4] == pytest.approx(0.42)
    assert row[5] == pytest.approx(2_500_000)
    assert row[6] == pytest.approx(1.0041)
    assert row[7] == pytest.approx(-0.37)


def test_real_prices_kept_separately_from_ticker_prices(tmp_path):
    """Цена решения и цена филла хранятся раздельно, одна не затирает другую."""
    from impulse_bot.db import ImpulseDB

    db = ImpulseDB(str(tmp_path / "t.sqlite"))
    db.open_pos("BBBUSDT", "Sell", 5.0, 2.0, 2.005, 1.991, "impulse_y")
    assert db.owned("BBBUSDT")["entry_real"] is None
    db.set_entry_real("BBBUSDT", 1.9987)
    assert db.owned("BBBUSDT")["entry_real"] == pytest.approx(1.9987)

    db.close_pos("BBBUSDT", 1.995, "broker_flat",
                 exit_real=1.9942, pnl_net=0.21)
    entry, entry_real, exit_px, exit_real, pnl_usd, pnl_net = db._db.execute(
        "SELECT entry, entry_real, exit, exit_real, pnl_usd, pnl_net "
        "FROM trades").fetchone()
    assert entry == pytest.approx(2.0)
    assert entry_real == pytest.approx(1.9987)
    assert exit_px == pytest.approx(1.995)
    assert exit_real == pytest.approx(1.9942)
    # pnl_usd остаётся расчётным по ценам тикера, pnl_net — фактический
    assert pnl_usd == pytest.approx((2.0 - 1.995) * 5.0)
    assert pnl_net == pytest.approx(0.21)


def test_close_without_exchange_data_still_records(tmp_path):
    """Если closed_pnl недоступен, сделка всё равно попадает в trades."""
    from impulse_bot.db import ImpulseDB

    db = ImpulseDB(str(tmp_path / "t.sqlite"))
    db.open_pos("CCCUSDT", "Buy", 1.0, 10.0, 9.975, 10.045, "impulse_z")
    db.close_pos("CCCUSDT", 10.02, "broker_flat")
    row = db._db.execute(
        "SELECT exit, exit_real, pnl_net FROM trades").fetchone()
    assert row[0] == pytest.approx(10.02)
    assert row[1] is None and row[2] is None
    assert db.open_count() == 0


def test_schema_migration_keeps_old_rows(tmp_path):
    """Открытие старой БД добавляет колонки, не теряя записи."""
    import sqlite3

    from impulse_bot.db import ImpulseDB

    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE positions (
             symbol TEXT PRIMARY KEY, side TEXT NOT NULL, qty REAL NOT NULL,
             entry REAL NOT NULL, sl REAL NOT NULL, tp REAL NOT NULL,
             ts_open INTEGER NOT NULL, link_id TEXT NOT NULL)""")
    old.execute(
        """CREATE TABLE trades (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             ts_open INTEGER, ts_close INTEGER, symbol TEXT, side TEXT,
             qty REAL, entry REAL, exit REAL, pnl_usd REAL, reason TEXT)""")
    old.execute("INSERT INTO trades (symbol, side, pnl_usd, reason) "
                "VALUES ('OLDUSDT', 'Buy', -1.25, 'broker_flat')")
    old.commit()
    old.close()

    db = ImpulseDB(path)
    symbol, pnl, burst = db._db.execute(
        "SELECT symbol, pnl_usd, burst_usd FROM trades").fetchone()
    assert symbol == "OLDUSDT"
    assert pnl == pytest.approx(-1.25)
    assert burst is None
    # новая запись рядом со старой работает
    db.open_pos("NEWUSDT", "Buy", 1.0, 1.0, 0.99, 1.01, "impulse_n")
    assert db.open_count() == 1
