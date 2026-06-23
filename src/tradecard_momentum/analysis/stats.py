"""Статистика значимости для гейтов sample-size.mdc (без подгонки).

Изолированная копия (пакет самостоятельный, без импортов из tradecard_bybit —
TASKSPEC §9). Реализация на stdlib (нормальная аппроксимация + erf), чтобы не
тянуть scipy. Все пороги (n, p) — из настроек, не зашиты.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _norm_sf(z: float) -> float:
    """1 − CDF стандартного нормального (правый хвост)."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


@dataclass
class ProportionTest:
    p1: float
    p2: float
    diff: float
    z: float
    p_value: float
    n1: int
    n2: int

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05


def two_proportion_test(success1: int, n1: int, success2: int, n2: int,
                        ) -> ProportionTest | None:
    """Двусторонний z-test разницы двух долей (pooled). None если выборки пусты."""
    if n1 <= 0 or n2 <= 0:
        return None
    p1 = success1 / n1
    p2 = success2 / n2
    pool = (success1 + success2) / (n1 + n2)
    se = math.sqrt(pool * (1.0 - pool) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        z = 0.0
        pval = 1.0
    else:
        z = (p2 - p1) / se
        pval = 2.0 * _norm_sf(abs(z))
    return ProportionTest(p1=p1, p2=p2, diff=p2 - p1, z=z, p_value=pval,
                          n1=n1, n2=n2)


def spearman_rho(xs: list[float], ys: list[float]) -> float | None:
    """Ранговый коэффициент Спирмена (монотонность кривой грейд→EXP, §5)."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx = _ranks(xs)
    ry = _ranks(ys)
    return _pearson(rx, ry)


def _ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)
