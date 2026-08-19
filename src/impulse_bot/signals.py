"""Правила входа. Числа из постов, не из наших бэктестов.

─── Research basis ───
- удар: Bitcointalk topic=5577812 (Dzhango): ≥$30k за короткий срез
  и ход цены ≥0.2%; вселенная не BTC/ETH/SOL, оборот $100k–$15M.
- лента: CScalp «лента сделок» — сторона тейкера ест лимиты
  (fsr-develop.ru/kak-rabotaet-lenta-sdelok-v-cscalp).
- кластер: CScalp — объём копится в ценовом кармане по ходу удара.
- цель: ForexFactory thread/1014708 — не микро-тейк размером с комиссию.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


MAJORS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})


@dataclass(frozen=True)
class Burst:
    symbol: str
    move_pct: float
    burst_usd: float
    side: str  # Buy | Sell


@dataclass(frozen=True)
class Tape:
    buy_usd: float
    sell_usd: float


@dataclass(frozen=True)
class Cluster:
    dir_frac: float


def in_universe(symbol: str, turnover24h: float, *,
                skip: set[str], lo: float, hi: float) -> bool:
    if not symbol.endswith("USDT"):
        return False
    if symbol in skip or symbol in MAJORS:
        return False
    return lo <= turnover24h <= hi


def detect_burst(symbol: str, prev_px: float, prev_turn: float,
                 px: float, turn: float, *,
                 burst_usd: float, move_pct: float) -> Burst | None:
    if prev_px <= 0:
        return None
    delta = turn - prev_turn
    if delta < burst_usd:
        return None
    move = (px / prev_px - 1.0) * 100.0
    if abs(move) < move_pct:
        return None
    return Burst(symbol=symbol, move_pct=move, burst_usd=delta,
                 side="Buy" if move > 0 else "Sell")


def tape_ok(tape: Tape, side: str, *, ratio: float) -> bool:
    if side == "Buy":
        return tape.buy_usd > tape.sell_usd * ratio
    return tape.sell_usd > tape.buy_usd * ratio


def cluster_ok(cl: Cluster, *, min_frac: float = 0.30) -> bool:
    return cl.dir_frac >= min_frac


def tape_from_prints(prints: list[tuple[str, float]]) -> Tape:
    """prints: (side Buy|Sell, usd)."""
    buys = sells = 0.0
    for side, usd in prints:
        if side == "Buy":
            buys += usd
        else:
            sells += usd
    return Tape(buys, sells)


def cluster_from_prints(prints: list[tuple[float, float]], side: str,
                        *, n_bins: int = 5) -> Cluster:
    """Карман по ходу удара: лонг — верхний бин, шорт — нижний.

    prints: (price, usd). 5 бинов — дискретизация ленты, не параметр из бэктеста.
    """
    if not prints:
        return Cluster(0.0)
    lo = min(p for p, _ in prints)
    hi = max(p for p, _ in prints)
    span = hi - lo
    if span <= 0:
        return Cluster(1.0)
    bins = [0.0] * n_bins
    for px, usd in prints:
        i = min(n_bins - 1, int((px - lo) / span * n_bins))
        bins[i] += usd
    total = sum(bins)
    if total <= 0:
        return Cluster(0.0)
    frac = (bins[-1] if side == "Buy" else bins[0]) / total
    return Cluster(frac)


def should_enter(burst: Burst | None, tape: Tape | None,
                 cluster: Cluster | None, *, tape_ratio: float) -> bool:
    """Удар обязателен. Лента и кластер — оба, как в методичке CScalp."""
    if burst is None or tape is None or cluster is None:
        return False
    return (tape_ok(tape, burst.side, ratio=tape_ratio)
            and cluster_ok(cluster))


def clamp_mkt_qty(qty: float, *, max_mkt: float, min_qty: float,
                  step: float) -> tuple[float, bool] | None:
    """Обрезать рыночный лот до maxMktOrderQty.

    Bybit lotSizeFilter.maxMktOrderQty — лимит Market, не maxOrderQty.
    https://bybit-exchange.github.io/docs/v5/market/instrument
    None — после обрезки лот меньше минимума.
    """
    if qty <= 0:
        return None
    capped = False
    if max_mkt > 0 and qty > max_mkt:
        qty = max_mkt
        capped = True
    if step > 0:
        qty = math.floor(qty / step) * step
    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        return None
    return qty, capped


def in_session(hour_utc: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour_utc < end
    return hour_utc >= start or hour_utc < end
