"""Stat-Arb Cross-Pair Spread Scalping.

Статистический арбитраж между коинтегрированными валютными парами.
Когда spread (z-score) расходится на ±2σ — вход на возврат к среднему.
Market-neutral: одновременно long одну пару и short другую.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection, _atr
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import (
    ols_hedge_ratio,
    rolling_z_score,
    spread_series,
)

log = logging.getLogger(__name__)

DEFAULT_PAIRS: list[tuple[str, str]] = [
    ("EURUSD=X", "GBPUSD=X"),
    ("AUDUSD=X", "NZDUSD=X"),
    ("USDJPY=X", "USDCAD=X"),
]

Z_ENTRY = 2.0
Z_EXIT = 0.5
LOOKBACK = 100
ZSCORE_WINDOW = 50
SL_ATR_MULT = 2.0


@dataclass(frozen=True, slots=True)
class StatArbSignal:
    pair_id: str
    symbol_a: str
    symbol_b: str
    z_score: float
    beta: float
    direction_a: TrendDirection
    direction_b: TrendDirection
    atr_a: float
    atr_b: float


class StatArbStrategy:
    """Stat-Arb: парный трейдинг на коинтегрированных валютных парах."""

    def __init__(
        self,
        store: StatsStore,
        *,
        pairs: list[tuple[str, str]] | None = None,
        max_positions: int = 20,
        max_per_pair: int = 2,
    ) -> None:
        self._store = store
        self._pairs = pairs or DEFAULT_PAIRS
        self._max_positions = max_positions
        self._max_per_pair = max_per_pair

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[StatArbSignal]:
        signals: list[StatArbSignal] = []

        for sym_a, sym_b in self._pairs:
            bars_a = bars_map.get(sym_a, [])
            bars_b = bars_map.get(sym_b, [])

            min_bars = LOOKBACK + ZSCORE_WINDOW
            if len(bars_a) < min_bars or len(bars_b) < min_bars:
                continue

            closes_a = [b.close for b in bars_a]
            closes_b = [b.close for b in bars_b]

            n = min(len(closes_a), len(closes_b))
            ca = closes_a[-n:]
            cb = closes_b[-n:]

            beta = ols_hedge_ratio(ca[-LOOKBACK:], cb[-LOOKBACK:])
            sprd = spread_series(ca, cb, beta)
            z = rolling_z_score(sprd, ZSCORE_WINDOW)

            if abs(z) < Z_ENTRY:
                continue

            atr_a = _atr(bars_a)
            atr_b = _atr(bars_b)

            if z > Z_ENTRY:
                dir_a = TrendDirection.SHORT
                dir_b = TrendDirection.LONG
            else:
                dir_a = TrendDirection.LONG
                dir_b = TrendDirection.SHORT

            pair_id = f"{sym_a}_{sym_b}"
            signals.append(StatArbSignal(
                pair_id=pair_id,
                symbol_a=sym_a,
                symbol_b=sym_b,
                z_score=round(z, 2),
                beta=round(beta, 4),
                direction_a=dir_a,
                direction_b=dir_b,
                atr_a=atr_a,
                atr_b=atr_b,
            ))

        return signals

    def process_signals(
        self,
        signals: list[StatArbSignal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="stat_arb")

        for sig in signals:
            if current >= self._max_positions:
                break

            pair_count = self._count_pair_positions(sig.pair_id)
            if pair_count >= self._max_per_pair:
                continue

            price_a = prices.get(sig.symbol_a)
            price_b = prices.get(sig.symbol_b)
            if not price_a or not price_b:
                continue

            pair_tag = f"sa_{uuid.uuid4().hex[:8]}"

            sl_a = (
                price_a - SL_ATR_MULT * sig.atr_a
                if sig.direction_a == TrendDirection.LONG
                else price_a + SL_ATR_MULT * sig.atr_a
            )
            sl_b = (
                price_b - SL_ATR_MULT * sig.atr_b
                if sig.direction_b == TrendDirection.LONG
                else price_b + SL_ATR_MULT * sig.atr_b
            )

            self._store.open_position(
                strategy="stat_arb",
                source=pair_tag,
                instrument=sig.symbol_a,
                direction=sig.direction_a.value,
                entry_price=price_a,
                stop_loss_price=sl_a,
            )
            self._store.open_position(
                strategy="stat_arb",
                source=pair_tag,
                instrument=sig.symbol_b,
                direction=sig.direction_b.value,
                entry_price=price_b,
                stop_loss_price=sl_b,
            )

            log.info(
                "  STAT-ARB OPEN: %s %s + %s %s (z=%.2f, β=%.4f, pair=%s)",
                display_name(sig.symbol_a), sig.direction_a.value.upper(),
                display_name(sig.symbol_b), sig.direction_b.value.upper(),
                sig.z_score, sig.beta, pair_tag,
            )
            opened += 2
            current += 2

        return opened

    def check_exits(self, bars_map: dict[str, list[Bar]]) -> int:
        """Закрыть пары, у которых z-score вернулся в зону выхода."""
        closed = 0
        open_positions = self._store.get_open_positions(strategy="stat_arb")

        pair_groups: dict[str, list] = {}
        for pos in open_positions:
            pair_groups.setdefault(pos.source, []).append(pos)

        for pair_tag, positions in pair_groups.items():
            if len(positions) != 2:
                continue

            pos_a, pos_b = positions[0], positions[1]
            bars_a = bars_map.get(pos_a.instrument, [])
            bars_b = bars_map.get(pos_b.instrument, [])

            if len(bars_a) < LOOKBACK + ZSCORE_WINDOW or len(bars_b) < LOOKBACK + ZSCORE_WINDOW:
                continue

            closes_a = [b.close for b in bars_a]
            closes_b = [b.close for b in bars_b]
            n = min(len(closes_a), len(closes_b))

            beta = ols_hedge_ratio(closes_a[-LOOKBACK:], closes_b[-LOOKBACK:])
            sprd = spread_series(closes_a[-n:], closes_b[-n:], beta)
            z = rolling_z_score(sprd, ZSCORE_WINDOW)

            if abs(z) < Z_EXIT:
                self._store.close_position(pos_a.id, f"stat_arb_revert_z={z:.2f}")
                self._store.close_position(pos_b.id, f"stat_arb_revert_z={z:.2f}")
                log.info(
                    "  STAT-ARB EXIT: pair=%s z=%.2f (reversion)",
                    pair_tag, z,
                )
                closed += 2

        return closed

    def _count_pair_positions(self, pair_id: str) -> int:
        positions = self._store.get_open_positions(strategy="stat_arb")
        count = 0
        seen_sources: set[str] = set()
        for p in positions:
            syms = {p.instrument for p2 in positions if p2.source == p.source}
            pair_key = "_".join(sorted(syms))
            if pair_id in pair_key or pair_key in pair_id:
                if p.source not in seen_sources:
                    seen_sources.add(p.source)
                    count += 1
        return count
