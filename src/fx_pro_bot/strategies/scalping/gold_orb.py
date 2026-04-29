"""Gold ORB Isolated — пробой Opening Range на XAU/USD (London + NY opens).

Стратегия разработана на основе 90-дневного backtest
(см. STRATEGIES.md §3b-bis и BUILDLOG.md 2026-04-24): изолированный ORB
только для золота показал +6145 net pips за 90 дней
(WR 42%, PF 1.67, Sharpe 3.16).

## Отличия от `session_orb`

`session_orb` (торгует все FX+commodities) строг к фильтрам: confirm bar,
ADX<25, volume ≥ 1.3×avg, EMA-slope. На 13 инструментах он скорее `break-even`,
но на Gold специально confirm-bar и ADX-filter РЕЖУТ edge:
- Gold движется на news/fundamentals, а не на volume/ADX
- confirm bar (M5 close за коробкой) теряет slingshot-движение
  после пробоя (часто цена сразу откатывает на уровень пробоя)

Gold ORB:
- **touch-break**: вход на касании box_high/box_low (без wait for close)
- **без ADX-filter**: Gold торгуется и в trend, и в range
- **без volume-filter**: M5 volume на Gold non-reliable (OTC OTC)
- **EMA-slope filter сохранён**: защита от contra-trend входов

## Параметры (из backtest 90d M5)

- SL = 1.5 × ATR, TP = 3.0 × ATR (R:R = 2)
- Box = 3 × M5 bars (15 мин) — London 08:00-08:15, NY 14:30-14:45 UTC
- Trade window: London 08:15-12:00, NY 14:45-17:00
- Touch-break: `bar.high > box_high` (long) или `bar.low < box_low` (short)
- Re-entry policy: пока активна позиция — block, после закрытия —
  следующий валидный touch-break открывает новую (multi-entry внутри
  сессии). См. OOS-анализ ниже.

## Re-entry (multi-entry) vs canonical (1 trade per session)

Канон Carter (2012, ch.7) предполагает «1 trade per session per day»
(вход только по первому валидному пробою, остальные сигналы в
сессии — это re-test и должны fade'иться, а не пробивать).

**Текущий код реализует multi-entry**: после закрытия предыдущей
позиции (по SL/TP/trail/time) следующий же touch-break открывает
новую. На сессии 29.04 это привело к 5 SHORT входам в один London
ORB box за 3 часа (см. BUILDLOG.md 2026-04-29).

OOS-анализ `[scripts/analyze_gold_orb_session_guard.py](../../scripts/analyze_gold_orb_session_guard.py)`
(29.04.2026, артефакт `data/gold_orb_session_guard_out.txt`) сравнил
multi-entry (текущий код) vs canonical-guard на 90d in-sample
(28.01–28.04) + fresh 30d OOS (28.12–28.01):

| режим                 | trades | WR    | Net pips | PF    | Sharpe |
|-----------------------|-------:|------:|---------:|------:|-------:|
| **multi-entry** 90d   |    485 | 75.1% |  +87,109 |  6.76 |  16.17 |
| canonical-guard 90d   |    114 | 41.2% |   +3,651 |  1.40 |   1.48 |
| **multi-entry** OOS30 |    122 | 53.3% |   +7,252 |  2.50 |   4.39 |
| canonical-guard OOS30 |     33 | 18.2% |   −1,264 |  0.51 |  −1.65 |

Multi-entry значимо лучше во всех метриках на обоих датасетах.
Walk-forward T1/T2/T3: multi-entry прибылен во всех 3-х третях
(+24K / +40K / +23K, PF 5.2 / 7.8 / 7.6); canonical-guard убыточен
в T1 (−604, PF 0.84). По плану `oos_gold_orb_session_guard` —
**FAIL** для canonical-guard.

**Caveat**: backtest BASE×CANON показывает Sharpe 16+ / PF 6.7+,
но live-performance существенно скромнее (slippage 8–19 pip vs
4.2 pip simulated, 5-min poll lag, broker amend REJECTED).
Относительная разница multi-entry vs canonical достоверна (биасы
одинаковые), но абсолютные числа калибруем по live.

## Защита от concurrent positions

Реализована через `max_positions=2` (1 на London × 1 на NY) и
`max_per_instrument=1` в `process_signals`. После закрытия
позиции счётчик уменьшается, что и разрешает re-entry.

Robustness grid:
| Config                | Net pips (90d) |
|-----------------------|----------------|
| SL1.5 × TP3.0 (base)  | +6146          |
| SL1.5 × TP2.0         | +6339          |
| SL2.0 × TP3.0         | +6318          |
| ADX<40 (hybrid)       | +7967          |

Walk-forward (90d / 3):
| Period | n  | WR%  | Net   | PF   |
|--------|----|------|-------|------|
| T1     | 32 | 40.6 | +2397 | 1.81 |
| T2     | 42 | 38.1 | +1298 | 1.35 |
| T3     | 41 | 48.8 | +2575 | 2.06 |

Все трети прибыльны, T3 (последние 30 дней) — лучшая → нет edge-decay.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _ema
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import ema_slope, session_range

log = logging.getLogger(__name__)

GOLD_ORB_INSTRUMENT = "GC=F"
GOLD_ORB_SOURCE = "gold_orb_breakout"

ORB_BARS = 3
SL_ATR_MULT = 1.5
GOLD_ORB_TP_ATR_MULT = 3.0

LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)
LONDON_CLOSE = time(12, 0)
NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)
NY_CLOSE = time(17, 0)

# Shadow-фильтры (только логирование, не влияют на торговлю).
# Параметры из 90d backtest + walk-forward 29.04.2026
# (`scripts/analyze_gold_orb_filters.py`, `data/gold_orb_filters_out.txt`,
# BUILDLOG.md 2026-04-29). НЕ являются canonical research-параметрами,
# а кандидатами на будущее обсуждение. Поэтому в shadow-режиме.
SHADOW_F1_MIN_BREAK_ATR = 0.3   # F1: пробой границы ORB-box в ATR
                                 # ниже этого = шумовой тык, кандидат на блок


@dataclass(frozen=True, slots=True)
class GoldOrbSignal:
    instrument: str
    direction: TrendDirection
    source: str
    entry_level: float   # точка пробоя = box_high (long) или box_low (short)
    box_high: float
    box_low: float
    atr: float
    session: str         # "london" | "ny"
    detail: str
    # Диагностика «качества» входа (28.04.2026, наблюдения позиции #150097702):
    # bars_since_box_end — сколько M5-баров прошло с конца ORB-коробки до touch-break
    # break_distance_atr — на сколько ATR текущая цена отстоит от пробитой границы
    # Эти поля только логируются, не влияют на торговую логику.
    bars_since_box_end: int = 0
    break_distance_atr: float = 0.0


class GoldOrbStrategy:
    """Gold ORB: изолированный ORB только для XAU/USD."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 2,       # max 1 на сессию × 2 сессии в день
        max_per_instrument: int = 1,  # только один trade на XAU за раз
        shadow: bool = False,         # если True — сигналы только логируются, без открытия
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._shadow = shadow

    def _evaluate_shadow_filters(self, sig: GoldOrbSignal) -> tuple[str, str]:
        """Возвращает (f1_status, f2_status) для shadow-логирования.

        НЕ влияет на торговлю — только наблюдение для будущего анализа.

        F1: 'ok' если break_distance_atr >= SHADOW_F1_MIN_BREAK_ATR,
            иначе 'BLOCK' (шумовой тык за границу ORB-box).
        F2: 'ok' если в этой сессии (London/NY) сегодня ещё НЕ было
            убыточной позиции в этом же направлении, иначе 'BLOCK'
            (sl_cooldown — не лезть туда же после стопа).
        """
        f1 = "ok" if sig.break_distance_atr >= SHADOW_F1_MIN_BREAK_ATR else "BLOCK"

        now = datetime.now(timezone.utc)
        today = now.date()
        if sig.session == "london":
            start = datetime.combine(today, LONDON_OPEN, tzinfo=timezone.utc)
            end = datetime.combine(today, LONDON_CLOSE, tzinfo=timezone.utc)
        elif sig.session == "ny":
            start = datetime.combine(today, NY_OPEN, tzinfo=timezone.utc)
            end = datetime.combine(today, NY_CLOSE, tzinfo=timezone.utc)
        else:
            return f1, "ok"

        try:
            had_loss = self._store.has_loss_position_in_window(
                strategy="gold_orb",
                direction=sig.direction.value,
                window_start_iso=start.isoformat(),
                window_end_iso=end.isoformat(),
            )
        except Exception:
            had_loss = False
        f2 = "BLOCK" if had_loss else "ok"
        return f1, f2

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        prices: dict[str, float],
    ) -> list[GoldOrbSignal]:
        signals: list[GoldOrbSignal] = []
        symbol = GOLD_ORB_INSTRUMENT
        bars = bars_map.get(symbol)
        if not bars or len(bars) < 51:
            return signals
        price = prices.get(symbol)
        if price is None or price <= 0:
            return signals

        atr = _atr(bars)
        if atr <= 0:
            return signals

        closes = [b.close for b in bars]
        ema_vals = _ema(closes, 50)
        slope = ema_slope(ema_vals, 5)

        sig = self._check_orb(symbol, bars, price, atr, slope)
        if sig:
            signals.append(sig)
        return signals

    def process_signals(
        self,
        signals: list[GoldOrbSignal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="gold_orb")

        for sig in signals:
            if current >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="gold_orb", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            sl_dist = SL_ATR_MULT * sig.atr
            if sig.direction == TrendDirection.LONG:
                sl = price - sl_dist
            else:
                sl = price + sl_dist

            f1, f2 = self._evaluate_shadow_filters(sig)

            if self._shadow:
                tp_dist = GOLD_ORB_TP_ATR_MULT * sig.atr
                tp = price + tp_dist if sig.direction == TrendDirection.LONG else price - tp_dist
                log.info(
                    "  GOLD-ORB SHADOW: %s %s @ %.5f [%s, box=[%.5f..%.5f], SL=%.5f, TP=%.5f, "
                    "bars_since_box_end=%d, break_dist=%.2fATR] [SHADOW F1=%s F2=%s]",
                    display_name(sig.instrument),
                    sig.direction.value.upper(),
                    price, sig.session, sig.box_high, sig.box_low, sl, tp,
                    sig.bars_since_box_end, sig.break_distance_atr,
                    f1, f2,
                )
                opened += 1
                current += 1
                continue

            pid = self._store.open_position(
                strategy="gold_orb",
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
                "  GOLD-ORB OPEN: %s %s @ %.5f [%s session, box=[%.5f..%.5f], SL=%.5f, "
                "bars_since_box_end=%d, break_dist=%.2fATR] [SHADOW F1=%s F2=%s]",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.session, sig.box_high, sig.box_low, sl,
                sig.bars_since_box_end, sig.break_distance_atr,
                f1, f2,
            )
            opened += 1
            current += 1

        return opened

    def _check_orb(
        self,
        symbol: str,
        bars: list[Bar],
        price: float,
        atr: float,
        slope: float,
    ) -> GoldOrbSignal | None:
        session_bars, session_tag = self._get_session_bars(bars)
        if not session_bars or len(session_bars) < ORB_BARS + 1:
            return None

        box_high, box_low = session_range(session_bars, ORB_BARS)
        if box_high == 0 or box_low == 0:
            return None

        last = bars[-1]
        # Диагностика: сколько M5-баров с конца ORB-коробки. Для оценки
        # «свежести» пробоя — late-entry (>>1 бар после box_end) часто
        # exhausted move с risk быстрого reversal'а (наблюдение 28.04
        # позиции #150097702 в BUILDLOG).
        last_t = last.ts.time() if last.ts.tzinfo else last.ts.replace(tzinfo=timezone.utc).time()
        if session_tag == "london":
            box_end_minutes = LONDON_ORB_END.hour * 60 + LONDON_ORB_END.minute
        else:
            box_end_minutes = NY_ORB_END.hour * 60 + NY_ORB_END.minute
        cur_minutes = last_t.hour * 60 + last_t.minute
        bars_since_box_end = max(0, (cur_minutes - box_end_minutes) // 5)

        # touch-break: high/low текущего бара пересёк границу
        if last.high > box_high:
            if slope < 0:   # contra-trend защита: LONG только при slope>=0
                return None
            break_dist_atr = (last.high - box_high) / atr if atr > 0 else 0.0
            return GoldOrbSignal(
                instrument=symbol,
                direction=TrendDirection.LONG,
                source=GOLD_ORB_SOURCE,
                entry_level=box_high,
                box_high=box_high,
                box_low=box_low,
                atr=atr,
                session=session_tag,
                detail=f"touch-break above {box_high:.5f} (high={last.high:.5f})",
                bars_since_box_end=bars_since_box_end,
                break_distance_atr=round(break_dist_atr, 2),
            )

        if last.low < box_low:
            if slope > 0:
                return None
            break_dist_atr = (box_low - last.low) / atr if atr > 0 else 0.0
            return GoldOrbSignal(
                instrument=symbol,
                direction=TrendDirection.SHORT,
                source=GOLD_ORB_SOURCE,
                entry_level=box_low,
                box_high=box_high,
                box_low=box_low,
                atr=atr,
                session=session_tag,
                detail=f"touch-break below {box_low:.5f} (low={last.low:.5f})",
                bars_since_box_end=bars_since_box_end,
                break_distance_atr=round(break_dist_atr, 2),
            )

        return None

    @staticmethod
    def _get_session_bars(bars: list[Bar]) -> tuple[list[Bar], str]:
        if not bars:
            return [], ""
        last_ts = bars[-1].ts
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        cur_time = last_ts.time()

        if LONDON_ORB_END <= cur_time < LONDON_CLOSE:
            session_start = LONDON_OPEN
            tag = "london"
        elif NY_ORB_END <= cur_time < NY_CLOSE:
            session_start = NY_OPEN
            tag = "ny"
        else:
            return [], ""

        session_bars = [
            b for b in bars
            if b.ts.time() >= session_start and b.ts.date() == last_ts.date()
        ]
        return session_bars, tag
