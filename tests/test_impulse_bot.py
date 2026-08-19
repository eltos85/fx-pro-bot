"""Фильтры impulse-bot: правила из постов, не подгонка OHLC под вход."""

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
