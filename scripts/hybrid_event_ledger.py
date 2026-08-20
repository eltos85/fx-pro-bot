"""Разбор конкретных сделок по эфиру: откуда взялся большой плюс.

Задача узкая: взять фактические закрытия, которые дали крупные плюсы
(#4266 +$618.83, #4288 +$393.95, #4276 +$130.76, #4220 +$40.61,
#4218 +$23.69), и показать по каждому — что за позиция была на бирже, кто её
набрал, по какой средней цене, кто и на каком уровне её закрыл, сколько денег
это дало и что купили обратно.

Никаких сравнений с другими стратегиями и никаких оценок «лучше/хуже». Только
разбор события.

Скрипт read-only и самодостаточный: запускается передачей в контейнер, где
соседних файлов репозитория нет.

    ssh root@204.168.149.140 "docker exec -i fx-pro-bot-scalp-bot-1 python3 - \
      --days 30" < scripts/hybrid_event_ledger.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# Окно запроса ограничено 7 днями, обходим окнами.
# https://bybit-exchange.github.io/docs/v5/order/execution
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
THROTTLE_SEC = 0.2

# У Funding-записей execQty равен размеру позиции — в набор лота они не идут.
# https://bybit-exchange.github.io/docs/v5/enum#exectype
TRADE_EXEC_TYPES = {"Trade", "AdlTrade", "BustTrade", "Delivery"}


def _ms(ms) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _f(raw, default=0.0) -> float:
    try:
        if raw in (None, ""):
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def who(fill: dict) -> str:
    """Кто сделал сделку. Метка владельца — orderLinkId."""
    link = fill.get("orderLinkId") or ""
    if link.startswith("scalp_"):
        return "скальп"
    if link.startswith("swing_"):
        return "свинг"
    if link.startswith("daytrend_"):
        return "дейтренд"
    sot = fill.get("stopOrderType") or ""
    if sot == "StopLoss":
        return "стоп скальпа"
    if sot == "TakeProfit":
        return "тейк скальпа"
    if sot and sot != "UNKNOWN":
        return f"биржевой {sot}"
    return "неизвестно"


def paginate(method, *, start_ms: int, end_ms: int, **params) -> list[dict]:
    rows: list[dict] = []
    win = start_ms
    while win < end_ms:
        win_end = min(win + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            resp = method(startTime=win, endTime=win_end, limit=100,
                          cursor=cursor, **params)
            res = resp.get("result") or {}
            rows.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor") or ""
            time.sleep(THROTTLE_SEC)
            if not cursor:
                break
        win = win_end
    return rows


def build_events(fills: list[dict]) -> list[dict]:
    """Проходит сделки по порядку и собирает события полного закрытия лота.

    Позиция на бирже одна, поэтому лот считается общим. Для каждой покупки
    запоминается, кто её сделал, — чтобы потом сказать, чей объём закрыли.
    """
    size = 0.0
    avg = 0.0
    legs: list[dict] = []       # покупки, из которых собран текущий лот
    opened_ts: int | None = None
    events: list[dict] = []

    for f in sorted(fills, key=lambda r: int(r["execTime"])):
        if (f.get("execType") or "Trade") not in TRADE_EXEC_TYPES:
            continue
        qty = _f(f.get("execQty"))
        px = _f(f.get("execPrice"))
        ts = int(f["execTime"])
        if qty <= 0 or px <= 0:
            continue
        buy = f.get("side") == "Buy"
        signed = qty if buy else -qty
        opening = size == 0.0 or (size > 0) == (signed > 0)

        if opening:
            if size == 0.0:
                avg = px
                opened_ts = ts
            else:
                avg = (avg * abs(size) + px * qty) / (abs(size) + qty)
            size += signed
            legs.append({"ts": ts, "qty": qty, "px": px, "who": who(f)})
            continue

        closing = min(qty, abs(size))
        sign = 1.0 if size > 0 else -1.0
        pnl = (px - avg) * closing * sign
        full = closing >= abs(size) - 1e-9

        events.append({
            "ts": ts, "closer": who(f), "closed_qty": closing,
            "close_px": px, "avg_px": avg, "pnl": pnl, "full": full,
            "pos_qty": abs(size), "legs": list(legs), "long": size > 0,
            "held_h": (ts - opened_ts) / 3_600_000 if opened_ts else 0.0,
        })

        size -= closing * sign
        if qty > closing:
            size = (qty - closing) * (1.0 if signed > 0 else -1.0)
            avg = px
            legs = [{"ts": ts, "qty": qty - closing, "px": px, "who": who(f)}]
            opened_ts = ts
        elif abs(size) < 1e-12:
            size, avg, legs, opened_ts = 0.0, 0.0, [], None
        else:
            # частичное закрытие: уменьшаем ноги пропорционально
            k = abs(size) / (abs(size) + closing)
            for leg in legs:
                leg["qty"] *= k

    # Чем закрыли — тем и зашли снова: ищем первый вход после события.
    entries = [(int(f["execTime"]), _f(f.get("execPrice")),
                _f(f.get("execQty")), who(f), f.get("side"))
               for f in sorted(fills, key=lambda r: int(r["execTime"]))
               if (f.get("execType") or "Trade") in TRADE_EXEC_TYPES]
    for ev in events:
        need = "Buy" if ev["long"] else "Sell"
        nxt = next((e for e in entries
                    if e[0] > ev["ts"] and e[4] == need), None)
        ev["next_entry"] = nxt
    return events


def in_our_favor(ev: dict) -> float:
    """Насколько цена закрытия ушла в нашу сторону от среднего входа, %.

    Для покупки это рост, для продажи в шорт — падение, поэтому знак
    переворачивается: иначе выгодное закрытие шорта выглядит как убыток.
    """
    if not ev["avg_px"]:
        return 0.0
    raw = (ev["close_px"] / ev["avg_px"] - 1) * 100
    return raw if ev["long"] else -raw


def print_event(i: int, ev: dict) -> None:
    when = _ms(ev["ts"])
    kind = "закрыт весь лот" if ev["full"] else "закрыта часть лота"
    print(f"\n─── событие {i}: {when:%d.%m %H:%M} UTC — {kind} ───")

    side = "покупка" if ev["long"] else "продажа в шорт"
    print(f"  Что было на бирже: {side}, {ev['pos_qty']:.3f} ETH, "
          f"средняя цена входа {ev['avg_px']:.2f}")
    print("  Из чего собран этот объём:")
    by_who: dict[str, list[dict]] = {}
    for leg in ev["legs"]:
        by_who.setdefault(leg["who"], []).append(leg)
    sign = 1.0 if ev["long"] else -1.0
    for name, group in by_who.items():
        vol = sum(g["qty"] for g in group)
        cost = sum(g["qty"] * g["px"] for g in group)
        share = 100 * vol / ev["pos_qty"] if ev["pos_qty"] else 0.0
        alone = (ev["close_px"] - cost / vol) * vol * sign
        print(f"    {name:<14} {vol:.3f} ETH по средней {cost/vol:.2f} "
              f"({share:.0f}% лота) — сам по себе дал бы {alone:+,.2f} $")

    fav = in_our_favor(ev)
    print(f"  Кто закрыл: {ev['closer']} по цене {ev['close_px']:.2f}")
    print(f"  Это на {fav:+.2f}% в нашу сторону от среднего входа "
          f"(держали {ev['held_h']:.1f} ч)")
    print(f"  Деньги: {ev['pnl']:+,.2f} $")

    nxt = ev["next_entry"]
    if nxt:
        gap_min = (nxt[0] - ev["ts"]) / 60000
        back = (nxt[1] / ev["close_px"] - 1) * 100 if ev["close_px"] else 0.0
        print(f"  Зашли снова через {gap_min:.0f} мин: {nxt[2]:.3f} ETH "
              f"по {nxt[1]:.2f} ({back:+.2f}% к цене закрытия), "
              f"вход сделал — {nxt[3]}")
    else:
        print("  Обратно пока не заходили")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--min-pnl", type=float, default=0.0,
                    help="показывать только события крупнее этой суммы")
    args = ap.parse_args()

    key = os.environ.get("SCALP_BYBIT_API_KEY", "")
    secret = os.environ.get("SCALP_BYBIT_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("нужны SCALP_BYBIT_API_KEY / SCALP_BYBIT_API_SECRET")
    demo = os.environ.get("SCALP_BYBIT_DEMO", "true").lower() in (
        "1", "true", "yes")

    from pybit.unified_trading import HTTP
    sess = HTTP(demo=demo, api_key=key, api_secret=secret, recv_window=20000)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)

    fills = paginate(sess.get_executions, start_ms=start_ms, end_ms=end_ms,
                     category="linear", symbol=args.symbol)
    print(f"Разбор {args.symbol}: {_ms(start_ms):%d.%m} → {now:%d.%m} UTC, "
          f"сделок в выгрузке {len(fills)}")

    events = build_events(fills)
    big = [e for e in events if abs(e["pnl"]) >= args.min_pnl]
    print(f"Событий закрытия: {len(events)}, из них показываю {len(big)}")

    for i, ev in enumerate(big, 1):
        print_event(i, ev)

    if not big:
        return 0

    print("\n" + "=" * 78)
    plus = [e for e in big if e["pnl"] > 0]
    full = [e for e in big if e["full"]]
    print(f"Всего событий: {len(big)}, из них в плюс {len(plus)}, "
          f"закрытий всего лота {len(full)}")
    print(f"Сумма денег по событиям: {sum(e['pnl'] for e in big):+,.2f} $")
    if plus:
        dists = sorted(in_our_favor(e) for e in plus)
        mid = dists[len(dists) // 2]
        print(f"Плюсовые события случались на {min(dists):+.2f}%…"
              f"{max(dists):+.2f}% в нашу сторону от среднего входа, "
              f"серединное значение {mid:+.2f}%")
        holds = sorted(e["held_h"] for e in plus)
        print(f"Держали до закрытия от {holds[0]:.1f} до {holds[-1]:.1f} ч, "
              f"серединное {holds[len(holds)//2]:.1f} ч")
        sizes = sorted(e["pos_qty"] for e in plus)
        print(f"Размер лота в плюсовых событиях: от {sizes[0]:.2f} до "
              f"{sizes[-1]:.2f} ETH")
    closers: dict[str, int] = {}
    for e in big:
        closers[e["closer"]] = closers.get(e["closer"], 0) + 1
    print("Кто закрывал: " + ", ".join(f"{k} — {v}" for k, v in
                                       sorted(closers.items(),
                                              key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
