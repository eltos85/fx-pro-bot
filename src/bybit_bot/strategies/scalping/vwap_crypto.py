"""VWAP Mean-Reversion для крипто-скальпинга.

Цена стремится вернуться к VWAP (~70-75% времени).
Вход: отклонение > DEVIATION_THRESHOLD * ATR + RSI подтверждение + фильтр ADX.
TP: возврат к VWAP. SL: 2.0 ATR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr, ema, rsi
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx, ema_slope, vwap

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 2.0
RSI_CONFIRM_LOW = 30
RSI_CONFIRM_HIGH = 70
# ADX < 25 = допустимо для mean reversion (PyQuantLab 2025: 108 конфигураций).
# Крипта редко даёт ADX < 20, зона 20-25 приемлема. > 25 = сильный тренд.
ADX_MAX = 25.0

# Мягкий HTF slope-фильтр: блокируем вход только против СИЛЬНОГО старшего тренда.
# |slope| < HTF_SLOPE_FLAT → считаем боковиком, mean-reversion работает в обе стороны.
# |slope| >= HTF_SLOPE_FLAT → запрещаем контр-трендовое направление.
# 0.0005 ≈ 0.05% за 1h бар → мягче, чем полный запрет. Локальный 5m slope
# отключён — он слишком шумный и режет почти все MR-сигналы на трендовых
# участках, хотя именно там MR часто отрабатывает.
HTF_SLOPE_FLAT = 0.0005


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

    def __init__(self) -> None:
        # Лимит параллельных позиций проверяется в app/main.py через
        # settings.scalping_max_positions, стратегия его не хранит.
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

            adx = compute_adx(bars)
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
                # Мягкий HTF-фильтр: не открываем LONG, если 1h тренд сильно вниз.
                # Локальный 5m slope отключён — он слишком шумный.
                if htf_slope is not None and htf_slope < -HTF_SLOPE_FLAT:
                    log.debug("%s LONG: HTF slope=%.6f < -%.6f (сильный down), пропуск",
                              symbol, htf_slope, HTF_SLOPE_FLAT)
                    continue
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
                # Не открываем SHORT, если 1h тренд сильно вверх.
                if htf_slope is not None and htf_slope > HTF_SLOPE_FLAT:
                    log.debug("%s SHORT: HTF slope=%.6f > +%.6f (сильный up), пропуск",
                              symbol, htf_slope, HTF_SLOPE_FLAT)
                    continue
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
