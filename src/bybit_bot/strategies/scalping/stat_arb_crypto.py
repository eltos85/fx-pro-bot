"""[DEPRECATED V1] Stat-Arb для крипто-пар на основе реальных 5m корреляций.

Отключена в V2 (13.04.2026). Заменена на EmaTrendStrategy.

Статистический арбитраж между коинтегрированными крипто-парами.
Когда spread (z-score) расходится на ±2σ — вход на возврат к среднему.
Market-neutral: одновременно long одну и short другую.

Пары подобраны по реальным Pearson-корреляциям на 5m данных (100 баров),
а не по академическим исследованиям на дневных данных. Каждый символ
участвует максимум в 2 парах — защита от концентрации риска.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import (
    ols_hedge_ratio,
    rolling_z_score,
    spread_series,
)

log = logging.getLogger(__name__)

DEFAULT_PAIRS: list[tuple[str, str]] = [
    # Подобраны по реальным 5m корреляциям (Pearson, 100 баров, 13.04.2026).
    # Каждый символ макс в 2 парах — защита от концентрации.
    ("BTCUSDT", "LINKUSDT"),    # corr 0.92, оба major/infra
    ("SUIUSDT", "ETCUSDT"),     # corr 0.92, L1/PoW fork
    ("LINKUSDT", "PENDLEUSDT"), # corr 0.93, DeFi infra
    ("FILUSDT", "TIAUSDT"),     # corr 0.91, storage/DA sector
    ("TIAUSDT", "OPUSDT"),      # corr 0.90, modular/L2
    ("DOGEUSDT", "SUIUSDT"),    # corr 0.92, meme/L1
    ("LTCUSDT", "BTCUSDT"),     # corr 0.79, legacy PoW
    ("DOGEUSDT", "XRPUSDT"),    # corr 0.79, payment/meme
]

Z_ENTRY = 2.0
Z_EXIT = 0.5
LOOKBACK = 100
ZSCORE_WINDOW = 50
SL_ATR_MULT = 2.0
# Минимальная корреляция для входа в пару.
# 0.5 давал слишком много слабых пар — по сути две независимые позиции.
# 0.7 отсекает нестабильную коинтеграцию на 5m таймфрейме.
MIN_CORRELATION = 0.7


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
            z = rolling_z_score(sprd, ZSCORE_WINDOW)

            log.debug("%s/%s: corr=%.2f β=%.4f z=%.2f (нужно |z|>%.1f)", sym_a, sym_b, corr, beta, z, Z_ENTRY)

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
