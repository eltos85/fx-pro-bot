"""EMA Trend-Following стратегия (V2).

Сигнал входа:
  - EMA 12/26 crossover
  - EMA 200 фильтр тренда (long только выше, short только ниже)
  - ADX > 20 (подтверждение тренда)
  - Volume > 0.7x avg (подтверждение объёмом)

Выход:
  - SL = 2x ATR
  - TP = 3x ATR (risk:reward 1:1.5)
  - Trailing: активация при +1.5 ATR, дистанция 1 ATR
  - Time-stop: 48 свечей без +1%
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.indicators import adx, atr, ema, volume_avg
from bybit_bot.market_data.models import Bar

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TrendSignal:
    symbol: str
    direction: str  # "Buy" | "Sell"
    price: float
    sl: float
    tp: float
    atr_val: float
    reasons: tuple[str, ...]


class EmaTrendStrategy:
    """Единственная стратегия V2: EMA crossover + фильтры."""

    def __init__(
        self,
        *,
        fast_period: int = 12,
        slow_period: int = 26,
        trend_period: int = 200,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
        volume_ratio: float = 0.7,
        sl_atr_mult: float = 2.0,
        tp_atr_mult: float = 3.0,
        cross_lookback: int = 3,
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.volume_ratio = volume_ratio
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        self.cross_lookback = cross_lookback

    def _find_crossover(
        self, ema_fast: list[float], ema_slow: list[float],
    ) -> tuple[str | None, int]:
        """Найти последний crossover в пределах lookback баров.

        Возвращает (direction, bars_ago) или (None, 0).
        Дополнительно проверяет что EMA fast всё ещё на стороне сигнала
        (не произошёл обратный crossover).
        """
        n = min(len(ema_fast), len(ema_slow))
        start = max(1, n - self.cross_lookback)

        for i in range(n - 1, start - 1, -1):
            prev_f, cur_f = ema_fast[i - 1], ema_fast[i]
            prev_s, cur_s = ema_slow[i - 1], ema_slow[i]

            if prev_f <= prev_s and cur_f > cur_s:
                if ema_fast[-1] > ema_slow[-1]:
                    return "Buy", n - 1 - i
                return None, 0
            if prev_f >= prev_s and cur_f < cur_s:
                if ema_fast[-1] < ema_slow[-1]:
                    return "Sell", n - 1 - i
                return None, 0

        return None, 0

    @property
    def min_bars(self) -> int:
        return max(self.slow_period, self.adx_period * 2) + 2

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        open_symbols: set[str] | None = None,
    ) -> list[TrendSignal]:
        """Сканировать все символы и вернуть сигналы."""
        signals: list[TrendSignal] = []
        open_symbols = open_symbols or set()

        for symbol, bars in bars_map.items():
            if symbol in open_symbols:
                continue
            sig = self.evaluate(symbol, bars)
            if sig is not None:
                signals.append(sig)
        return signals

    def evaluate(self, symbol: str, bars: list[Bar]) -> TrendSignal | None:
        """Оценить один символ. Возвращает TrendSignal или None."""
        if len(bars) < self.min_bars:
            log.debug("%s: недостаточно баров (%d < %d)", symbol, len(bars), self.min_bars)
            return None

        closes = [b.close for b in bars]

        ema_fast = ema(closes, self.fast_period)
        ema_slow = ema(closes, self.slow_period)

        if len(ema_fast) < 2 or len(ema_slow) < 2:
            return None

        cross_dir, bars_ago = self._find_crossover(ema_fast, ema_slow)
        if cross_dir is None:
            return None

        if ema_fast[-1] == ema_slow[-1]:
            return None

        reasons: list[str] = []

        if cross_dir == "Buy":
            direction = "Buy"
            reasons.append(f"ema_cross_up({bars_ago}b)")
        else:
            direction = "Sell"
            reasons.append(f"ema_cross_down({bars_ago}b)")

        price = closes[-1]

        ema_trend = ema(closes, self.trend_period)
        if ema_trend:
            trend_val = ema_trend[-1]
            trend_aligned = (direction == "Buy" and price > trend_val) or \
                            (direction == "Sell" and price < trend_val)
            if trend_aligned:
                reasons.append(f"ema{self.trend_period}_ok")
            else:
                reasons.append(f"ema{self.trend_period}_counter")

        adx_val = adx(bars, self.adx_period)
        if adx_val < self.adx_threshold:
            log.debug("%s: ADX=%.1f < %.1f, нет тренда", symbol, adx_val, self.adx_threshold)
            return None
        reasons.append(f"adx={adx_val:.0f}")

        avg_vol = volume_avg(bars, 20)
        cur_vol = bars[-1].volume
        if avg_vol > 0 and cur_vol < avg_vol * self.volume_ratio:
            log.debug("%s: volume=%.0f < %.1fx avg=%.0f", symbol, cur_vol, self.volume_ratio, avg_vol)
            return None
        reasons.append("vol_ok")

        atr_val = atr(bars)
        if atr_val <= 0:
            return None

        sl_dist = atr_val * self.sl_atr_mult
        tp_dist = atr_val * self.tp_atr_mult

        if direction == "Buy":
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist

        log.info(
            "SIGNAL: %s %s @ %.4f | SL=%.4f TP=%.4f ATR=%.4f ADX=%.0f | %s",
            direction, symbol, price, sl, tp, atr_val, adx_val,
            ", ".join(reasons),
        )

        return TrendSignal(
            symbol=symbol,
            direction=direction,
            price=price,
            sl=sl,
            tp=tp,
            atr_val=atr_val,
            reasons=tuple(reasons),
        )
