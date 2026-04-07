"""Мониторинг открытых позиций: SL, trailing stop, time-stops, dead positions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.stats.store import PositionRow, StatsStore
from fx_pro_bot.strategies.exits import update_paper_positions

log = logging.getLogger(__name__)

LEADERS_HARD_STOP_HOURS = 168.0

OUTSIDERS_TIME_STOPS = [
    (1.0, -90.0),
    (2.0, -60.0),
    (4.0, -40.0),
    (8.0, -20.0),
]
OUTSIDERS_HARD_STOP_HOURS = 24.0
OUTSIDERS_HARD_STOP_MIN_PROFIT = 50.0
OUTSIDERS_AGGRESSIVE_TP = 10.0

OUTSIDERS_CONFIRMED_TIME_STOPS = [
    (2.0, -60.0),
    (4.0, -40.0),
    (8.0, -20.0),
    (16.0, -10.0),
]
OUTSIDERS_CONFIRMED_HARD_STOP_HOURS = 36.0
OUTSIDERS_CONFIRMED_HARD_STOP_MIN_PROFIT = 40.0
OUTSIDERS_CONFIRMED_AGGRESSIVE_TP = 10.0

SCALPING_HARD_STOP_HOURS = 12.0
SCALPING_TP_PIPS = 8.0
SCALPING_TRAIL_TRIGGER_PIPS = 5.0
SCALPING_TRAIL_DISTANCE_PIPS = 3.0

GLOBAL_HARD_STOP_HOURS = 72.0

DEAD_ATR_MULT = 1.5


class PositionMonitor:
    """Мониторинг всех открытых позиций каждый цикл."""

    def __init__(self, store: StatsStore, *, outsiders_mode: str = "classic", lot_size: float = 0.01) -> None:
        self._store = store
        self._outsiders_mode = outsiders_mode
        self._lot_size = lot_size

    def run(self, prices: dict[str, float], atrs: dict[str, float]) -> dict[str, int]:
        """Обновить все позиции, проверить стопы. Возвращает статистику действий."""
        stats = {"updated": 0, "closed_sl": 0, "closed_trail": 0, "closed_time": 0, "closed_tp": 0}

        for pos in self._store.get_open_positions():
            price = prices.get(pos.instrument)
            if price is None:
                continue

            ps = pip_size(pos.instrument)
            atr = atrs.get(pos.instrument, price * 0.005)

            pips = _calc_pips(pos.direction, pos.entry_price, price, ps)
            pct = pips * ps / pos.entry_price * 100 if pos.entry_price else 0.0

            peak = max(pos.peak_price, price) if pos.direction == "long" else min(pos.peak_price, price)
            trough = min(pos.trough_price, price) if pos.direction == "long" else max(pos.trough_price, price)

            trail_price = pos.trail_price
            trail_activated = pos.trail_activated
            peak_pips = _calc_pips(pos.direction, pos.entry_price, peak, ps)

            if pos.strategy == "leaders" and trail_price > 0 and peak_pips > 0:
                trail_activated = True
                if pos.direction == "long":
                    new_trail_level = peak - trail_price
                    trail_price = max(trail_price, new_trail_level - pos.entry_price)
                else:
                    new_trail_level = peak + trail_price
                    trail_price = max(trail_price, pos.entry_price - new_trail_level)

            self._store.update_position_price(
                pos.id, price, pips, pct, peak, trough, trail_price, trail_activated,
            )
            stats["updated"] += 1

            exit_reason = self._check_exits(pos, price, pips, peak_pips, atr, ps)
            if exit_reason:
                self._store.close_position(pos.id, exit_reason)
                self._close_all_papers(pos.id)
                category = "closed_sl" if "stop_loss" in exit_reason or "dead" in exit_reason else (
                    "closed_trail" if "trail" in exit_reason else (
                        "closed_tp" if "tp" in exit_reason else "closed_time"
                    )
                )
                stats[category] = stats.get(category, 0) + 1
                log.info(
                    "  CLOSE %s: %s %s → %+.1f pips (%s)",
                    pos.strategy.upper(), display_name(pos.instrument),
                    pos.direction.upper(), pips, exit_reason,
                )
            else:
                update_paper_positions(
                    self._store, pos.id, price, pos.direction,
                    atr, ps, pos.entry_price, pos.created_at,
                )

        return stats

    def _check_exits(
        self, pos: PositionRow, price: float,
        pips: float, peak_pips: float, atr: float, ps: float,
    ) -> str:
        if pos.direction == "long" and price <= pos.stop_loss_price and pos.stop_loss_price > 0:
            return "stop_loss"
        if pos.direction == "short" and price >= pos.stop_loss_price and pos.stop_loss_price > 0:
            return "stop_loss"

        if pos.strategy == "leaders" and pos.trail_activated and pos.trail_price > 0:
            atr_pips = atr / ps if ps > 0 else 0
            trail_dist = 0.7 * atr_pips
            if peak_pips - pips > trail_dist and peak_pips > trail_dist:
                return "trailing"

        age_hours = self._position_age_hours(pos)

        if pos.strategy == "outsiders":
            if pips >= OUTSIDERS_CONFIRMED_AGGRESSIVE_TP:
                return "aggressive_tp"
            if peak_pips >= 5.0 and (peak_pips - pips) >= 3.0:
                return "outsiders_trail"
            if self._outsiders_mode == "confirmed":
                for hours_limit, pips_limit in OUTSIDERS_CONFIRMED_TIME_STOPS:
                    if age_hours >= hours_limit and pips <= pips_limit:
                        return f"time_stop_{hours_limit:.0f}h"
                if age_hours >= OUTSIDERS_CONFIRMED_HARD_STOP_HOURS and pips < OUTSIDERS_CONFIRMED_HARD_STOP_MIN_PROFIT:
                    return "hard_stop_36h"
            else:
                for hours_limit, pips_limit in OUTSIDERS_TIME_STOPS:
                    if age_hours >= hours_limit and pips <= pips_limit:
                        return f"time_stop_{hours_limit:.0f}h"
                if age_hours >= OUTSIDERS_HARD_STOP_HOURS and pips < OUTSIDERS_HARD_STOP_MIN_PROFIT:
                    return "hard_stop_24h"

        if pos.strategy == "leaders" and age_hours >= LEADERS_HARD_STOP_HOURS:
            return "leaders_time_7d"

        scalping = ("vwap_reversion", "stat_arb", "session_orb")
        if pos.strategy in scalping:
            if pips >= SCALPING_TP_PIPS:
                return "scalp_tp"
            if peak_pips >= SCALPING_TRAIL_TRIGGER_PIPS and (peak_pips - pips) >= SCALPING_TRAIL_DISTANCE_PIPS:
                return "scalp_trail"
            if age_hours >= SCALPING_HARD_STOP_HOURS:
                return "scalp_time_12h"

        if age_hours >= GLOBAL_HARD_STOP_HOURS:
            return "global_time_72h"

        atr_pips = atr / ps if ps > 0 else 0
        if atr_pips > 0 and pips < -DEAD_ATR_MULT * atr_pips:
            return "dead"

        return ""

    def _position_age_hours(self, pos: PositionRow) -> float:
        try:
            created = datetime.fromisoformat(pos.created_at)
            return (datetime.now(tz=UTC) - created).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0.0

    def _close_all_papers(self, position_id: str) -> None:
        papers = self._store.get_open_paper_positions(position_id=position_id)
        for pp in papers:
            self._store.close_paper_position(pp.id, "parent_closed")


def _calc_pips(direction: str, entry: float, current: float, ps: float) -> float:
    if ps == 0:
        return 0.0
    if direction == "long":
        return (current - entry) / ps
    return (entry - current) / ps
