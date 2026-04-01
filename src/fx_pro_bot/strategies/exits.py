"""4 параллельные paper exit-стратегии для outsider-позиций.

progressive — 5 ATR-уровней лесенкой
grid — 4 фиксированных TP в пипсах
hold90 — trailing с порогами активации
scalp — быстрый выход с tight SL и time stop
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.stats.store import PaperPositionRow, StatsStore

log = logging.getLogger(__name__)

EXIT_STRATEGIES = ("progressive", "grid", "hold90", "scalp")

PROGRESSIVE_LEVELS_ATR = (0.5, 1.0, 1.5, 2.0, 3.0)
GRID_LEVELS_PIPS = (10.0, 25.0, 50.0, 100.0)
HOLD90_ACTIVATE_ATR = 1.5
HOLD90_TIGHT_ATR = 3.0
HOLD90_TRAIL_PCT = 0.30
SCALP_TP_ATR = 1.0
SCALP_SL_ATR = 1.5
SCALP_TIME_HOURS = 4.0


def create_paper_positions(
    store: StatsStore,
    position_id: str,
    entry_price: float,
    direction: TrendDirection,
    atr: float,
    pip_sz: float,
) -> list[str]:
    """Создать 4 paper-позиции для outsider-сигнала."""
    ids: list[str] = []
    for strat in EXIT_STRATEGIES:
        ppid = store.open_paper_position(
            position_id=position_id,
            exit_strategy=strat,
            entry_price=entry_price,
        )
        ids.append(ppid)
    return ids


def update_paper_positions(
    store: StatsStore,
    position_id: str,
    current_price: float,
    direction: str,
    atr: float,
    pip_sz: float,
    entry_price: float,
    created_at_iso: str,
) -> None:
    """Обновить все open paper-позиции для данной позиции."""
    papers = store.get_open_paper_positions(position_id=position_id)
    for pp in papers:
        _update_single_paper(
            store, pp, current_price, direction, atr, pip_sz, entry_price, created_at_iso,
        )


def _profit_pips(direction: str, entry: float, current: float, pip_sz: float) -> float:
    if direction == "long":
        return (current - entry) / pip_sz
    return (entry - current) / pip_sz


def _profit_pct(entry: float, profit_pips: float, pip_sz: float) -> float:
    if entry == 0:
        return 0.0
    return profit_pips * pip_sz / entry * 100


def _update_single_paper(
    store: StatsStore,
    pp: PaperPositionRow,
    current_price: float,
    direction: str,
    atr: float,
    pip_sz: float,
    entry_price: float,
    created_at_iso: str,
) -> None:
    pips = _profit_pips(direction, entry_price, current_price, pip_sz)
    pct = _profit_pct(entry_price, pips, pip_sz)
    peak = max(pp.peak_price, current_price) if direction == "long" else pp.peak_price
    if direction == "short":
        peak = min(pp.peak_price, current_price) if pp.peak_price > 0 else current_price

    peak_pips = _profit_pips(direction, entry_price, peak, pip_sz)

    exit_reason = ""

    if pp.exit_strategy == "progressive":
        exit_reason = _check_progressive(pips, atr, pip_sz, pp.levels_hit)
    elif pp.exit_strategy == "grid":
        exit_reason = _check_grid(pips, pp.levels_hit)
    elif pp.exit_strategy == "hold90":
        exit_reason = _check_hold90(pips, peak_pips, atr, pip_sz)
    elif pp.exit_strategy == "scalp":
        exit_reason = _check_scalp(pips, atr, pip_sz, created_at_iso)

    store.update_paper_position(
        pp.id, current_price, pips, pct, peak,
        levels_hit=pp.levels_hit,
    )

    if exit_reason:
        store.close_paper_position(pp.id, exit_reason)


def _check_progressive(
    pips: float, atr: float, pip_sz: float, levels_hit: list[str],
) -> str:
    atr_pips = atr / pip_sz if pip_sz > 0 else 0
    for i, mult in enumerate(PROGRESSIVE_LEVELS_ATR):
        level_name = f"L{i+1}_{mult}ATR"
        target_pips = mult * atr_pips
        if pips >= target_pips and level_name not in levels_hit:
            levels_hit.append(level_name)

    if len(levels_hit) >= len(PROGRESSIVE_LEVELS_ATR):
        return "progressive_all_levels"

    if pips < -2.0 * atr_pips:
        return "progressive_sl"

    return ""


def _check_grid(pips: float, levels_hit: list[str]) -> str:
    for target in GRID_LEVELS_PIPS:
        level_name = f"G_{target:.0f}p"
        if pips >= target and level_name not in levels_hit:
            levels_hit.append(level_name)

    if len(levels_hit) >= len(GRID_LEVELS_PIPS):
        return "grid_all_levels"

    if pips < -50.0:
        return "grid_sl"

    return ""


def _check_hold90(
    pips: float, peak_pips: float, atr: float, pip_sz: float,
) -> str:
    atr_pips = atr / pip_sz if pip_sz > 0 else 0
    activate = HOLD90_ACTIVATE_ATR * atr_pips
    tight = HOLD90_TIGHT_ATR * atr_pips

    if peak_pips >= tight:
        trail_floor = peak_pips * (1 - HOLD90_TRAIL_PCT * 0.5)
        if pips < trail_floor:
            return "hold90_tight_trail"
    elif peak_pips >= activate:
        trail_floor = peak_pips * (1 - HOLD90_TRAIL_PCT)
        if pips < trail_floor:
            return "hold90_trail"

    if pips < -2.0 * atr_pips:
        return "hold90_sl"

    return ""


def _check_scalp(
    pips: float, atr: float, pip_sz: float, created_at_iso: str,
) -> str:
    atr_pips = atr / pip_sz if pip_sz > 0 else 0

    if pips >= SCALP_TP_ATR * atr_pips:
        return "scalp_tp"

    if pips <= -SCALP_SL_ATR * atr_pips:
        return "scalp_sl"

    try:
        created = datetime.fromisoformat(created_at_iso)
        age_hours = (datetime.now(tz=UTC) - created).total_seconds() / 3600
        if age_hours >= SCALP_TIME_HOURS:
            return "scalp_time"
    except (ValueError, TypeError):
        pass

    return ""
