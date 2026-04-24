"""Turtle H4 — classic 20-day breakout (Dennis 1983, simplified) для commodities.

Стратегия разработана на основе 2-летнего backtest FxPro M5 данных
(см. STRATEGIES.md и BUILDLOG.md 2026-04-24). Turtle breakout на H4 для
commodities показал устойчивый positive edge на OOS:

| Symbol | IS_n | IS_net | OOS_n | OOS_net | OOS_PF |
|--------|------|--------|-------|---------|--------|
| GC=F   |  16  | +2291  |  12   | +7320   |  1.87  |
| BZ=F   |  19  |  -942  |  13   | +1539   |  2.10  |

FX пары на Turtle убыточны. Торгуем только commodities.

## Механика (упрощённая Turtle, без pyramiding)

1. Ресемпл M5 → H4.
2. **Entry** — пробой 20-дневного донского канала:
   • LONG  — `high[t] > max(high[-120 ... -1])` (120 H4 bars = 20 дней)
   • SHORT — `low[t]  < min(low[-120 ... -1])`
3. **SL** — 2×ATR(14) от entry.
4. **Exit** (trailing):
   • LONG — `low[t] < min(low[-60 ... -1])` (10-дневный противоположный пробой)
   • SHORT — `high[t] > max(high[-60 ... -1])`
5. **Time-stop** — 30 дней (180 H4 bars).

Только ОДИН вход на символ за раз (без pyramiding).

## Параметры (фиксированы из backtest)

| Param              | Value | Why                                                |
|--------------------|-------|----------------------------------------------------|
| ENTRY_LOOKBACK_H4  |  120  | 20 дней × 6 H4 — классика Turtle                   |
| EXIT_LOOKBACK_H4   |   60  | 10 дней — половина entry                           |
| ATR_STOP_MULT      |  2.0  | classic 2N Turtle                                  |
| MAX_HOLD_H4        |  180  | 30 дней — крайний time-stop                        |

## Инструменты

`GC=F` (Gold), `BZ=F` (Brent). FX-пары — убыточны на Turtle, не торгуем.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import resample_m5_to_h4
from fx_pro_bot.strategies.scalping.squeeze_h4 import _atr_series

log = logging.getLogger(__name__)

TURTLE_H4_SYMBOLS: tuple[str, ...] = ("GC=F", "BZ=F")
TURTLE_H4_SOURCE = "turtle_h4"

ENTRY_LOOKBACK_H4 = 120   # 20 дней × 6 H4
EXIT_LOOKBACK_H4 = 60     # 10 дней — trail
ATR_STOP_MULT = 2.0
MAX_HOLD_H4 = 180
ATR_N = 14

MIN_BARS_REQUIRED = ENTRY_LOOKBACK_H4 + ATR_N + 5


@dataclass(frozen=True, slots=True)
class TurtleH4Signal:
    instrument: str
    direction: TrendDirection
    source: str
    entry_level: float       # breakout уровень
    atr: float
    lookback_high: float
    lookback_low: float
    detail: str


class TurtleH4Strategy:
    """20-day Turtle breakout на H4 для commodities."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 2,
        max_per_instrument: int = 1,
        shadow: bool = False,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._shadow = shadow

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        prices: dict[str, float],
    ) -> list[TurtleH4Signal]:
        signals: list[TurtleH4Signal] = []
        for symbol in TURTLE_H4_SYMBOLS:
            m5_bars = bars_map.get(symbol)
            if not m5_bars:
                continue
            price = prices.get(symbol)
            if price is None or price <= 0:
                continue

            h4_bars = resample_m5_to_h4(m5_bars)
            if len(h4_bars) < MIN_BARS_REQUIRED:
                continue

            sig = self._check_breakout(symbol, h4_bars)
            if sig:
                signals.append(sig)
        return signals

    def process_signals(
        self,
        signals: list[TurtleH4Signal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="turtle_h4")

        for sig in signals:
            if current >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="turtle_h4", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            sl_dist = ATR_STOP_MULT * sig.atr
            if sig.direction == TrendDirection.LONG:
                sl = price - sl_dist
            else:
                sl = price + sl_dist

            if self._shadow:
                log.info(
                    "  TURTLE-H4 SHADOW: %s %s @ %.5f [breakout=%.5f, "
                    "ATR=%.5f, SL=%.5f] %s",
                    display_name(sig.instrument),
                    sig.direction.value.upper(),
                    price, sig.entry_level, sig.atr, sl, sig.detail,
                )
                opened += 1
                current += 1
                continue

            pid = self._store.open_position(
                strategy="turtle_h4",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )
            ps = pip_size(sig.instrument)
            cost = estimate_entry_cost(sig.instrument, sig.source, sig.atr, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            log.info(
                "  TURTLE-H4 OPEN: %s %s @ %.5f [20d-high=%.5f, 20d-low=%.5f, "
                "ATR=%.5f, SL=%.5f] %s",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.lookback_high, sig.lookback_low, sig.atr, sl, sig.detail,
            )
            opened += 1
            current += 1

        return opened

    def _check_breakout(
        self,
        symbol: str,
        h4_bars: list[Bar],
    ) -> TurtleH4Signal | None:
        atr_vals = _atr_series(h4_bars, ATR_N)

        n = len(h4_bars)
        last = h4_bars[-1]

        atr_prev = atr_vals[n - 2]
        if atr_prev != atr_prev or atr_prev <= 0:
            return None

        window = h4_bars[n - 1 - ENTRY_LOOKBACK_H4:n - 1]
        if len(window) < ENTRY_LOOKBACK_H4:
            return None

        lookback_high = max(b.high for b in window)
        lookback_low = min(b.low for b in window)

        if last.high > lookback_high:
            return TurtleH4Signal(
                instrument=symbol,
                direction=TrendDirection.LONG,
                source=TURTLE_H4_SOURCE,
                entry_level=lookback_high,
                atr=atr_prev,
                lookback_high=lookback_high,
                lookback_low=lookback_low,
                detail=f"breakout ABOVE 20d-high (high={last.high:.5f} > {lookback_high:.5f})",
            )

        if last.low < lookback_low:
            return TurtleH4Signal(
                instrument=symbol,
                direction=TrendDirection.SHORT,
                source=TURTLE_H4_SOURCE,
                entry_level=lookback_low,
                atr=atr_prev,
                lookback_high=lookback_high,
                lookback_low=lookback_low,
                detail=f"breakout BELOW 20d-low (low={last.low:.5f} < {lookback_low:.5f})",
            )

        return None
