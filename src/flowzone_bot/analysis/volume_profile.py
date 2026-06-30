"""Volume Profile engine flowzone_bot (POC / VAH / VAL / HVN / LVN / ledge).

Канон STRATEGY §3.1 + §6.3: профиль строится из ИСПОЛНЕННОГО ПОТОКА
(tick/footprint), а не из kline-volume, и агрегируется по корзинам цен.

Research basis (канон Market Profile + канон-автор):
- **Value Area ≈ 68% объёма** вокруг POC — канон-автор Fabervaale буквально
  называет 68%: видео «The Only Orderflow Guide» (28:50) — *«value area… where
  the 68% of the volume of the distribution took place»*; winkler-rulebook —
  *«Value Area boundaries — where 68% of volume was transacted»*. 68% = одно
  стандартное отклонение нормального распределения (Gaussian в ролике). Раньше
  было 0.70 (Steidlmayer/Dalton literature) — правка к канон-автору. Расширение
  Value Area от POC «двумя рядами» — каноничный CME/Dalton-алгоритм (сравниваем
  сумму двух соседних корзин сверху и снизу, добавляем больший ряд, пока не
  наберём value_area_pct).
- **POC (Point of Control)** — цена с максимальным объёмом (Steidlmayer).
- **HVN/LVN (high/low volume node)** — локальные максимумы/минимумы объёма по
  цене (Dalton: HVN = принятие/баланс, LVN = отвержение/быстрый проход).
- **Volume ledge** — резкий переход HVN→LVN (STRATEGY §3.1: «volume goes from
  really peak point to really low point… really fast»).

Все функции — чистые (детерминированы по входу), тестируются на синтетике с
известным распределением (TASKSPEC §8).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VolumeProfile:
    """Профиль объёма по корзинам цен. ``buckets``: idx → (buy_vol, sell_vol),
    цена корзины = idx × bucket_size (нижняя граница)."""
    bucket_size: float
    buckets: dict[int, tuple[float, float]]
    poc_idx: int
    poc_price: float          # центр POC-корзины
    vah: float                # верхняя граница Value Area (цена)
    val: float                # нижняя граница Value Area (цена)
    total_volume: float
    value_area_volume: float
    # idx-ы внутри Value Area (включительно lo..hi)
    va_lo_idx: int = 0
    va_hi_idx: int = 0

    def bucket_volume(self, idx: int) -> float:
        b = self.buckets.get(idx)
        return (b[0] + b[1]) if b else 0.0

    def bucket_delta(self, idx: int) -> float:
        """Дельта корзины: агрессивный buy − sell (delta-at-price, STRATEGY §3.2)."""
        b = self.buckets.get(idx)
        return (b[0] - b[1]) if b else 0.0

    def idx_of_price(self, price: float) -> int:
        return int(price / self.bucket_size) if self.bucket_size > 0 else 0

    def delta_at_price(self, price: float) -> float:
        return self.bucket_delta(self.idx_of_price(price))


def build_profile_from_prints(prints: list, bucket_size: float,
                              value_area_pct: float = 0.68
                              ) -> VolumeProfile | None:
    """Собрать VolumeProfile из списка принтов (канон §3 — профиль ПРЕДЫДУЩЕЙ
    swing-точки, A2). ``prints`` = [(ts, price, size, side), ...] (side =
    "Buy"|"Sell"). Принты агрегируются в корзины цен (idx = price/bucket_size)
    → (buy_vol, sell_vol), далее ``build_profile``.

    Источник принтов — SQLite ``prints`` (state/db.py), окно = [ts предыдущего
    swing, now] (переменная длина per-swing). Канон требует профиль из
    исполненного потока (footprint), не из kline-volume (no-data-fitting.mdc)."""
    if not prints or bucket_size <= 0:
        return None
    buckets: dict[int, list[float]] = {}
    for _ts, price, size, side in prints:
        if price <= 0 or size <= 0:
            continue
        idx = int(price / bucket_size)
        b = buckets.get(idx)
        if b is None:
            b = [0.0, 0.0]
            buckets[idx] = b
        if str(side).upper() == "BUY":
            b[0] += size
        else:
            b[1] += size
    if not buckets:
        return None
    return build_profile({i: (b[0], b[1]) for i, b in buckets.items()},
                         bucket_size, value_area_pct)


def build_profile(buckets: dict[int, tuple[float, float]], bucket_size: float,
                  value_area_pct: float = 0.68) -> VolumeProfile | None:
    """Собрать VolumeProfile из карты корзин (idx → (buy, sell)).

    Value Area — каноничным «двухрядным» расширением от POC (Steidlmayer/Dalton):
    на каждом шаге сравниваем сумму двух корзин над текущей VA и двух под ней,
    присоединяем больший ряд (обе корзины), пока объём VA < value_area_pct.
    Возвращает None если данных нет.
    """
    if not buckets or bucket_size <= 0:
        return None
    vols = {i: (bu + se) for i, (bu, se) in buckets.items()}
    total = sum(vols.values())
    if total <= 0:
        return None
    # POC: максимум объёма; тай-брейк — меньший idx (детерминизм).
    poc_idx = min((i for i in vols), key=lambda i: (-vols[i], i))
    min_idx, max_idx = min(vols), max(vols)
    target = total * value_area_pct
    lo = hi = poc_idx
    va = vols[poc_idx]

    def pair_above() -> float:
        return vols.get(hi + 1, 0.0) + vols.get(hi + 2, 0.0)

    def pair_below() -> float:
        return vols.get(lo - 1, 0.0) + vols.get(lo - 2, 0.0)

    while va < target and (lo > min_idx or hi < max_idx):
        up = pair_above() if hi < max_idx else -1.0
        dn = pair_below() if lo > min_idx else -1.0
        if up < 0 and dn < 0:
            break
        if up >= dn:
            for _ in range(2):
                if hi >= max_idx:
                    break
                hi += 1
                va += vols.get(hi, 0.0)
                if va >= target:
                    break
        else:
            for _ in range(2):
                if lo <= min_idx:
                    break
                lo -= 1
                va += vols.get(lo, 0.0)
                if va >= target:
                    break

    return VolumeProfile(
        bucket_size=bucket_size,
        buckets=dict(buckets),
        poc_idx=poc_idx,
        poc_price=(poc_idx + 0.5) * bucket_size,
        vah=(hi + 1) * bucket_size,   # верхняя граница верхней VA-корзины
        val=lo * bucket_size,          # нижняя граница нижней VA-корзины
        total_volume=total,
        value_area_volume=va,
        va_lo_idx=lo,
        va_hi_idx=hi,
    )


def merge_profiles(profiles: list[VolumeProfile],
                   value_area_pct: float = 0.68) -> VolumeProfile | None:
    """Composite / double-day profile (D3, канон-автор «The Only Orderflow
    Guide»: *«merge them… double day profile… merge»*).

    Сливает несколько `VolumeProfile` (одинакового `bucket_size`) в один
    composite: суммирует (buy, sell) по корзинам цен и пересчитывает POC / Value
    Area «двухрядным» алгоритмом `build_profile`. Сильные VAH/VAL, подтверждённые
    несколькими профилями, — мощные зоны reload (канон).

    [НАШЕ] инфра-утилита: в live-путь НЕ подключена по умолчанию
    (`FLOWZONE_PROFILE_MERGE_ENABLED=false`); включение composite-зон как
    торгового критерия требует OOS-валидации (`no-data-fitting.mdc`,
    `strategy-guard.mdc`). Возвращает None при пустом/несовместимом входе.
    """
    profiles = [p for p in profiles if p is not None]
    if not profiles:
        return None
    bucket_size = profiles[0].bucket_size
    if any(p.bucket_size != bucket_size for p in profiles):
        return None  # разные разрешения корзин — сливать бессмысленно
    merged: dict[int, list[float]] = {}
    for p in profiles:
        for idx, (bu, se) in p.buckets.items():
            b = merged.get(idx)
            if b is None:
                b = [0.0, 0.0]
                merged[idx] = b
            b[0] += bu
            b[1] += se
    if not merged:
        return None
    return build_profile({i: (b[0], b[1]) for i, b in merged.items()},
                         bucket_size, value_area_pct)


def find_hvn_lvn(profile: VolumeProfile) -> tuple[list[int], list[int]]:
    """HVN/LVN — локальные максимумы/минимумы объёма по цене среди НЕпустых
    корзин (Dalton). Возвращает (hvn_idxs, lvn_idxs). Края не классифицируем
    (нет двух соседей)."""
    idxs = sorted(profile.buckets)
    hvn: list[int] = []
    lvn: list[int] = []
    for k in range(1, len(idxs) - 1):
        i = idxs[k]
        v = profile.bucket_volume(i)
        vp = profile.bucket_volume(idxs[k - 1])
        vn = profile.bucket_volume(idxs[k + 1])
        if v > vp and v > vn:
            hvn.append(i)
        elif v < vp and v < vn:
            lvn.append(i)
    return hvn, lvn


@dataclass
class Ledge:
    """Volume ledge: резкий обрыв объёма от HVN к LVN. ``price`` — граница между
    корзинами обрыва; ``side`` = 'below' (объём падает вниз по цене) / 'above'."""
    idx: int           # idx high-volume корзины (пик перед обрывом)
    price: float       # цена границы обрыва
    side: str
    drop_ratio: float  # vol(low)/vol(high) — чем меньше, тем резче


def find_ledges(profile: VolumeProfile, drop_frac: float = 0.5) -> list[Ledge]:
    """Найти volume ledges: соседние НЕпустые корзины, где объём падает от пика
    к провалу «быстро» — vol(next)/vol(peak) ≤ drop_frac (STRATEGY §3.1: HVN→LVN
    really fast). ``drop_frac`` 0.5 = объём вдвое — нейтральный порог «резко»
    (не тюнинг под P&L; reversible). Каждая граница даёт ledge в обе стороны
    обрыва."""
    idxs = sorted(profile.buckets)
    out: list[Ledge] = []
    for k in range(len(idxs) - 1):
        i, j = idxs[k], idxs[k + 1]
        if j != i + 1:
            continue  # обрыв должен быть между СОСЕДНИМИ корзинами
        vi = profile.bucket_volume(i)
        vj = profile.bucket_volume(j)
        if vi <= 0 or vj <= 0:
            continue
        boundary = (i + 1) * profile.bucket_size
        if vj <= vi * drop_frac:  # объём резко падает ВВЕРХ по цене
            out.append(Ledge(idx=i, price=boundary, side="above",
                             drop_ratio=vj / vi))
        elif vi <= vj * drop_frac:  # объём резко падает ВНИЗ по цене
            out.append(Ledge(idx=j, price=boundary, side="below",
                             drop_ratio=vi / vj))
    return out
