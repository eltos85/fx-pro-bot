"""Защитные фильтры входа — проверки перед открытием позиции."""

from __future__ import annotations

import logging

from fx_pro_bot.config.settings import SPREAD_PIPS, pip_size
from fx_pro_bot.stats.store import StatsStore

log = logging.getLogger(__name__)

MAX_SPREAD_MULT = 2.0
MAX_PRICE_DRIFT_ATR = 2.0
MAX_INSTRUMENT_POSITIONS = 3


def check_entry_allowed(
    store: StatsStore,
    *,
    strategy: str,
    instrument: str,
    signal_price: float,
    current_price: float,
    atr: float,
    max_positions: int,
    max_per_instrument: int = MAX_INSTRUMENT_POSITIONS,
) -> tuple[bool, str]:
    """Проверить все фильтры. Возвращает (allowed, reason)."""
    total_open = store.count_open_positions(strategy=strategy)
    if total_open >= max_positions:
        return False, f"лимит позиций {strategy}: {total_open}/{max_positions}"

    instr_open = store.count_open_positions(strategy=strategy, instrument=instrument)
    if instr_open >= max_per_instrument:
        return False, f"лимит на инструмент {instrument}: {instr_open}/{max_per_instrument}"

    if atr > 0 and signal_price > 0:
        drift = abs(current_price - signal_price)
        if drift > MAX_PRICE_DRIFT_ATR * atr:
            return False, f"цена ушла на {drift / atr:.1f} ATR от сигнала"

    spread = SPREAD_PIPS.get(instrument, 2.0)
    ps = pip_size(instrument)
    normal_spread_price = spread * ps
    if atr > 0 and normal_spread_price > MAX_SPREAD_MULT * atr * 0.1:
        return False, f"спред слишком высок для волатильности"

    if current_price <= 0:
        return False, "некорректная цена"

    return True, "ok"
