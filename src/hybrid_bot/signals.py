"""Правила hybrid_bot: тренд для входа и расстояние для фиксации.

─── Research basis ───
- Тренд: SMA(20) > SMA(50) на 4h — классический свинг-кросс (Murphy 1999,
  «Technical Analysis of the Financial Markets», ch.9). Это то же правило, по
  которому набиралась позиция в разобранных сделках (STRATEGY_HYBRID.md §17.1),
  поэтому оно и взято. Статистический гейт IS/OOS оно не проходит
  (STRATEGY_HYBRID.md §16), край не утверждается — бот собирает форвард.
- Фиксация от средней цены входа: расстояние посчитано на 1460 днях эфира
  (scripts/hybrid_fix_threshold.py, канон §17.6). Порог не подбирается по
  последним сделкам — значение живёт в настройках.
- Сайзинг: нотионал ∝ 40% годовых / реализованная вола, окно 60 дней,
  потолок 3× базовой ставки — Moskowitz/Ooi/Pedersen 2012. Формула та же,
  что в scripts/hybrid_core_select.py (§16.2): на 9 символах из 9 лучше
  Sharpe и просадка, чем фикс. нотионал. Одобрено 2026-08-24. Не край
  контура — меньше риска на том же сигнале.

Сигнал тренда считается ТОЛЬКО по закрытым барам (последний формирующийся
отрезается в клиенте).
"""

from __future__ import annotations

import math
import statistics

TREND_FAST = 20
TREND_SLOW = 50

# MOP 2012 / STRATEGY_HYBRID.md §16.2 — не подбирать.
VOL_TARGET_ANNUAL = 0.40
VOL_LOOKBACK_D = 60
VOL_MAX_MULT = 3.0


def sma(values: list[float], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    chunk = values[-window:]
    return sum(chunk) / window


def trend_long(closes: list[float]) -> int | None:
    """1 = держим покупку, 0 = стоим вне рынка, None = мало баров."""
    fast = sma(closes, TREND_FAST)
    slow = sma(closes, TREND_SLOW)
    if fast is None or slow is None:
        return None
    return 1 if fast > slow else 0


def fix_price(avg_entry: float, threshold_pct: float) -> float:
    """Цена, на которой закрываем позицию целиком."""
    return avg_entry * (1 + threshold_pct / 100.0)


def should_fix(last_price: float, avg_entry: float,
               threshold_pct: float) -> bool:
    if last_price <= 0 or avg_entry <= 0:
        return False
    return last_price >= fix_price(avg_entry, threshold_pct)


def distance_pct(last_price: float, avg_entry: float) -> float:
    """На сколько процентов цена ушла вверх от средней цены входа."""
    if avg_entry <= 0:
        return 0.0
    return (last_price / avg_entry - 1.0) * 100.0


def bars_per_day(interval: str) -> float:
    """Сколько закрытых баров в сутках. '240' = 4h → 6."""
    try:
        minutes = int(interval)
    except (TypeError, ValueError):
        return 6.0
    if minutes <= 0:
        return 6.0
    return 1440.0 / minutes


def realized_vol_annual(closes: list[float], *,
                        interval: str = "240") -> float | None:
    """Годовая волатильность по лог-доходностям за 60 дней.

    Та же формула, что scripts/hybrid_core_select.py:_realized_vol.
    Считается по закрытым барам, без текущего.
    """
    bpd = bars_per_day(interval)
    n = int(VOL_LOOKBACK_D * bpd)
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1):]
    rets = [math.log(window[k] / window[k - 1])
            for k in range(1, len(window)) if window[k - 1] > 0]
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    return sd * math.sqrt(365 * bpd)


def vol_scale(vol: float) -> float:
    """Множитель к базовой ставке: цель / вола, потолок 3×."""
    if vol <= 0:
        return 0.0
    return min(VOL_MAX_MULT, VOL_TARGET_ANNUAL / vol)


def vol_notional(base_usd: float, closes: list[float], *,
                 interval: str = "240") -> float | None:
    """Ставка в долларах. None — мало баров, вызывающий решает fallback."""
    vol = realized_vol_annual(closes, interval=interval)
    if vol is None:
        return None
    return base_usd * vol_scale(vol)
