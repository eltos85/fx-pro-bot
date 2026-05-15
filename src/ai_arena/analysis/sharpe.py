"""Cumulative Sharpe для AI Arena (Nof1 self-calibration signal).

Source (gist nof1-prompt.md, секция PERFORMANCE METRICS & FEEDBACK):

    Sharpe Ratio = (Average Return - Risk-Free Rate) / Standard Deviation of Returns

    Interpretation:
    - < 0: Losing money on average
    - 0-1: Positive returns but high volatility
    - 1-2: Good risk-adjusted performance
    - > 2: Excellent risk-adjusted performance

Source НЕ указывает rolling-окно. Nof1 Season 1 идёт cumulative с
17 окт 2025 (~7 месяцев на момент аудита 2026-05-15) и сообщает
Sharpe вместе с `Current Total Return (percent)` — оба идут с момента
старта эксперимента. Раньше у нас стоял rolling 14d (наша
интерпретация) — это расходилось с source. Теперь — **cumulative**
с момента старта бота (`get_started_at_ts`).

Risk-free rate = 0 (gist подтверждает по умолчанию).

Формула канонична (Sharpe 1966).
"""
from __future__ import annotations

import math


def compute_returns(equities: list[float]) -> list[float]:
    """Простые арифметические returns между последовательными точками."""
    if len(equities) < 2:
        return []
    out: list[float] = []
    prev = equities[0]
    for e in equities[1:]:
        if prev <= 0:
            prev = e
            continue
        out.append((e - prev) / prev)
        prev = e
    return out


def cumulative_sharpe(
    snapshots: list[dict],
    *,
    risk_free_rate: float = 0.0,
    annualization_factor: float | None = None,
) -> float | None:
    """Считает Sharpe из всех equity-snapshot'ов с момента старта.

    Nof1 сообщает Sharpe «as is» (без annualization) — это per-cycle
    Sharpe от per-cycle returns. Сравнение с порогами `>0 / >1 / >2`
    не зависит от шкалы.

    Если хочется annualize — передать ``annualization_factor``:
    sqrt(N), где N = ожидаемое число snapshots в год (3-мин цикл →
    N ≈ 175200, sqrt ≈ 418).

    snapshots — список dict с ключами ``ts`` (unix sec) и
    ``total_equity_usd``. Должны быть отсортированы по ts (ascending).

    Возвращает None если данных не хватает (< 3 точек) или std=0.
    """
    if not snapshots or len(snapshots) < 3:
        return None
    equities = [float(s["total_equity_usd"]) for s in snapshots]
    returns = compute_returns(equities)
    if len(returns) < 2:
        return None
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return None
    sharpe = (mean_r - risk_free_rate) / std
    if annualization_factor is not None:
        sharpe *= annualization_factor
    return sharpe
