"""Order-flow примитивы flowzone_bot: big-trades + absorption-триггер.

Канон STRATEGY §3.2-3.4, §4:
- **delta-at-price (delta print)** — дельта (агрессивный buy − sell) на уровне.
  Реализована в ``volume_profile.VolumeProfile.bucket_delta/delta_at_price``;
  здесь — сумма дельты по принтам в ценовой ПОЛОСЕ зоны.
- **big trades** (§3.3) — крупные исполненные принты, маркирующие уровень.
  Порог относительный: percentile размера сделок за окно (TASKSPEC §6.3 — не
  magic-number). Практика footprint/order-flow: institutional-tail размеров.
- **absorption** (§4) — главный триггер входа: контр-сторона агрессирует, но
  ПОГЛОЩАЕТСЯ доминирующей и не двигает цену в свою сторону («failed buyers/
  sellers», deep trades в теле свечи). Absorption = много агрессии, нет
  движения (каноничный order-flow признак, Bookmap/footprint).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.data.aggregates import TradePrint


def size_percentile(sizes: list[float], pct: float) -> float | None:
    """Percentile размера (линейная интерполяция). None если пусто."""
    if not sizes:
        return None
    s = sorted(sizes)
    if len(s) == 1:
        return s[0]
    pct = min(max(pct, 0.0), 1.0)
    pos = pct * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(s):
        return s[-1]
    return s[lo] + (s[lo + 1] - s[lo]) * frac


def big_trade_threshold(trades: list[TradePrint], *, pct: float = 0.90,
                        min_samples: int = 20) -> float | None:
    """Порог «крупной» сделки = percentile размеров за окно. None если данных
    мало (< min_samples) — на малой выборке percentile = шум (sample-size)."""
    if len(trades) < max(2, min_samples):
        return None
    return size_percentile([t.size for t in trades], pct)


def detect_big_trades(trades: list[TradePrint], threshold: float,
                      side: str | None = None) -> list[TradePrint]:
    """Принты с size ≥ threshold (опц. фильтр по стороне агрессора Buy/Sell)."""
    out = []
    for t in trades:
        if t.size < threshold:
            continue
        if side is not None and t.side.upper() != side.upper():
            continue
        out.append(t)
    return out


def zone_delta(trades: list[TradePrint], low: float, high: float) -> float:
    """delta print в ценовой ПОЛОСЕ зоны [low, high]: Σ signed_delta принтов,
    попавших в полосу (STRATEGY §3.2 — дельта, исполненная ИМЕННО на уровне)."""
    return sum(t.signed_delta for t in trades if low <= t.price <= high)


@dataclass
class Absorption:
    """Результат проверки поглощения контр-стороны в окне «тела свечи»."""
    confirmed: bool
    side: str                       # сторона НАШЕЙ сделки: long | short
    counter_vol: float = 0.0        # объём контр-стороны (агрессоры, что failed)
    dominant_vol: float = 0.0       # объём доминирующей стороны
    price_move: float = 0.0         # last − first в окне (signed)
    big_counter: int = 0            # число крупных принтов контр-стороны
    reasons: list[str] = field(default_factory=list)


def detect_absorption(trades: list[TradePrint], side: str, *,
                      big_threshold: float | None,
                      min_counter_frac: float = 0.5) -> Absorption:
    """Подтверждение absorption контр-стороны для входа ``side``.

    Для ШОРТА контр-сторона = агрессивные ПОКУПАТЕЛИ: они давят вверх, но
    поглощаются продавцами и цена НЕ растёт → «failed buyers». Для ЛОНГА
    зеркально: агрессивные продавцы поглощены, цена не падает → «failed sellers».

    Условия (все обязательны):
    1. контр-сторона ≥ ``min_counter_frac`` объёма окна (реально агрессировала);
    2. ≥1 крупная сделка контр-стороны (deep trade в теле свечи), если порог
       big_threshold известен;
    3. цена НЕ прошла в сторону контр-агрессии (поглощена): для шорта price_move
       ≤ 0 (не выросла), для лонга ≥ 0 (не упала).
    """
    if side not in ("long", "short") or not trades:
        return Absorption(False, side, reasons=["no_data"])
    buy_vol = sum(t.size for t in trades if t.side.upper() == "BUY")
    sell_vol = sum(t.size for t in trades if t.side.upper() == "SELL")
    total = buy_vol + sell_vol
    price_move = trades[-1].price - trades[0].price
    if total <= 0:
        return Absorption(False, side, reasons=["empty"])

    # контр-сторона = та, которую должны поглотить (против направления сделки)
    if side == "short":
        counter_vol, dominant_vol = buy_vol, sell_vol
        counter_side = "Buy"
        price_absorbed = price_move <= 0   # покупатели давили, но цена не выросла
    else:  # long
        counter_vol, dominant_vol = sell_vol, buy_vol
        counter_side = "Sell"
        price_absorbed = price_move >= 0   # продавцы давили, но цена не упала

    reasons: list[str] = []
    counter_frac = counter_vol / total
    cond_pressure = counter_frac >= min_counter_frac
    if cond_pressure:
        reasons.append(f"counter_pressure={counter_frac:.0%}")
    big_counter = 0
    if big_threshold is not None:
        big_counter = len(detect_big_trades(trades, big_threshold, side=counter_side))
    cond_big = big_counter >= 1 if big_threshold is not None else False
    if cond_big:
        reasons.append(f"deep_trades={big_counter}")
    if price_absorbed:
        reasons.append("price_absorbed")

    confirmed = bool(cond_pressure and cond_big and price_absorbed)
    if not confirmed and not reasons:
        reasons.append("no_absorption")
    return Absorption(confirmed, side, counter_vol=counter_vol,
                      dominant_vol=dominant_vol, price_move=price_move,
                      big_counter=big_counter, reasons=reasons)
