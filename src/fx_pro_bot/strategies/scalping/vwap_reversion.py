"""VWAP Mean-Reversion Micro-Scalper.

Цена стремится вернуться к VWAP (~70-75% времени в активные часы),
т.к. институциональные алгоритмы используют VWAP как бенчмарк исполнения.

Вход: отклонение от VWAP > DEVIATION_THRESHOLD * ATR + RSI подтверждение.
Выход: возврат к VWAP или SL за 1.5 ATR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _ema, _rsi
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import ema_slope, vwap

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 1.0
RSI_CONFIRM_LOW = 35
RSI_CONFIRM_HIGH = 65
SL_ATR_MULT = 1.5
TP_ATR_MULT = 1.0


@dataclass(frozen=True, slots=True)
class VwapSignal:
    instrument: str
    direction: TrendDirection
    deviation_atr: float
    rsi: float
    vwap_price: float
    atr: float


class VwapReversionStrategy:
    """VWAP Mean-Reversion: торговля возврата цены к VWAP."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 30,
        max_per_instrument: int = 3,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        prices: dict[str, float],
    ) -> list[VwapSignal]:
        signals: list[VwapSignal] = []

        for symbol, bars in bars_map.items():
            if len(bars) < 51:
                continue

            price = prices.get(symbol)
            if price is None or price <= 0:
                continue

            atr = _atr(bars)
            if atr <= 0:
                continue

            vwap_val = vwap(bars[-50:])
            deviation = (price - vwap_val) / atr

            closes = [b.close for b in bars]
            rsi = _rsi(closes, 14)
            ema_vals = _ema(closes, 50)
            slope = ema_slope(ema_vals, 5)

            if deviation < -DEVIATION_THRESHOLD and rsi < RSI_CONFIRM_LOW:
                if slope < 0:
                    continue
                signals.append(VwapSignal(
                    instrument=symbol,
                    direction=TrendDirection.LONG,
                    deviation_atr=abs(deviation),
                    rsi=rsi,
                    vwap_price=vwap_val,
                    atr=atr,
                ))

            elif deviation > DEVIATION_THRESHOLD and rsi > RSI_CONFIRM_HIGH:
                if slope > 0:
                    continue
                signals.append(VwapSignal(
                    instrument=symbol,
                    direction=TrendDirection.SHORT,
                    deviation_atr=abs(deviation),
                    rsi=rsi,
                    vwap_price=vwap_val,
                    atr=atr,
                ))

        return signals

    def process_signals(
        self,
        signals: list[VwapSignal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="vwap_reversion")

        for sig in signals:
            if current >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="vwap_reversion", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            if sig.direction == TrendDirection.LONG:
                sl = price - SL_ATR_MULT * sig.atr
            else:
                sl = price + SL_ATR_MULT * sig.atr

            self._store.open_position(
                strategy="vwap_reversion",
                source="vwap_deviation",
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )

            ps = pip_size(sig.instrument)
            log.info(
                "  VWAP OPEN: %s %s @ %.5f (VWAP=%.5f, dev=%.1f ATR, RSI=%.0f, SL=%.5f)",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.vwap_price, sig.deviation_atr, sig.rsi, sl,
            )
            opened += 1
            current += 1

        return opened
