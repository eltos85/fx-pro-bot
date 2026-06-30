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


# ─── D7: initiative auction / exhaustion (доп. order-flow паттерны) ───────
# Канон «The Only Orderflow Guide»: initiative = сильная направленная дельта +
# close в сторону агрессии → continuation; exhaustion = затухающий объём +
# contrarian imbalance на экстремуме → разворот.
# [НАШЕ] детекторы: в live-вход НЕ гейтят по умолчанию
# (`FLOWZONE_INITIATIVE_EXHAUSTION_ENABLED=false`); основной канон-сетап —
# absorption-reload (§4). Гейтинг новых триггеров требует OOS-валидации
# (no-data-fitting.mdc, strategy-guard.mdc).


@dataclass
class Initiative:
    """Результат проверки initiative auction (continuation-паттерн)."""
    confirmed: bool
    side: str                  # направление инициативы: long | short
    net_delta: float = 0.0     # buy_vol − sell_vol
    delta_frac: float = 0.0    # |net_delta| / total
    price_move: float = 0.0
    big_dominant: int = 0
    reasons: list[str] = field(default_factory=list)


def detect_initiative(trades: list[TradePrint], side: str, *,
                      big_threshold: float | None = None,
                      min_delta_frac: float = 0.30) -> Initiative:
    """Initiative auction — сильная направленная дельта + цена идёт в сторону
    агрессии (канон continuation). Для ``side="long"``: buy-доминанта
    (net_delta > 0) и цена растёт; для ``"short"``: sell-доминанта и цена падает.

    Условия: (1) |net_delta|/total ≥ ``min_delta_frac`` (сильная агрессия);
    (2) цена закрыла в сторону агрессии; (3) ≥1 крупный принт доминирующей
    стороны (если big_threshold известен). ``min_delta_frac`` 0.30 — нейтральный
    порог «сильной» агрессии (не тюнинг под P&L; reversible)."""
    if side not in ("long", "short") or not trades:
        return Initiative(False, side, reasons=["no_data"])
    buy_vol = sum(t.size for t in trades if t.side.upper() == "BUY")
    sell_vol = sum(t.size for t in trades if t.side.upper() == "SELL")
    total = buy_vol + sell_vol
    price_move = trades[-1].price - trades[0].price
    if total <= 0:
        return Initiative(False, side, reasons=["empty"])
    net = buy_vol - sell_vol
    delta_frac = abs(net) / total
    if side == "long":
        dominant, directional = "Buy", (net > 0 and price_move > 0)
    else:
        dominant, directional = "Sell", (net < 0 and price_move < 0)
    reasons: list[str] = []
    if delta_frac >= min_delta_frac:
        reasons.append(f"strong_delta={delta_frac:.0%}")
    if directional:
        reasons.append("price_in_direction")
    big_dominant = 0
    if big_threshold is not None:
        big_dominant = len(detect_big_trades(trades, big_threshold, side=dominant))
        if big_dominant >= 1:
            reasons.append(f"big_dominant={big_dominant}")
    cond_delta = delta_frac >= min_delta_frac
    cond_big = (big_dominant >= 1) if big_threshold is not None else True
    confirmed = bool(cond_delta and directional and cond_big)
    if not confirmed and not reasons:
        reasons.append("no_initiative")
    return Initiative(confirmed, side, net_delta=net, delta_frac=delta_frac,
                      price_move=price_move, big_dominant=big_dominant,
                      reasons=reasons)


@dataclass
class Exhaustion:
    """Результат проверки exhaustion (reversal-паттерн)."""
    confirmed: bool
    move_dir: str              # направление затухающего движения: up | down
    vol_decay: float = 0.0     # second_half_vol / first_half_vol (≤1 = затухание)
    contrarian_frac: float = 0.0  # доля контр-стороны в последней трети окна
    reasons: list[str] = field(default_factory=list)


def detect_exhaustion(trades: list[TradePrint], move_dir: str, *,
                      min_decay: float = 0.80,
                      min_contrarian_frac: float = 0.60,
                      min_samples: int = 18) -> Exhaustion:
    """Exhaustion — затухающий объём + contrarian imbalance на экстремуме (канон
    reversal). ``move_dir`` = направление движения, которое затухает:
    ``"up"`` (аптренд выдыхается → контр-сторона = продавцы в хвосте окна);
    ``"down"`` (даунтренд → контр-сторона = покупатели).

    Условия: (1) объём второй половины окна ≤ ``min_decay`` × первой (затухание);
    (2) в последней трети окна контр-сторона ≥ ``min_contrarian_frac`` (встречная
    агрессия на экстремуме). Пороги 0.80/0.60 — нейтральные (reversible)."""
    if move_dir not in ("up", "down") or len(trades) < max(6, min_samples):
        return Exhaustion(False, move_dir, reasons=["no_data"])
    mid = len(trades) // 2
    first_vol = sum(t.size for t in trades[:mid])
    second_vol = sum(t.size for t in trades[mid:])
    if first_vol <= 0:
        return Exhaustion(False, move_dir, reasons=["no_data"])
    decay = second_vol / first_vol
    third = max(len(trades) // 3, 1)
    tail = trades[len(trades) - third:]
    tb = sum(t.size for t in tail if t.side.upper() == "BUY")
    ts = sum(t.size for t in tail if t.side.upper() == "SELL")
    ttot = tb + ts
    if ttot <= 0:
        return Exhaustion(False, move_dir, reasons=["no_data"])
    if move_dir == "up":
        contrarian_frac = ts / ttot
    else:
        contrarian_frac = tb / ttot
    reasons: list[str] = []
    if decay <= min_decay:
        reasons.append(f"vol_decay={decay:.2f}")
    if contrarian_frac >= min_contrarian_frac:
        reasons.append(f"contrarian={contrarian_frac:.0%}")
    confirmed = bool(decay <= min_decay and contrarian_frac >= min_contrarian_frac)
    if not confirmed and not reasons:
        reasons.append("no_exhaustion")
    return Exhaustion(confirmed, move_dir, vol_decay=decay,
                      contrarian_frac=contrarian_frac, reasons=reasons)
