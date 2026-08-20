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

Сигнал тренда считается ТОЛЬКО по закрытым барам (последний формирующийся
отрезается в клиенте).
"""

from __future__ import annotations

TREND_FAST = 20
TREND_SLOW = 50


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
