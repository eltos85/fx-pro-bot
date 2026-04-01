"""Shadow analytics — ROI-снимки для всех открытых позиций."""

from __future__ import annotations

import logging

from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.stats.store import StatsStore

log = logging.getLogger(__name__)


class ShadowTracker:
    """Фоновый трекинг ROI-экстремумов для каждой открытой позиции."""

    def __init__(self, store: StatsStore) -> None:
        self._store = store
        self._peak_pips: dict[str, float] = {}
        self._max_dd: dict[str, float] = {}

    def run(self, prices: dict[str, float]) -> int:
        """Снять ROI-снимок для всех открытых позиций. Возвращает кол-во записей."""
        count = 0
        for pos in self._store.get_open_positions():
            price = prices.get(pos.instrument)
            if price is None:
                continue

            ps = pip_size(pos.instrument)
            if ps == 0:
                continue

            if pos.direction == "long":
                pips = (price - pos.entry_price) / ps
            else:
                pips = (pos.entry_price - price) / ps

            pct = pips * ps / pos.entry_price * 100 if pos.entry_price else 0.0

            prev_peak = self._peak_pips.get(pos.id, 0.0)
            peak_pips = max(prev_peak, pips)
            self._peak_pips[pos.id] = peak_pips
            peak_pct = peak_pips * ps / pos.entry_price * 100 if pos.entry_price else 0.0

            prev_dd = self._max_dd.get(pos.id, 0.0)
            dd = peak_pips - pips
            max_dd = max(prev_dd, dd)
            self._max_dd[pos.id] = max_dd

            self._store.record_shadow(
                position_id=pos.id,
                price=price,
                profit_pips=round(pips, 2),
                profit_pct=round(pct, 4),
                peak_profit_pips=round(peak_pips, 2),
                peak_profit_pct=round(peak_pct, 4),
                max_drawdown_pips=round(max_dd, 2),
            )
            count += 1

        return count

    def log_summary(self) -> None:
        summary = self._store.shadow_summary()
        if not summary:
            return

        log.info("── Shadow Analytics ──")
        for row in summary:
            log.info(
                "  %s: %d позиций, пик %+.1f пунктов, просадка -%.1f, средний пик %+.1f",
                row["strategy"],
                row["positions_tracked"],
                row["best_peak"],
                abs(row["worst_dd"]),
                row["avg_peak"],
            )
