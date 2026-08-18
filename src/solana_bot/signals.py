"""Правила входа/выхода щитков. Числа из постов, не из наших бэктестов.

─── Research basis ───
- объём ≥$100k / 5 мин, цели +7%…+30%: Teletype lexdollar
  (скальп щитков Solana).
- SL −12%, мин. ликвидность, мин. возраст пула — риск-капы: в источнике
  нет стопа, без них автомат держит rug до нуля.
"""

from __future__ import annotations

from dataclasses import dataclass


WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({WSOL, USDC, USDT})


@dataclass(frozen=True)
class Shield:
    mint: str
    symbol: str
    quote_mint: str
    volume_m5: float
    move_m5_pct: float
    liquidity_usd: float
    age_sec: float
    price_usd: float


def universe_ok(s: Shield, *, vol_lo: float, liq_lo: float, age_lo: float,
                skip: set[str]) -> bool:
    if not s.mint or s.mint in skip or s.mint == WSOL:
        return False
    if s.quote_mint not in QUOTE_MINTS:
        return False
    if s.volume_m5 < vol_lo:
        return False
    if s.liquidity_usd < liq_lo:
        return False
    if s.age_sec < age_lo:
        return False
    return True


def should_enter(s: Shield, *, move_min_pct: float) -> bool:
    """Всплеск объёма уже в universe_ok. Здесь — ход за 5 мин."""
    return s.move_m5_pct >= move_min_pct


def exit_reason(entry: float, px: float, *, tp_pct: float, cap_pct: float,
                sl_pct: float) -> str | None:
    if entry <= 0 or px <= 0:
        return None
    pnl = (px / entry - 1.0) * 100.0
    if pnl >= cap_pct:
        return "cap"
    if pnl >= tp_pct:
        return "tp"
    if pnl <= -sl_pct:
        return "sl"
    return None
