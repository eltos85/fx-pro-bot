"""Squeeze H4 — Bollinger-внутри-Keltner компрессия + breakout.

Стратегия разработана на основе 2-летнего backtest FxPro M5 данных
(см. STRATEGIES.md и BUILDLOG.md 2026-04-24): TTM Squeeze на commodities
(Gold, Brent) показал один из сильнейших edge'ей за всё исследование:

| Symbol | IS_n | IS_net | OOS_n | OOS_net | OOS_PF |
|--------|------|--------|-------|---------|--------|
| GC=F   |  20  | +1433  |  13   | +10799  |  4.11  |
| BZ=F   |  17  |  +222  |  13   |  +1606  |  2.01  |

FX пары на Squeeze в целом убыточны (cost dominates). Торгуем только
commodities.

## Механика

1. Ресемпл M5 → H4.
2. Индикаторы на H4:
   • BB(20, 2σ) — верхняя/нижняя полосы.
   • KC(20, 1.5×ATR14) — каналы Келтнера.
   • SMA(50) — трендовый фильтр.
3. **Squeeze ON** = BB полностью внутри KC
   (`bb_upper < kc_upper AND bb_lower > kc_lower`).
4. **Release** = squeeze был ON ≥ 3 H4 bars и сейчас OFF.
5. **Direction:**
   • LONG  — `close > SMA50` и `close > BB_upper_prev`
   • SHORT — `close < SMA50` и `close < BB_lower_prev`
6. **Entry** — на следующем H4 баре по open.
7. **SL** — `2 × ATR(14)` от entry.
8. **Exit** — `close < SMA50` (long) / `close > SMA50` (short) или
   time-stop 10 дней (60 H4 баров).

## Параметры (фиксированы из 2-year OOS результата)

| Param             | Value | Why                                              |
|-------------------|-------|--------------------------------------------------|
| BB_N              | 20    | стандарт Боллинджера                             |
| BB_K              | 2.0   | стандарт 2 σ                                     |
| KC_N              | 20    | стандарт Kelner 20                               |
| KC_MULT           | 1.5   | классика TTM                                     |
| SMA_N             | 50    | trend filter 50 H4 ≈ 8 дней                      |
| ATR_STOP_MULT     | 2.0   | classic 2×ATR stop                               |
| MIN_SQUEEZE_BARS  | 3     | фильтр шумных одиночных bar-squeeze              |
| MAX_HOLD_H4       | 60    | 10 дней = средний период тренда на H4            |

## Инструменты

Только `GC=F` (Gold) и `BZ=F` (Brent). Любые FX пары — disabled по costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _ema
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import resample_m5_to_h4

log = logging.getLogger(__name__)

SQUEEZE_H4_SYMBOLS: tuple[str, ...] = ("GC=F", "BZ=F")
SQUEEZE_H4_SOURCE = "squeeze_h4"

BB_N = 20
BB_K = 2.0
KC_N = 20
KC_MULT = 1.5
SMA_N = 50
ATR_N = 14
ATR_STOP_MULT = 2.0
MIN_SQUEEZE_BARS = 3
MAX_HOLD_H4 = 60   # 10 days × 6 H4 bars
MIN_BARS_REQUIRED = SMA_N + 5   # нужно ≥55 H4 bars


@dataclass(frozen=True, slots=True)
class SqueezeH4Signal:
    instrument: str
    direction: TrendDirection
    source: str
    entry_level: float
    sma50: float
    atr: float
    squeeze_count: int   # сколько H4 баров подряд был squeeze ON
    detail: str


def _sma(values: list[float], n: int) -> list[float]:
    """Простое скользящее среднее списка float."""
    result: list[float] = []
    cumsum = 0.0
    for i, v in enumerate(values):
        cumsum += v
        if i >= n:
            cumsum -= values[i - n]
        if i + 1 >= n:
            result.append(cumsum / n)
        else:
            result.append(float("nan"))
    return result


def _std(values: list[float]) -> float:
    """Population std без numpy."""
    if not values:
        return 0.0
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / len(values)
    return var ** 0.5 if var > 0 else 0.0


def _rolling_std(values: list[float], n: int) -> list[float]:
    """Rolling population std."""
    result: list[float] = []
    for i in range(len(values)):
        if i + 1 < n:
            result.append(float("nan"))
        else:
            result.append(_std(values[i - n + 1:i + 1]))
    return result


def _atr_series(bars: list[Bar], n: int = ATR_N) -> list[float]:
    """Полная серия ATR значений (Wilder)."""
    if len(bars) < n + 1:
        return [float("nan")] * len(bars)
    trs: list[float] = []
    prev_close = bars[0].close
    for b in bars:
        tr = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low - prev_close),
        )
        trs.append(tr)
        prev_close = b.close
    atr_vals: list[float] = [float("nan")] * (n - 1)
    atr0 = sum(trs[:n]) / n
    atr_vals.append(atr0)
    cur = atr0
    for i in range(n, len(trs)):
        cur = (cur * (n - 1) + trs[i]) / n
        atr_vals.append(cur)
    return atr_vals


class SqueezeH4Strategy:
    """TTM Squeeze на H4 для commodities."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 2,         # max 1 на инструмент × 2 инструмента
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
    ) -> list[SqueezeH4Signal]:
        signals: list[SqueezeH4Signal] = []
        for symbol in SQUEEZE_H4_SYMBOLS:
            m5_bars = bars_map.get(symbol)
            if not m5_bars:
                continue
            price = prices.get(symbol)
            if price is None or price <= 0:
                continue

            h4_bars = resample_m5_to_h4(m5_bars)
            if len(h4_bars) < MIN_BARS_REQUIRED:
                continue

            sig = self._check_squeeze(symbol, h4_bars, price)
            if sig:
                signals.append(sig)
        return signals

    def process_signals(
        self,
        signals: list[SqueezeH4Signal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="squeeze_h4")

        for sig in signals:
            if current >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="squeeze_h4", instrument=sig.instrument,
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
                    "  SQUEEZE-H4 SHADOW: %s %s @ %.5f [SMA50=%.5f, ATR=%.5f, "
                    "squeeze_count=%d, SL=%.5f] %s",
                    display_name(sig.instrument),
                    sig.direction.value.upper(),
                    price, sig.sma50, sig.atr, sig.squeeze_count, sl, sig.detail,
                )
                opened += 1
                current += 1
                continue

            pid = self._store.open_position(
                strategy="squeeze_h4",
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
                "  SQUEEZE-H4 OPEN: %s %s @ %.5f [SMA50=%.5f, ATR=%.5f, "
                "squeeze=%d H4 bars, SL=%.5f] %s",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.sma50, sig.atr, sig.squeeze_count, sl, sig.detail,
            )
            opened += 1
            current += 1

        return opened

    def _check_squeeze(
        self,
        symbol: str,
        h4_bars: list[Bar],
        price: float,
    ) -> SqueezeH4Signal | None:
        closes = [b.close for b in h4_bars]
        highs = [b.high for b in h4_bars]
        lows = [b.low for b in h4_bars]

        mid = _sma(closes, BB_N)
        std = _rolling_std(closes, BB_N)
        atr_vals = _atr_series(h4_bars, ATR_N)
        sma50 = _sma(closes, SMA_N)

        n = len(h4_bars)
        i_now = n - 1
        i_prev = n - 2

        # Проверить валидность индикаторов
        if (
            mid[i_prev] != mid[i_prev] or std[i_prev] != std[i_prev]
            or atr_vals[i_prev] != atr_vals[i_prev]
            or sma50[i_now] != sma50[i_now]
            or atr_vals[i_prev] <= 0
        ):
            return None

        # BB на предыдущем баре (чтобы не использовать текущий, ещё не закрытый
        # в реальности, хотя мы уже агрегировали).
        bb_u_prev = mid[i_prev] + BB_K * std[i_prev]
        bb_l_prev = mid[i_prev] - BB_K * std[i_prev]
        kc_u_prev = mid[i_prev] + KC_MULT * atr_vals[i_prev]
        kc_l_prev = mid[i_prev] - KC_MULT * atr_vals[i_prev]

        # Squeeze на текущем?
        cur_mid = mid[i_now]
        cur_std = std[i_now]
        cur_atr = atr_vals[i_now]
        if cur_mid != cur_mid or cur_std != cur_std or cur_atr != cur_atr or cur_atr <= 0:
            return None
        bb_u_now = cur_mid + BB_K * cur_std
        bb_l_now = cur_mid - BB_K * cur_std
        kc_u_now = cur_mid + KC_MULT * cur_atr
        kc_l_now = cur_mid - KC_MULT * cur_atr
        squeeze_now = (bb_u_now < kc_u_now) and (bb_l_now > kc_l_now)

        # Release = на предыдущем баре был squeeze, сейчас OFF
        squeeze_prev = (bb_u_prev < kc_u_prev) and (bb_l_prev > kc_l_prev)

        if squeeze_now or not squeeze_prev:
            return None

        # Посчитать сколько баров подряд был squeeze ON до i_prev включительно
        squeeze_count = 0
        for j in range(i_prev, max(0, i_prev - 30), -1):
            if mid[j] != mid[j] or std[j] != std[j] or atr_vals[j] != atr_vals[j]:
                break
            bu = mid[j] + BB_K * std[j]
            bl = mid[j] - BB_K * std[j]
            ku = mid[j] + KC_MULT * atr_vals[j]
            kl = mid[j] - KC_MULT * atr_vals[j]
            if bu < ku and bl > kl:
                squeeze_count += 1
            else:
                break

        if squeeze_count < MIN_SQUEEZE_BARS:
            return None

        last = h4_bars[-1]
        ma50 = sma50[i_now]

        # Long: close > SMA50 И close > BB_upper_prev
        if last.close > ma50 and last.close > bb_u_prev:
            return SqueezeH4Signal(
                instrument=symbol,
                direction=TrendDirection.LONG,
                source=SQUEEZE_H4_SOURCE,
                entry_level=last.close,
                sma50=ma50,
                atr=cur_atr,
                squeeze_count=squeeze_count,
                detail=(
                    f"BB_up_prev={bb_u_prev:.5f}, close={last.close:.5f} > "
                    f"both BB & SMA50({ma50:.5f})"
                ),
            )

        # Short: close < SMA50 И close < BB_lower_prev
        if last.close < ma50 and last.close < bb_l_prev:
            return SqueezeH4Signal(
                instrument=symbol,
                direction=TrendDirection.SHORT,
                source=SQUEEZE_H4_SOURCE,
                entry_level=last.close,
                sma50=ma50,
                atr=cur_atr,
                squeeze_count=squeeze_count,
                detail=(
                    f"BB_lo_prev={bb_l_prev:.5f}, close={last.close:.5f} < "
                    f"both BB & SMA50({ma50:.5f})"
                ),
            )

        return None
