"""Построитель торговых зон flowzone_bot (confluence из факторов VP).

Канон STRATEGY §3: зона — место максимального давления в прошлом, куда цена
вернётся для перезарядки (reload по направлению аукциона). Строится из факторов
объёмного профиля; самая сильная зона — там, где СОВПАДАЮТ несколько факторов
(§3.4 «confluence of value area high, big trades and delta level… super strong
area»). Чеклист §7 п.3: конфлюэнс ≥2 факторов = зона.

Факторы (канон §3.1-3.3):
- ``value_area`` — граница Value Area (VAH для шорта-резистанса / VAL для лонга);
- ``poc`` — Point of Control;
- ``ledge`` — volume ledge (резкий HVN→LVN);
- ``delta`` — сильная дельта-печать на уровне (одно-сторонний исполненный поток);
- ``big_trades`` — уровень, поддержанный крупными исполненными сделками.

Зона берётся ТОЛЬКО по направлению аукциона (continuation, §1, §5.4): для шорта
— уровни ВЫШЕ цены (резистанс, reload шорта), для лонга — НИЖЕ (саппорт).
Все функции чистые — тестируются на синтетическом профиле.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.analysis.orderflow import detect_big_trades
from flowzone_bot.analysis.volume_profile import VolumeProfile, find_ledges
from flowzone_bot.data.aggregates import TradePrint


@dataclass
class Zone:
    side: str                 # сторона continuation-входа: long | short
    low: float                # нижняя граница зоны (цена)
    high: float               # верхняя граница зоны (цена)
    price: float              # референс входа (ближняя к цене граница зоны)
    score: int                # число РАЗНЫХ факторов конфлюэнса
    factors: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float, pad: float = 0.0) -> bool:
        return (self.low - pad) <= price <= (self.high + pad)


def _delta_level(profile: VolumeProfile, min_frac: float) -> float | None:
    """Цена корзины с самой сильной односторонней дельта-печатью (|delta| ≥
    min_frac × объём корзины). None если выраженной нет."""
    best_idx = None
    best_abs = 0.0
    for idx in profile.buckets:
        vol = profile.bucket_volume(idx)
        if vol <= 0:
            continue
        d = abs(profile.bucket_delta(idx))
        if d >= min_frac * vol and d > best_abs:
            best_abs = d
            best_idx = idx
    if best_idx is None:
        return None
    return (best_idx + 0.5) * profile.bucket_size


def build_zones(profile: VolumeProfile, side: str, ref_price: float,
                recent_trades: list[TradePrint], *,
                big_threshold: float | None = None,
                min_confluence: int = 2, cluster_ticks: int = 5,
                delta_min_frac: float = 0.6, ledge_drop_frac: float = 0.5
                ) -> list[Zone]:
    """Собрать зоны конфлюэнса по направлению ``side`` относительно ``ref_price``.

    Кандидат-уровни (factor, price) кластеризуются по близости (tolerance =
    bucket_size × cluster_ticks); зона = кластер, её score = число РАЗНЫХ
    факторов. Возвращаются только зоны на нужной стороне (шорт — выше цены, лонг
    — ниже) со score ≥ min_confluence, отсортированные по score убыв.
    """
    if profile is None or side not in ("long", "short") or profile.bucket_size <= 0:
        return []
    cand: list[tuple[str, float]] = []
    cand.append(("value_area", profile.vah if side == "short" else profile.val))
    cand.append(("poc", profile.poc_price))
    for lg in find_ledges(profile, drop_frac=ledge_drop_frac):
        cand.append(("ledge", lg.price))
    dl = _delta_level(profile, delta_min_frac)
    if dl is not None:
        cand.append(("delta", dl))
    if big_threshold is not None:
        for t in detect_big_trades(recent_trades, big_threshold):
            cand.append(("big_trades", t.price))

    # сторона континуации: шорт reload-ит выше цены, лонг — ниже
    if side == "short":
        cand = [(f, p) for f, p in cand if p >= ref_price]
    else:
        cand = [(f, p) for f, p in cand if p <= ref_price]
    if not cand:
        return []

    tol = profile.bucket_size * cluster_ticks
    cand.sort(key=lambda x: x[1])
    clusters: list[list[tuple[str, float]]] = []
    for f, p in cand:
        if clusters and p - clusters[-1][-1][1] <= tol:
            clusters[-1].append((f, p))
        else:
            clusters.append([(f, p)])

    zones: list[Zone] = []
    pad = profile.bucket_size / 2.0
    for cl in clusters:
        factors = sorted({f for f, _ in cl})
        if len(factors) < min_confluence:
            continue
        prices = [p for _, p in cl]
        low, high = min(prices) - pad, max(prices) + pad
        # референс входа — ближняя к текущей цене граница (лимитка «в зоне»)
        entry_ref = low if side == "short" else high
        zones.append(Zone(side=side, low=low, high=high, price=entry_ref,
                          score=len(factors), factors=factors))
    zones.sort(key=lambda z: z.score, reverse=True)
    return zones
