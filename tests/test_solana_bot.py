"""Фильтры solana-bot: пороги из Teletype, не синтетические пампы."""

from solana_bot.signals import (
    QUOTE_MINTS,
    USDC,
    WSOL,
    Shield,
    exit_reason,
    should_enter,
    universe_ok,
)


def _sh(**kw) -> Shield:
    base = dict(
        mint="Token1111111111111111111111111111111111111",
        symbol="FOO",
        quote_mint=WSOL,
        volume_m5=120_000,
        move_m5_pct=8.0,
        liquidity_usd=40_000,
        age_sec=3600,
        price_usd=0.001,
    )
    base.update(kw)
    return Shield(**base)


def test_universe_volume_liq_age_quote():
    kw = dict(vol_lo=100_000, liq_lo=25_000, age_lo=1800, skip=set())
    assert universe_ok(_sh(), **kw)
    assert not universe_ok(_sh(volume_m5=80_000), **kw)
    assert not universe_ok(_sh(liquidity_usd=10_000), **kw)
    assert not universe_ok(_sh(age_sec=60), **kw)
    assert not universe_ok(_sh(mint=WSOL), **kw)
    assert not universe_ok(_sh(quote_mint="RandomMint111111111111111111111111111"), **kw)
    assert universe_ok(_sh(quote_mint=USDC), **kw)
    assert USDC in QUOTE_MINTS


def test_enter_needs_move():
    assert should_enter(_sh(move_m5_pct=5.0), move_min_pct=5.0)
    assert not should_enter(_sh(move_m5_pct=1.0), move_min_pct=5.0)


def test_telegram_candidate_and_cooldown():
    from solana_bot.telegram import (
        TelegramNotifier,
        fmt_candidate,
        should_alert,
    )

    text = fmt_candidate(symbol="FOO", mint="Token1111111111111111111111111111111111111",
                         volume_m5=120_000, move_m5_pct=8.0,
                         liquidity_usd=40_000, price_usd=0.001)
    assert "[solana]" in text and "кандидат" in text and "FOO" in text
    last: dict[str, float] = {}
    assert should_alert(last, "m", 100.0, 1800)
    assert not should_alert(last, "m", 200.0, 1800)
    assert should_alert(last, "m", 2000.0, 1800)
    assert not TelegramNotifier("", "", enabled=True).active


def test_bot_is_off_by_default():
    from solana_bot.settings import SolanaSettings

    cfg = SolanaSettings()
    assert cfg.enabled is False
    assert cfg.telegram_enabled is False


def test_exits_tp_cap_sl():
    assert exit_reason(1.0, 1.07, tp_pct=7, cap_pct=30, sl_pct=12) == "tp"
    assert exit_reason(1.0, 1.35, tp_pct=7, cap_pct=30, sl_pct=12) == "cap"
    assert exit_reason(1.0, 0.85, tp_pct=7, cap_pct=30, sl_pct=12) == "sl"
    assert exit_reason(1.0, 1.02, tp_pct=7, cap_pct=30, sl_pct=12) is None
