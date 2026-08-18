"""Канонические сигналы long/flat. Без подбора окон.

─── Research basis ───
- sma200_daily: close > SMA(200) на дневных барах — Murphy 1999,
  «Technical Analysis of the Financial Markets», режимный фильтр.
  Проверено scripts/scalp_vip0_trend_research.py (5 лет BTC, VIP 0 0.110%):
  +192% vs B&H +70%, просадка −32% vs −67%.
- sma20_50_4h: SMA(20) > SMA(50) на 4h — классический свинг-кросс
  (Murphy 1999). Срок удержания в замере ~6 дней
  (scripts/scalp_swing_research.py). Стат. гейт IS/OOS не пройден —
  бот собирает форвард, не утверждает край.

Сигнал считается ТОЛЬКО по закрытым барам (последний формирующийся
отрезается снаружи).
"""

from __future__ import annotations


def sma(values: list[float], window: int) -> float | None:
    if window <= 0 or len(values) < window:
        return None
    chunk = values[-window:]
    return sum(chunk) / window


def sma200_daily(closes: list[float]) -> int | None:
    """1 = long, 0 = flat. None = мало баров."""
    avg = sma(closes, 200)
    if avg is None:
        return None
    return 1 if closes[-1] > avg else 0


def sma20_50_4h(closes: list[float]) -> int | None:
    fast = sma(closes, 20)
    slow = sma(closes, 50)
    if fast is None or slow is None:
        return None
    return 1 if fast > slow else 0


STRATEGIES = {
    "sma200_daily": sma200_daily,
    "sma20_50_4h": sma20_50_4h,
}
