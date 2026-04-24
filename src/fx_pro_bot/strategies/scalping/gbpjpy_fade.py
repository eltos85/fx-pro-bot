"""GBPJPY fade — trigger по GBPUSD, fade-entry на GBPJPY.

Стратегия разработана на основе 2-летнего backtest FxPro M5 + walk-forward
валидации (см. STRATEGIES.md и BUILDLOG.md 2026-04-24). Единственная FX
стратегия, показавшая положительный edge на OOS:

| Window   | Params               |  n | Net pips |
|----------|----------------------|----|----------|
| WFO_W2   | trig=2σ, ent=1h, h=36 | 17 |  +530    |
| WFO_W3   | trig=2σ, ent=1h, h=36 | 18 |  +341    |
| WFO_W4   | trig=2σ, ent=1h, h=36 | 20 |  +461    |
| **ИТОГО**| **OOS sum**          |**55**| **+1332** |
| PF OOS   |                      |    |  1.06    |
| p-value  | permutation test      |    |  0.13 (weak) |

Edge слабый, но стабильный через walk-forward окна → торгуем на минимальном
лоте (shadow/0.01) как diversifier к commodity-стратегиям.

## Механика

1. На каждом цикле (5 мин) проверяем M5 бары GBPUSD:
   • считаем log-return за последние 4h (= 48 M5 bars), со сдвигом 1h назад
   • т.е. return от `close[t-60bars]` до `close[t-12bars]`
   • std: 30-дневное rolling std этих 4h-returns
   • z = return_4h / std
2. **Trigger:** |z| ≥ 2.0
3. **Entry (fade):** direction = opposite от знака return
   • Если GBPUSD упал сильно (z<-2) → LONG GBPJPY (fade: GBP ещё не обвалилась)
   • Если GBPUSD вырос сильно (z>+2) → SHORT GBPJPY
4. **SL:** 2× std в pip-единицах (защита от runaway-move).
5. **Time-stop:** 36h (обрабатывается в monitor.py).
6. **Cool-off:** 4h между триггерами (проверяется по последнему open_position).

## Инструменты

Триггер: `GBPUSD=X`. Entry: `GBPJPY=X`.
Никакие другие пары не торгуются этой стратегией.

## Почему fade, а не momentum

По backtest 2-летних данных (scripts/backtest_gbpusd_jpy_fade.py) fade
направление дало +1332 pips OOS, momentum — убыток. Экономический смысл:
экстремальный move GBPUSD (без corresponding move на JPY-кроссах)
= overreaction на USD-ноги → mean-reverts в следующие 36h.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore

log = logging.getLogger(__name__)

GBPJPY_FADE_TRIGGER = "GBPUSD=X"
GBPJPY_FADE_SYMBOL = "GBPJPY=X"
GBPJPY_FADE_SOURCE = "gbpjpy_fade"

RETURN_WINDOW_M5 = 48          # 4h
ENTRY_DELAY_M5 = 12            # 1h назад (см. WFO ENTRY_DELAY_H=1)
STD_WINDOW_DAYS = 30           # 30d rolling std
M5_PER_DAY = 288
STD_WINDOW_M5 = STD_WINDOW_DAYS * M5_PER_DAY
TRIGGER_SIGMA = 2.0
SL_SIGMA = 2.0
COOLOFF_HOURS = 4.0

MIN_BARS_REQUIRED = STD_WINDOW_M5 + RETURN_WINDOW_M5 + ENTRY_DELAY_M5 + 1


@dataclass(frozen=True, slots=True)
class GbpjpyFadeSignal:
    instrument: str
    direction: TrendDirection
    source: str
    entry_level: float
    z_score: float
    reaction_pips: float   # величина движения GBPUSD в пунктах
    sigma_px: float         # std в единицах цены
    detail: str


class GbpjpyFadeStrategy:
    """M5 trigger GBPUSD → fade entry GBPJPY."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 1,
        shadow: bool = False,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._shadow = shadow

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        prices: dict[str, float],
    ) -> list[GbpjpyFadeSignal]:
        signals: list[GbpjpyFadeSignal] = []

        trig_bars = bars_map.get(GBPJPY_FADE_TRIGGER)
        if not trig_bars or len(trig_bars) < MIN_BARS_REQUIRED:
            return signals

        jpy_price = prices.get(GBPJPY_FADE_SYMBOL)
        if jpy_price is None or jpy_price <= 0:
            return signals

        n = len(trig_bars)
        # t = n-1 — текущий бар
        # Entry — сейчас, trigger — 1 час назад (ENTRY_DELAY_M5)
        t_trig_end = n - 1 - ENTRY_DELAY_M5
        t_trig_start = t_trig_end - RETURN_WINDOW_M5
        if t_trig_start < 0 or t_trig_end <= t_trig_start:
            return signals

        c_start = trig_bars[t_trig_start].close
        c_end = trig_bars[t_trig_end].close
        if c_start <= 0 or c_end <= 0:
            return signals
        ret_trig = math.log(c_end / c_start)

        # 30d rolling std of overlapping 4h returns, взятое ДО trigger point
        std_start_idx = t_trig_end - STD_WINDOW_M5
        std_end_idx = t_trig_end
        if std_start_idx < RETURN_WINDOW_M5:
            return signals

        rets: list[float] = []
        for i in range(std_start_idx, std_end_idx, RETURN_WINDOW_M5):
            j = i + RETURN_WINDOW_M5
            if j >= len(trig_bars):
                break
            c_i = trig_bars[i].close
            c_j = trig_bars[j].close
            if c_i > 0 and c_j > 0:
                rets.append(math.log(c_j / c_i))

        if len(rets) < 30:
            return signals

        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        std = math.sqrt(var) if var > 0 else 0.0
        if std <= 0:
            return signals

        z = (ret_trig - m) / std
        if abs(z) < TRIGGER_SIGMA:
            return signals

        # Cool-off: не открываем ещё одну если < 4h назад уже открывали
        if self._is_in_cooloff():
            return signals

        # fade: direction = opposite reaction sign
        if z > 0:
            direction = TrendDirection.SHORT
        else:
            direction = TrendDirection.LONG

        # SL: 2σ в единицах цены GBPJPY (сигма gbpjpy отдельная! но у нас
        # только std GBPUSD. Используем реакцию в относительной величине:
        # sigma_rel_px_jpy = std × jpy_price)
        sigma_px = abs(std) * jpy_price

        reaction_pips = abs(ret_trig) * jpy_price / pip_size(GBPJPY_FADE_SYMBOL)

        signals.append(GbpjpyFadeSignal(
            instrument=GBPJPY_FADE_SYMBOL,
            direction=direction,
            source=GBPJPY_FADE_SOURCE,
            entry_level=jpy_price,
            z_score=z,
            reaction_pips=reaction_pips,
            sigma_px=sigma_px,
            detail=(
                f"GBPUSD 4h ret={ret_trig*10000:.1f} pip "
                f"(z={z:+.2f}, σ={std*10000:.2f}bp) → fade"
            ),
        ))
        return signals

    def process_signals(
        self,
        signals: list[GbpjpyFadeSignal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="gbpjpy_fade")

        for sig in signals:
            if current >= self._max_positions:
                break

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            sl_dist = SL_SIGMA * sig.sigma_px
            if sl_dist <= 0:
                continue

            if sig.direction == TrendDirection.LONG:
                sl = price - sl_dist
            else:
                sl = price + sl_dist

            if self._shadow:
                log.info(
                    "  GBPJPY-FADE SHADOW: %s %s @ %.5f "
                    "[z=%.2f, react=%.1f pip, SL=%.5f] %s",
                    display_name(sig.instrument),
                    sig.direction.value.upper(),
                    price, sig.z_score, sig.reaction_pips, sl, sig.detail,
                )
                opened += 1
                current += 1
                continue

            pid = self._store.open_position(
                strategy="gbpjpy_fade",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )
            ps = pip_size(sig.instrument)
            cost = estimate_entry_cost(sig.instrument, sig.source, sig.sigma_px, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            log.info(
                "  GBPJPY-FADE OPEN: %s %s @ %.5f "
                "[z=%.2f, react=%.1f pip, SL=%.5f] %s",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.z_score, sig.reaction_pips, sl, sig.detail,
            )
            opened += 1
            current += 1

        return opened

    def _is_in_cooloff(self) -> bool:
        """True если за последние COOLOFF_HOURS уже была открыта gbpjpy_fade."""
        cutoff = datetime.now(tz=UTC) - timedelta(hours=COOLOFF_HOURS)
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM positions
                WHERE strategy = 'gbpjpy_fade'
                ORDER BY created_at DESC LIMIT 1
                """,
            ).fetchone()
        if not row:
            return False
        try:
            created = datetime.fromisoformat(row["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return False
        return created > cutoff
