"""Rolling Sharpe для AI Arena (Nof1 self-calibration signal).

Nof1 включает Sharpe Ratio в каждый user prompt как fidelity-feedback:
- Sharpe < 0   → reduce size, tighten stops
- Sharpe 0–1   → positive but volatile
- Sharpe > 1   → strategy working, maintain discipline
- Sharpe > 2   → excellent (но beware overconfidence)

Окно — 14 дней с скользящим шагом: rebalance каждый цикл (3 мин).
Возвращаем (mean - rf) / std для returns между последовательными
equity-snapshot'ами.

Формула канонична (Sharpe 1966):
    Sharpe = (avg_return - rf) / std(returns)

Risk-free rate = 0 (Nof1 phrasing «assuming risk-free rate of 0»).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta


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


def rolling_sharpe_14d(
    snapshots: list[dict],
    *,
    risk_free_rate: float = 0.0,
    annualization_factor: float | None = None,
) -> float | None:
    """Считает Sharpe из equity-snapshot'ов за последние 14 дней.

    Nof1 сообщает Sharpe «as is» (без annualization) — это per-cycle
    Sharpe. Мы тоже не аннуализируем по дефолту: каждый snapshot — это
    точка от 3-мин цикла, и сравнение с >0 / >1 / >2 не зависит от
    шкалы (сравнивается с порогами в одних и тех же единицах).

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


def cutoff_ts_14d_ago() -> int:
    return int((datetime.now(tz=UTC) - timedelta(days=14)).timestamp())
