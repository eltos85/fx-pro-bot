"""VWAP Mean-Reversion для крипто-скальпинга.

Цена стремится вернуться к VWAP (~70-75% времени).
Вход: отклонение > DEVIATION_THRESHOLD * ATR + RSI подтверждение + фильтр ADX.
TP: возврат к VWAP. SL: 2.0 ATR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr, ema, ema_bounce, rsi
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import ema_slope, vwap

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 2.0
RSI_CONFIRM_LOW = 30
RSI_CONFIRM_HIGH = 70
SL_ATR_MULT = 2.0
TP_ATR_MULT = 1.5
# ADX < 25 = допустимо для mean reversion (PyQuantLab 2025: 108 конфигураций).
# Крипта редко даёт ADX < 20, зона 20-25 приемлема. > 25 = сильный тренд.
ADX_MAX = 25.0


def _compute_adx(bars: list[Bar], period: int = 14) -> float:
    """ADX — сила тренда (0-100). ADX < 20 → боковик, ADX > 25 → сильный тренд."""
    n = len(bars)
    if n < period * 2 + 1:
        return 0.0

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_list: list[float] = []

    for i in range(1, n):
        high_diff = bars[i].high - bars[i - 1].high
        low_diff = bars[i - 1].low - bars[i].low
        plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0.0)
        minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0.0)
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        tr_list.append(tr)

    def _smooth(values: list[float], p: int) -> list[float]:
        result = [sum(values[:p])]
        for v in values[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    sm_tr = _smooth(tr_list, period)
    sm_plus = _smooth(plus_dm, period)
    sm_minus = _smooth(minus_dm, period)

    dx_values: list[float] = []
    for i in range(len(sm_tr)):
        if sm_tr[i] == 0:
            continue
        plus_di = 100 * sm_plus[i] / sm_tr[i]
        minus_di = 100 * sm_minus[i] / sm_tr[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 0.0

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


@dataclass(frozen=True, slots=True)
class VwapSignal:
    symbol: str
    direction: Direction
    deviation_atr: float
    rsi: float
    vwap_price: float
    atr_value: float
    entry_price: float


class VwapCryptoStrategy:
    """VWAP Mean-Reversion скальпинг для крипто.

    Rolling VWAP по последним 50 барам (без привязки к FX-сессиям).
    Фильтр: ADX ≤ 20 (боковик), RSI подтверждение, EMA slope.
    HTF фильтр: 1h EMA(50) slope определяет разрешённое направление.
    """

    def __init__(self, *, max_positions: int = 10, max_per_symbol: int = 2) -> None:
        self._max_positions = max_positions
        self._max_per_symbol = max_per_symbol
        self._htf_slopes: dict[str, float] = {}

    def set_htf_slopes(self, slopes: dict[str, float]) -> None:
        """Задать 1h EMA(50) slope для каждого символа (рассчитывается в main.py)."""
        self._htf_slopes = slopes

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[VwapSignal]:
        signals: list[VwapSignal] = []

        for symbol, bars in bars_map.items():
            if len(bars) < 51:
                continue

            price = bars[-1].close
            atr_val = atr(bars)
            if atr_val <= 0:
                continue

            adx = _compute_adx(bars)
            if adx > ADX_MAX:
                log.debug("%s: ADX=%.1f > %.1f, пропускаю (тренд)", symbol, adx, ADX_MAX)
                continue

            vwap_val = vwap(bars[-50:])
            deviation = (price - vwap_val) / atr_val

            closes = [b.close for b in bars]
            rsi_val = rsi(closes, 14)
            ema_vals = ema(closes, 50)
            slope = ema_slope(ema_vals, 5)

            log.debug(
                "%s: VWAP=%.4f price=%.4f dev=%.2f ATR, ADX=%.1f, RSI=%.1f, slope=%.6f",
                symbol, vwap_val, price, deviation, adx, rsi_val, slope,
            )

            htf_slope = self._htf_slopes.get(symbol)

            if deviation < -DEVIATION_THRESHOLD and rsi_val < RSI_CONFIRM_LOW:
                # Slope-фильтры отключены на демо: чистый mean reversion без
                # оглядки на локальный и старший тренд.
                # if slope < 0: continue
                # if htf_slope is not None and htf_slope < 0: continue
                signals.append(VwapSignal(
                    symbol=symbol,
                    direction=Direction.LONG,
                    deviation_atr=abs(deviation),
                    rsi=rsi_val,
                    vwap_price=vwap_val,
                    atr_value=atr_val,
                    entry_price=price,
                ))

            elif deviation > DEVIATION_THRESHOLD and rsi_val > RSI_CONFIRM_HIGH:
                # Slope-фильтры отключены на демо (см. комментарий выше).
                # if slope > 0: continue
                # if htf_slope is not None and htf_slope > 0: continue
                signals.append(VwapSignal(
                    symbol=symbol,
                    direction=Direction.SHORT,
                    deviation_atr=abs(deviation),
                    rsi=rsi_val,
                    vwap_price=vwap_val,
                    atr_value=atr_val,
                    entry_price=price,
                ))

        return signals
