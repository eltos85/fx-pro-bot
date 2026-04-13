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
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.volume_ratio = volume_ratio
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult

    @property
    def min_bars(self) -> int:
        return self.trend_period + 2

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

        cur_fast = ema_fast[-1]
        cur_slow = ema_slow[-1]
        prev_fast = ema_fast[-2]
        prev_slow = ema_slow[-2]

        cross_up = prev_fast <= prev_slow and cur_fast > cur_slow
        cross_down = prev_fast >= prev_slow and cur_fast < cur_slow

        if not cross_up and not cross_down:
            return None

        reasons: list[str] = []

        if cross_up:
            direction = "Buy"
            reasons.append("ema_cross_up")
        else:
            direction = "Sell"
            reasons.append("ema_cross_down")

        ema_trend = ema(closes, self.trend_period)
        if not ema_trend:
            return None
        trend_val = ema_trend[-1]
        price = closes[-1]

        if direction == "Buy" and price < trend_val:
            log.debug("%s: Buy отфильтрован — цена ниже EMA%d", symbol, self.trend_period)
            return None
        if direction == "Sell" and price > trend_val:
            log.debug("%s: Sell отфильтрован — цена выше EMA%d", symbol, self.trend_period)
            return None
        reasons.append(f"ema{self.trend_period}_ok")

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
