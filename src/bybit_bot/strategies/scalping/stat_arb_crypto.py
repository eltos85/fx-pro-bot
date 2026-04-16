"""Stat-Arb для крипто-пар (BTC/ETH, SOL/ETH и др.).

Статистический арбитраж между коинтегрированными крипто-парами.
Когда spread (z-score) расходится на ±2σ — вход на возврат к среднему.
Market-neutral: одновременно long одну и short другую.

Коинтеграция BTC-ETH: корреляция 0.75-0.82 (Springer Nature, 2024; Racthera, 2025).
При корреляции < 0.5 — mean reversion success rate 73% (FullSwing AI).
Риск: корреляция ослабевает в бычий рынок (инвесторы бегут в альты).
ETH vol 55-75% vs BTC 45-65% — учитывать при sizing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import (
    adf_pvalue,
    ols_hedge_ratio,
    rolling_z_score,
    spread_series,
)

log = logging.getLogger(__name__)

DEFAULT_PAIRS: list[tuple[str, str]] = [
    # Только ADF-подтверждённые пары (p < 0.05, 5d/5m, 2026-04-13).
    # Остальные 14 пар убраны: спреды нестационарны на текущих данных.
    ("SOLUSDT", "LINKUSDT"),    # corr 0.82, ADF p=0.0012
    ("SOLUSDT", "WIFUSDT"),     # corr 0.68, ADF p=0.0055
]

Z_ENTRY = 2.5
Z_EXIT = 0.5
LOOKBACK = 100
ZSCORE_WINDOW = 50
SL_ATR_MULT = 2.0
# Минимальная корреляция для входа в пару.
# При корреляции < 0.5 коинтеграция нестабильна (Crypto Economy, 2025).
MIN_CORRELATION = 0.5


@dataclass(frozen=True, slots=True)
class StatArbSignal:
    pair_tag: str
    symbol_a: str
    symbol_b: str
    z_score: float
    beta: float
    direction_a: Direction
    direction_b: Direction
    atr_a: float
    atr_b: float
    price_a: float
    price_b: float


class StatArbCryptoStrategy:
    """Парный трейдинг на коинтегрированных крипто-парах."""

    def __init__(
        self,
        *,
        pairs: list[tuple[str, str]] | None = None,
        max_pairs: int = 4,
    ) -> None:
        self._pairs = pairs or DEFAULT_PAIRS
        self._max_pairs = max_pairs

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

            corr = self._correlation(ca[-LOOKBACK:], cb[-LOOKBACK:])
            if corr < MIN_CORRELATION:
                log.debug("%s/%s: корреляция %.2f < %.2f, пропускаю", sym_a, sym_b, corr, MIN_CORRELATION)
                continue

            beta = ols_hedge_ratio(ca[-LOOKBACK:], cb[-LOOKBACK:])
            sprd = spread_series(ca, cb, beta)

            pval = adf_pvalue(sprd[-LOOKBACK:])
            if pval > 0.05:
                log.debug("%s/%s: ADF p=%.3f > 0.05, спред нестационарен", sym_a, sym_b, pval)
                continue

            z = rolling_z_score(sprd, ZSCORE_WINDOW)

            log.debug("%s/%s: corr=%.2f β=%.4f z=%.2f adf_p=%.3f (нужно |z|>%.1f)",
                      sym_a, sym_b, corr, beta, z, pval, Z_ENTRY)

            if abs(z) < Z_ENTRY:
                continue

            atr_a = atr(bars_a)
            atr_b = atr(bars_b)

            if z > Z_ENTRY:
                dir_a = Direction.SHORT
                dir_b = Direction.LONG
            else:
                dir_a = Direction.LONG
                dir_b = Direction.SHORT

            pair_tag = f"sa_{sym_a}_{sym_b}_{uuid.uuid4().hex[:6]}"
            signals.append(StatArbSignal(
                pair_tag=pair_tag,
                symbol_a=sym_a,
                symbol_b=sym_b,
                z_score=round(z, 2),
                beta=round(beta, 4),
                direction_a=dir_a,
                direction_b=dir_b,
                atr_a=atr_a,
                atr_b=atr_b,
                price_a=bars_a[-1].close,
                price_b=bars_b[-1].close,
            ))

        return signals

    @staticmethod
    def _correlation(a: list[float], b: list[float]) -> float:
        """Pearson correlation между двумя сериями."""
        import math
        n = min(len(a), len(b))
        if n < 10:
            return 0.0
        mean_a = sum(a[-n:]) / n
        mean_b = sum(b[-n:]) / n
        cov = sum((a[-n + i] - mean_a) * (b[-n + i] - mean_b) for i in range(n)) / n
        var_a = sum((a[-n + i] - mean_a) ** 2 for i in range(n)) / n
        var_b = sum((b[-n + i] - mean_b) ** 2 for i in range(n)) / n
        denom = math.sqrt(var_a * var_b)
        return cov / denom if denom > 0 else 0.0

    def check_exits(self, bars_map: dict[str, list[Bar]], open_pair_tags: list[str]) -> list[str]:
        """Вернуть pair_tag'и, которые нужно закрыть (z-score вернулся к среднему)."""
        to_close: list[str] = []

        for sym_a, sym_b in self._pairs:
            bars_a = bars_map.get(sym_a, [])
            bars_b = bars_map.get(sym_b, [])

            min_bars = LOOKBACK + ZSCORE_WINDOW
            if len(bars_a) < min_bars or len(bars_b) < min_bars:
                continue

            closes_a = [b.close for b in bars_a]
            closes_b = [b.close for b in bars_b]
            n = min(len(closes_a), len(closes_b))

            beta = ols_hedge_ratio(closes_a[-LOOKBACK:], closes_b[-LOOKBACK:])
            sprd = spread_series(closes_a[-n:], closes_b[-n:], beta)
            z = rolling_z_score(sprd, ZSCORE_WINDOW)

            if abs(z) < Z_EXIT:
                pair_prefix = f"sa_{sym_a}_{sym_b}_"
                for tag in open_pair_tags:
                    if tag.startswith(pair_prefix):
                        to_close.append(tag)
                        log.info("STAT-ARB EXIT: %s z=%.2f (reversion)", tag, z)

        return to_close
