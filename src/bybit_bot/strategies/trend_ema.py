"""EMA Trend-Following стратегия (V2).

Основано на исследованиях:
- 9/21 EMA: +0.069R expectancy на BTC 1h (quant-signals.com, 2716 бэктестов)
- ADX > 20 стандартный порог (Medium, fmz.com, cryptotrading-guide.com)
- Retest entry: вход на откате к fast EMA после crossover (fmz.com/strategy/491506)
- Volume по закрытому бару (freqtrade best practices)
- ATR-based SL: 64% WR vs 52% фиксированных (quant-signals.com)

Логика входа (positional state + pullback):
  1. EMA fast > EMA slow = "long zone" (или наоборот для short)
  2. ADX > 20 = тренд подтверждён
  3. Цена откатила к fast EMA (в пределах 0.3% от неё) = pullback entry
  4. Volume предыдущего закрытого бара > 0.5x avg

Выход:
  - SL = 1.5x ATR (исследования: 1.5-2x оптимально)
  - TP = 3x ATR (risk:reward 1:2)
  - Trailing: активация при +1.5 ATR, дистанция 1 ATR
  - Time-stop: 48 часов без +1%
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
    """EMA trend-following: positional state + pullback entry."""

    def __init__(
        self,
        *,
        fast_period: int = 9,
        slow_period: int = 21,
        trend_period: int = 200,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
        volume_ratio: float = 0.5,
        pullback_pct: float = 0.003,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 3.0,
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.volume_ratio = volume_ratio
        self.pullback_pct = pullback_pct
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult

    @property
    def min_bars(self) -> int:
        return max(self.slow_period, self.adx_period * 2) + 2

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        open_symbols: set[str] | None = None,
    ) -> list[TrendSignal]:
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
        if len(bars) < self.min_bars:
            log.debug("%s: недостаточно баров (%d < %d)", symbol, len(bars), self.min_bars)
            return None

        closes = [b.close for b in bars]
        price = closes[-1]

        ema_fast = ema(closes, self.fast_period)
        ema_slow = ema(closes, self.slow_period)

        if len(ema_fast) < 2 or len(ema_slow) < 2:
            return None

        fast_val = ema_fast[-1]
        slow_val = ema_slow[-1]

        # 1. Positional state: определяем зону
        if fast_val > slow_val:
            direction = "Buy"
        elif fast_val < slow_val:
            direction = "Sell"
        else:
            return None

        reasons: list[str] = []
        reasons.append(f"ema{self.fast_period}{'>' if direction == 'Buy' else '<'}ema{self.slow_period}")

        # 2. Pullback к fast EMA: цена в пределах pullback_pct от fast EMA
        distance_to_fast = abs(price - fast_val) / fast_val if fast_val > 0 else 1.0

        if direction == "Buy":
            near_fast = price <= fast_val * (1 + self.pullback_pct)
            if not near_fast:
                log.debug("%s: Buy — цена %.2f слишком далеко от EMA%d=%.2f (%.1f%%)",
                          symbol, price, self.fast_period, fast_val, distance_to_fast * 100)
                return None
        else:
            near_fast = price >= fast_val * (1 - self.pullback_pct)
            if not near_fast:
                log.debug("%s: Sell — цена %.2f слишком далеко от EMA%d=%.2f (%.1f%%)",
                          symbol, price, self.fast_period, fast_val, distance_to_fast * 100)
                return None

        reasons.append(f"pullback={distance_to_fast:.1%}")

        # 3. ADX > threshold
        adx_val = adx(bars, self.adx_period)
        if adx_val < self.adx_threshold:
            log.debug("%s: ADX=%.1f < %.1f", symbol, adx_val, self.adx_threshold)
            return None
        reasons.append(f"adx={adx_val:.0f}")

        # 4. Volume предыдущего закрытого бара
        avg_vol = volume_avg(bars[:-1], 20) if len(bars) > 21 else 0.0
        prev_vol = bars[-2].volume if len(bars) > 1 else 0.0
        if avg_vol > 0 and prev_vol < avg_vol * self.volume_ratio:
            log.debug("%s: prev_vol=%.0f < %.1fx avg=%.0f", symbol, prev_vol, self.volume_ratio, avg_vol)
            return None
        reasons.append("vol_ok")

        # 5. EMA 200 trend alignment (информационный, не блокирует)
        ema_trend = ema(closes, self.trend_period)
        if ema_trend:
            trend_val = ema_trend[-1]
            aligned = (direction == "Buy" and price > trend_val) or \
                      (direction == "Sell" and price < trend_val)
            reasons.append(f"ema{self.trend_period}_{'ok' if aligned else 'counter'}")

        # SL / TP
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
