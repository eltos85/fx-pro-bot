"""Gold ORB Isolated — пробой Opening Range на XAU/USD (London + NY opens).

## H2 ATR-regime + H5 Liquidity-sweep фильтры (active 2026-05-04)

Активны два дополнительных фильтра, основанные на 365-дневном backtest
M5 GC=F (493 сделки baseline, см. BUILDLOG.md 2026-05-04
«research(H1-H5 results)»):

- **H2 ATR Regime Filter** (`regime_filter=True`):
  signal проходит только если **daily ATR-14d > P70** на 30-day rolling
  window (волатильный режим «expansion»). По 365d данным:
  - 188 сделок (38% выборки) попали в expansion → **PF 2.02 vs 1.57
    baseline** (IS edge +0.05, OOS edge +0.87, не флипает).
  Источник: mql5.com/en/blogs/post/769030 «Regime Mismatch» Apr 2026 +
  XAU SENTINEL v2.2; Crabel «Day Trading with Short Term Price Patterns»
  (1990) ch.7 для ATR-regime.

- **H5 Liquidity Sweep Filter** (`sweep_filter=True`):
  signal проходит только если в last 50 M5 баров до сигнала был
  «sweep»: prior_window bars[-50:-10] установил extremum, recent_window
  bars[-10:-1] этот extremum пробил, signal-бар вернулся внутрь prior
  range. По 365d: 42 сделки (8.5%) → **PF 2.98 vs 1.57 baseline**
  (IS PF 2.56, OOS PF 3.41, edge не флипает).
  Источник: ICT/SMC «Liquidity Grab» paradigm (2026 retail education
  + institutional order flow theory).

**Compliance — user-override Bonferroni.** Backtest показал устойчивый
edge без sign-flip, но Bonferroni-correction p < 0.01 на ALL не
достигнут (H2: p=0.135, H5: p=0.05). User (демо-счёт, риск убытков
приемлем) принял решение об активации с явным записанным override в
BUILDLOG.md 2026-05-04 «activate(H2+H5)». При деплое сдвигается
baseline-дата (`fxpro-stats-baseline.mdc`).

**Параметры — frozen (no-data-fitting.mdc).**
- `H2_ATR_PERCENTILE = 70.0` (P70 на 30-day rolling)
- `H2_DAILY_ATR_WINDOW = 30` (rolling window для percentile)
- `H5_LOOKBACK_BARS = 50` (~4 часа M5)
- `H5_PRIOR_END_OFFSET = 10` (последние 10 баров — recent window)
Подкручивание этих чисел = curve-fitting, запрещено.



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

# ─── H2 ATR-regime / H5 Liquidity-sweep — frozen из research ───
# (BUILDLOG 2026-05-04, scripts/test_h1_h5_filters.py).
# НЕ подкручивать без обновления research-источника.
H2_ATR_PERCENTILE = 70.0
H2_DAILY_ATR_WINDOW = 30
H2_DAILY_ATR_PERIOD = 14
H5_LOOKBACK_BARS = 50
H5_PRIOR_END_OFFSET = 10


def _percentile(values: list[float], pct: float) -> float:
    """Линейная интерполяция percentile (как np.percentile, без numpy)."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (pct / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


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
    # H2/H5 meta-fields (для логирования и audit'а), используются
    # `regime_filter` / `sweep_filter` для допуска или блока сигнала.
    # См. docstring модуля «H2 ATR-regime + H5 Liquidity-sweep».
    h2_regime: str = "unknown"           # "expansion" | "normal" | "compression" | "unknown"
    h2_atr_percentile: float = -1.0      # текущий daily ATR vs 30d window
    h5_swept_pre: bool = False           # был ли sweep liquidity до пробоя


class GoldOrbStrategy:
    """Gold ORB: изолированный ORB только для XAU/USD."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 2,       # max 1 на сессию × 2 сессии в день
        max_per_instrument: int = 1,  # только один trade на XAU за раз
        shadow: bool = False,         # если True — сигналы только логируются, без открытия
        regime_filter: bool = False,  # H2: блокировать compression/normal
        sweep_filter: bool = False,   # H5: блокировать non-swept signals
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._shadow = shadow
        self._regime_filter = regime_filter
        self._sweep_filter = sweep_filter
        # Cache daily ATR series для H2-percentile (обновляется
        # отдельным fetch'ем daily-баров, см. update_daily_atr_history).
        self._daily_atr_history: list[float] = []
        self._daily_atr_history_date: str | None = None

    def update_daily_atr_history(self, daily_bars: list[Bar]) -> None:
        """Обновить series daily ATR-14d из последних N daily баров.

        Вызывается из main.py раз в час. Считает ATR-14d на каждом
        баре (после первых 15) и сохраняет в self._daily_atr_history.

        Если daily_bars короче 30 — фильтр H2 не активируется (см.
        _h2_regime).
        """
        if not daily_bars or len(daily_bars) < H2_DAILY_ATR_PERIOD + 2:
            return
        # ATR на daily-барах (используем тот же _atr что и для M5).
        # _atr принимает list[Bar] и возвращает single value по последним
        # 14 барам — итерируем sliding window.
        atrs: list[float] = []
        for i in range(H2_DAILY_ATR_PERIOD + 1, len(daily_bars) + 1):
            window = daily_bars[i - H2_DAILY_ATR_PERIOD - 1: i]
            atr = _atr(window)
            if atr > 0:
                atrs.append(atr)
        if len(atrs) >= H2_DAILY_ATR_WINDOW:
            self._daily_atr_history = atrs
            self._daily_atr_history_date = daily_bars[-1].ts.date().isoformat()
            log.info(
                "GOLD-ORB H2: daily ATR history updated (%d points, last day %s, "
                "current_atr=%.2f, P70=%.2f)",
                len(atrs), self._daily_atr_history_date,
                atrs[-1], _percentile(atrs[-H2_DAILY_ATR_WINDOW:], H2_ATR_PERCENTILE),
            )

    def _h2_regime(self) -> tuple[str, float]:
        """Возвращает (regime_label, current_percentile_pct).

        Если daily ATR history не загружена / выборки <30 — ("unknown", -1).
        """
        if len(self._daily_atr_history) < H2_DAILY_ATR_WINDOW:
            return "unknown", -1.0
        window = self._daily_atr_history[-H2_DAILY_ATR_WINDOW:]
        current = self._daily_atr_history[-1]
        # Percentile rank current внутри window (% значений ниже current).
        below = sum(1 for v in window if v < current)
        pct = 100.0 * below / len(window)
        if pct >= H2_ATR_PERCENTILE:
            return "expansion", pct
        if pct <= 30.0:
            return "compression", pct
        return "normal", pct

    def _h5_swept(
        self, bars: list[Bar], signal_idx: int, direction: TrendDirection,
    ) -> bool:
        """Проверка liquidity-sweep до пробоя.

        Long: prior_low (bars[-50:-10]) выкошен в recent (bars[-10:-1]),
              signal-bar close >= prior_low.
        Short: зеркально для high.
        """
        if signal_idx < H5_LOOKBACK_BARS:
            return False
        lb_start = signal_idx - H5_LOOKBACK_BARS
        prior_end = signal_idx - H5_PRIOR_END_OFFSET
        if prior_end <= lb_start:
            return False
        prior = bars[lb_start:prior_end]
        recent = bars[prior_end:signal_idx]
        if not prior or not recent:
            return False
        sig_bar = bars[signal_idx]
        if direction == TrendDirection.LONG:
            prior_low = min(b.low for b in prior)
            recent_low = min(b.low for b in recent)
            return recent_low < prior_low and sig_bar.close >= prior_low
        prior_high = max(b.high for b in prior)
        recent_high = max(b.high for b in recent)
        return recent_high > prior_high and sig_bar.close <= prior_high

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

            atr_pips = round(sig.atr / ps, 1) if ps > 0 else 0.0
            try:
                self._store.save_open_diagnostics(
                    pid,
                    shadow_f1_status=f1,
                    shadow_f2_status=f2,
                    break_distance_atr=sig.break_distance_atr,
                    bars_since_box_end=sig.bars_since_box_end,
                    atr_at_open_pips=atr_pips,
                    h2_regime=sig.h2_regime,
                    h2_atr_percentile=sig.h2_atr_percentile,
                    h5_swept_pre=sig.h5_swept_pre,
                )
            except Exception as exc:
                log.warning("save_open_diagnostics failed for %s: %s", pid, exc)

            log.info(
                "  GOLD-ORB OPEN: %s %s @ %.5f [%s, box=[%.2f..%.2f], SL=%.2f] "
                "[SHADOW F1=%s F2=%s break=%.2fATR age=%db] "
                "[H2 regime=%s pct=%.1f%% H5 swept=%s]",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.session, sig.box_high, sig.box_low, sl,
                f1, f2, sig.break_distance_atr, sig.bars_since_box_end,
                sig.h2_regime, sig.h2_atr_percentile, sig.h5_swept_pre,
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

        # H2/H5 meta-fields (вычисляются всегда, фильтруют только если включены).
        regime, atr_pct = self._h2_regime()
        signal_idx = len(bars) - 1

        # touch-break: high/low текущего бара пересёк границу
        if last.high > box_high:
            if slope < 0:   # contra-trend защита: LONG только при slope>=0
                return None
            break_dist_atr = (last.high - box_high) / atr if atr > 0 else 0.0
            swept = self._h5_swept(bars, signal_idx, TrendDirection.LONG)
            if not self._allow_signal(symbol, TrendDirection.LONG, regime, atr_pct, swept):
                return None
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
                h2_regime=regime,
                h2_atr_percentile=round(atr_pct, 1),
                h5_swept_pre=swept,
            )

        if last.low < box_low:
            if slope > 0:
                return None
            break_dist_atr = (box_low - last.low) / atr if atr > 0 else 0.0
            swept = self._h5_swept(bars, signal_idx, TrendDirection.SHORT)
            if not self._allow_signal(symbol, TrendDirection.SHORT, regime, atr_pct, swept):
                return None
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
                h2_regime=regime,
                h2_atr_percentile=round(atr_pct, 1),
                h5_swept_pre=swept,
            )

        return None

    def _allow_signal(
        self,
        symbol: str,
        direction: TrendDirection,
        regime: str,
        atr_pct: float,
        swept: bool,
    ) -> bool:
        """Применить H2 + H5 фильтры. Возвращает False если signal заблокирован.

        - H2 (regime_filter=True): пропускаем только expansion. Если
          history не загружена (regime="unknown") — fail-safe: signal
          ПРОПУСКАЕТСЯ (не блокируем без data, иначе бот не торгует
          до первого daily fetch'а).
        - H5 (sweep_filter=True): пропускаем только swept=True.
        """
        if self._regime_filter and regime != "unknown" and regime != "expansion":
            log.info(
                "  GOLD-ORB BLOCK[H2]: %s %s regime=%s atr_pct=%.1f%% < P70 — skip",
                display_name(symbol), direction.value.upper(), regime, atr_pct,
            )
            return False
        if self._sweep_filter and not swept:
            log.info(
                "  GOLD-ORB BLOCK[H5]: %s %s no liquidity-sweep in last %d bars — skip",
                display_name(symbol), direction.value.upper(), H5_LOOKBACK_BARS,
            )
            return False
        return True

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
