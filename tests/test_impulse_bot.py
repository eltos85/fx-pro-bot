"""Фильтры impulse-bot: правила из постов, не подгонка OHLC под вход."""

from impulse_bot.signals import (
    Burst,
    Cluster,
    Tape,
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


def test_london_session():
    assert in_session(10, 7, 16)
    assert not in_session(6, 7, 16)
    assert not in_session(16, 7, 16)
