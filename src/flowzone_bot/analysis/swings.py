"""Swing-точки flowzone_bot для целей сделки (STRATEGY §5.3, §8).

Канон: «Цель — ближайшая swing-точка» (§5.3 «targeting for a swing point»);
вход и структура читаются на M5 (§6.3 — ТФ входа). Swing-точка — локальный
экстремум цены: swing high выше соседних баров слева/справа, swing low — ниже.

Research basis: Bill Williams «Trading Chaos» (1995) — фрактал = бар, чей
максимум (минимум) строго выше (ниже) ``left`` баров слева и ``right`` баров
справа. Канонический фрактал Уильямса — 2 бара с каждой стороны (left=right=2):
это и есть «локальная swing-точка» на ТФ. Функции чистые, тестируются на
синтетических сериях с известными экстремумами.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Swing:
    idx: int          # индекс бара в серии
    price: float      # цена swing-точки (high для 'high', low для 'low')
    kind: str         # 'high' | 'low'
    ts: float = 0.0   # unix-время swing-бара (для per-swing профиля, A2; 0 если
                      # series без ts — тогда per-swing окно брать нельзя)


def find_swings(highs: list[float], lows: list[float], *, left: int = 2,
                right: int = 2,
                ts: list[float] | None = None) -> list[Swing]:
    """Найти фракталы Уильямса в сериях highs/lows.

    Swing high на баре i: highs[i] строго > highs всех ``left`` баров слева и
    ``right`` баров справа. Swing low зеркально по lows. Края (без полного окна
    left/right) не классифицируем — экстремум не подтверждён.

    ``ts`` — опц. unix-время баров; если передано, попадает в ``Swing.ts`` для
    per-swing профиля (A2: окно = [ts предыдущего swing, now]). Без ts —
    ``Swing.ts=0`` (per-swing окно собрать нельзя)."""
    n = min(len(highs), len(lows))
    if n == 0 or left < 1 or right < 1:
        return []
    have_ts = ts is not None and len(ts) >= n
    out: list[Swing] = []
    for i in range(left, n - right):
        hi = highs[i]
        if all(hi > highs[j] for j in range(i - left, i)) and \
           all(hi > highs[j] for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, hi, "high", ts[i] if have_ts else 0.0))
        lo = lows[i]
        if all(lo < lows[j] for j in range(i - left, i)) and \
           all(lo < lows[j] for j in range(i + 1, i + right + 1)):
            out.append(Swing(i, lo, "low", ts[i] if have_ts else 0.0))
    return out


def nearest_swing_target(swings: list[Swing], side: str, entry: float
                         ) -> float | None:
    """Ближайшая swing-цель по направлению сделки (континуация, §5.4).

    Шорт (вниз): ближайший swing LOW НИЖЕ входа (наибольший low < entry).
    Лонг (вверх): ближайший swing HIGH ВЫШЕ входа (наименьший high > entry).
    None если в сторону цели подтверждённой swing-точки нет.
    """
    if side == "short":
        lows = [s.price for s in swings if s.kind == "low" and s.price < entry]
        return max(lows) if lows else None
    if side == "long":
        highs = [s.price for s in swings if s.kind == "high" and s.price > entry]
        return min(highs) if highs else None
    return None


def swing_targets(swings: list[Swing], side: str, entry: float) -> list[float]:
    """Все swing-цели по направлению сделки, по близости к входу (ближняя
    первой). Для частичной фиксации: цель 1 = ближайшая, цель 2 = следующая
    (reload-структура, STRATEGY §5.3)."""
    if side == "short":
        lows = sorted((s.price for s in swings
                       if s.kind == "low" and s.price < entry), reverse=True)
        return lows
    if side == "long":
        highs = sorted(s.price for s in swings
                       if s.kind == "high" and s.price > entry)
        return highs
    return []
