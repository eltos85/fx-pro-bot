"""Измеритель контура H-HYBRID: P&L по ОДНОЙ книге символа (read-only).

Шаг 0 плана из STRATEGY_HYBRID.md. Скрипт ничего не меняет: только читает
Bybit API. Локальные SQLite БД не трогает вообще — двойной счёт «скальп +
horizon» и был исходной проблемой учёта.

Зачем именно биржа, а не БД (stats-collection.mdc):
  * `closedPnl` из /v5/position/closed-pnl — net, ground truth;
  * `avgEntryPrice` там же = средний вход ЗАКРЫТОГО объёма, то есть якорь
    смешанной позиции. Реконструировать якорь из двух БД не нужно;
  * `execFee` из /v5/execution/list даёт комиссию по каждой ноге — прямой
    замер «налога на 17 ног» из бенчмарка 2026-08-20.

Атрибуция ног по orderLinkId (shared account, one-way):
  scalp_*    — тактика (скальп)
  swing_*    — ядро (swing-bot)
  daytrend_* — ядро (daytrend-bot)
  пусто + stopOrderType — биржевой брекет (set_trading_stop, tpslMode=Full)

Docs:
  https://bybit-exchange.github.io/docs/v5/order/execution
  https://bybit-exchange.github.io/docs/v5/position/close-pnl
  Оба эндпоинта: окно endTime-startTime <= 7 дней, limit [1,100], cursor.

Запуск (контейнер скальпа — там pybit и SCALP_BYBIT_* ключи):
    ssh root@204.168.149.140 \
      "docker exec -i fx-pro-bot-scalp-bot-1 python3 - --days 3 --snapshot" \
      < scripts/hybrid_contour_pnl.py

Локально:
    SCALP_BYBIT_API_KEY=... SCALP_BYBIT_API_SECRET=... \
      python3 scripts/hybrid_contour_pnl.py --days 3
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone

from pybit.unified_trading import HTTP

SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000

# Bybit linear non-VIP taker. Ядро horizon и брекеты скальпа — всегда taker.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
TAKER_FEE = 0.00055

# Консервативный клиентский троттлинг: 5 req/s, ниже лимитов API.
# https://bybit-exchange.github.io/docs/v5/rate-limit
THROTTLE_SEC = 0.2

# execType: у Funding-записей execQty равен размеру позиции, а execFee — это
# funding payment. Если не отделить их, книга и комиссии считаются неверно.
# https://bybit-exchange.github.io/docs/v5/enum#exectype
TRADE_EXEC_TYPES = {"Trade", "AdlTrade", "BustTrade", "Delivery"}
FUNDING_EXEC_TYPES = {"Funding"}

ACTOR_CORE = "core"
ACTOR_TACTIC = "tactic"
ACTOR_BRACKET = "bracket"
ACTOR_UNKNOWN = "unknown"


def _ms_to_utc(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _fnum(raw: object, default: float = 0.0) -> float:
    try:
        if raw in (None, ""):
            return default
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _actor(fill: dict) -> tuple[str, str]:
    """Кто инициировал ногу. Возвращает (класс, детализация)."""
    link = fill.get("orderLinkId") or ""
    if link.startswith("scalp_"):
        return ACTOR_TACTIC, link
    if link.startswith("swing_"):
        return ACTOR_CORE, "swing"
    if link.startswith("daytrend_"):
        return ACTOR_CORE, "daytrend"
    sot = fill.get("stopOrderType") or ""
    if sot and sot != "UNKNOWN":
        return ACTOR_BRACKET, sot
    return ACTOR_UNKNOWN, link or "-"


def _paginate(method, *, start_ms: int, end_ms: int, **params) -> list[dict]:
    """Полный обход с cursor по окнам <= 7 дней (требование обоих эндпоинтов)."""
    rows: list[dict] = []
    win_start = start_ms
    while win_start < end_ms:
        win_end = min(win_start + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            resp = method(
                startTime=win_start, endTime=win_end,
                limit=100, cursor=cursor, **params,
            )
            if resp.get("retCode") != 0:
                raise RuntimeError(f"Bybit: {resp.get('retMsg')}")
            result = resp.get("result") or {}
            rows.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            time.sleep(THROTTLE_SEC)
            if not cursor:
                break
        win_start = win_end
    return rows


def _replay_book(fills: list[dict]) -> tuple[float, float, float]:
    """Проигрывает книгу one-way по avg-cost. -> (realized_gross, size, avg)."""
    size = 0.0  # >0 long, <0 short
    avg = 0.0
    realized = 0.0
    for f in sorted(fills, key=lambda r: int(r["execTime"])):
        qty = _fnum(f.get("execQty"))
        px = _fnum(f.get("execPrice"))
        if qty <= 0 or px <= 0:
            continue
        signed = qty if f.get("side") == "Buy" else -qty
        if size == 0.0:
            size, avg = signed, px
            continue
        if (size > 0) == (signed > 0):
            total = abs(size) + qty
            avg = (avg * abs(size) + px * qty) / total
            size += signed
            continue
        closing = min(qty, abs(size))
        realized += (px - avg) * closing * (1.0 if size > 0 else -1.0)
        size += closing * (1.0 if signed > 0 else -1.0)
        if qty > closing:  # флип через ноль
            size = (qty - closing) * (1.0 if signed > 0 else -1.0)
            avg = px
        elif abs(size) < 1e-12:
            size, avg = 0.0, 0.0
    return realized, size, avg


def _report_symbol(sess: HTTP, symbol: str, category: str,
                   start_ms: int, end_ms: int) -> dict:
    raw = _paginate(sess.get_executions, start_ms=start_ms, end_ms=end_ms,
                    category=category, symbol=symbol)
    fills = [f for f in raw if f.get("execType") in TRADE_EXEC_TYPES]
    fundings = [f for f in raw if f.get("execType") in FUNDING_EXEC_TYPES]
    other = [f for f in raw if f.get("execType") not in TRADE_EXEC_TYPES
             and f.get("execType") not in FUNDING_EXEC_TYPES]
    closes = _paginate(sess.get_closed_pnl, start_ms=start_ms, end_ms=end_ms,
                       category=category, symbol=symbol)
    pos_resp = sess.get_positions(category=category, symbol=symbol)
    time.sleep(THROTTLE_SEC)
    tick_resp = sess.get_tickers(category=category, symbol=symbol)
    time.sleep(THROTTLE_SEC)

    pos = ((pos_resp.get("result") or {}).get("list") or [{}])[0]
    tick = ((tick_resp.get("result") or {}).get("list") or [{}])[0]
    pos_size = _fnum(pos.get("size"))
    pos_avg = _fnum(pos.get("avgPrice"))
    unreal = _fnum(pos.get("unrealisedPnl"))
    mark = _fnum(pos.get("markPrice")) or _fnum(tick.get("markPrice")) \
        or _fnum(tick.get("lastPrice"))

    print(f"\n{'=' * 78}\n  {symbol}   окно "
          f"{_ms_to_utc(start_ms):%Y-%m-%d %H:%M} → "
          f"{_ms_to_utc(end_ms):%Y-%m-%d %H:%M} UTC\n{'=' * 78}")

    # ── Реализации: якорь (avgEntryPrice) против лока (avgExitPrice) ──────
    realized_net = sum(_fnum(c.get("closedPnl")) for c in closes)
    fees_in_closes = sum(_fnum(c.get("openFee")) + _fnum(c.get("closeFee"))
                         for c in closes)
    link_by_order = {f.get("orderId"): _actor(f) for f in fills}

    if closes:
        print("\n  Реализации (closedPnl = net; якорь = avgEntryPrice биржи)")
        print(f"  {'время UTC':<17} {'кто закрыл':<22} {'side':<5} "
              f"{'объём':>9} {'якорь':>10} {'лок':>10} "
              f"{'лок−якорь':>10} {'net$':>10}")
        for c in sorted(closes, key=lambda r: int(r["createdTime"])):
            anchor = _fnum(c.get("avgEntryPrice"))
            lock = _fnum(c.get("avgExitPrice"))
            drift = (lock / anchor - 1.0) * 100.0 if anchor else 0.0
            cls, det = link_by_order.get(c.get("orderId"), (ACTOR_UNKNOWN, "-"))
            who = f"{cls}:{det}"[:22]
            print(f"  {_ms_to_utc(c['createdTime']):%Y-%m-%d %H:%M} "
                  f"{who:<22} {c.get('side', ''):<5} "
                  f"{_fnum(c.get('closedSize')):>9.4f} {anchor:>10.2f} "
                  f"{lock:>10.2f} {drift:>9.3f}% "
                  f"{_fnum(c.get('closedPnl')):>10.2f}")
        print(f"  {'ИТОГО реализовано (net)':<58} "
              f"{realized_net:>19.2f}")
        print(f"  {'в т.ч. комиссии внутри closedPnl':<58} "
              f"{-fees_in_closes:>19.2f}")
    else:
        print("\n  Реализаций в окне нет.")

    # ── Ноги: сколько их и сколько стоят ─────────────────────────────────
    by_actor: dict[str, dict] = {}
    for f in fills:
        cls, det = _actor(f)
        key = f"{cls}:{det}" if cls != ACTOR_TACTIC else cls
        agg = by_actor.setdefault(key, {"n": 0, "val": 0.0, "fee": 0.0,
                                        "maker": 0})
        agg["n"] += 1
        agg["val"] += _fnum(f.get("execValue"))
        agg["fee"] += _fnum(f.get("execFee"))
        agg["maker"] += 1 if f.get("isMaker") else 0

    fees_legs = sum(a["fee"] for a in by_actor.values())
    if by_actor:
        print(f"\n  Ноги исполнений: {len(fills)}, комиссия ${fees_legs:.2f}")
        print(f"  {'кто':<26} {'ног':>5} {'maker':>6} {'оборот$':>12} "
              f"{'комиссия$':>10}")
        for key in sorted(by_actor, key=lambda k: -by_actor[k]["fee"]):
            a = by_actor[key]
            print(f"  {key:<26} {a['n']:>5} {a['maker']:>6} "
                  f"{a['val']:>12.2f} {a['fee']:>10.2f}")
    if other:
        kinds = sorted({f.get("execType") or "?" for f in other})
        print(f"  прочие execType (в книгу не идут): {', '.join(kinds)}")

    # ── Funding: плата за удержание инвентаря ────────────────────────────
    funding_paid = sum(_fnum(f.get("execFee")) for f in fundings)
    if fundings:
        print(f"\n  Funding: {len(fundings)} начислений, "
              f"заплачено ${funding_paid:.2f} "
              f"(>0 = списано с нас)")

    # ── Остаток и итог контура ───────────────────────────────────────────
    print(f"\n  Открытый остаток: {pos_size:.4f} @ {pos_avg:.2f}, "
          f"mark {mark:.2f}, unrealized ${unreal:.2f}")
    contour_total = realized_net + unreal - funding_paid
    print(f"  КОНТУР ИТОГО (realized net + unrealized − funding): "
          f"${contour_total:.2f}")

    # ── Бенчмарк: держать первый лот ядра из окна ────────────────────────
    core_entries = [f for f in fills
                    if _actor(f)[0] == ACTOR_CORE and f.get("side") == "Buy"]
    hold_total = None
    hold_qty = hold_px = 0.0
    if core_entries and mark:
        first = min(core_entries, key=lambda r: int(r["execTime"]))
        hold_ts = int(first["execTime"])
        hold_qty = _fnum(first.get("execQty"))
        hold_px = _fnum(first.get("execPrice"))
        hold_gross = (mark - hold_px) * hold_qty
        hold_fee = hold_px * hold_qty * TAKER_FEE
        # Холд платит funding на ПОЛНЫЙ лот всё время. Ставку восстанавливаем
        # из фактических начислений: rate = execFee / execValue.
        hold_funding = 0.0
        for f in fundings:
            if int(f["execTime"]) < hold_ts:
                continue
            value = _fnum(f.get("execValue"))
            if value <= 0:
                continue
            rate = _fnum(f.get("execFee")) / value
            px = _fnum(f.get("markPrice")) or _fnum(f.get("execPrice")) or mark
            hold_funding += rate * hold_qty * px
        hold_total = hold_gross - hold_fee - hold_funding
        print(f"\n  Бенчмарк «просто держать ядро»: {hold_qty:.4f} @ "
              f"{hold_px:.2f} от {_ms_to_utc(hold_ts):%m-%d %H:%M}")
        print(f"    gross ${hold_gross:.2f} − вход-комиссия ${hold_fee:.2f} "
              f"− funding ${hold_funding:.2f} = ${hold_total:.2f} (1 нога)")
        delta = contour_total - hold_total
        verdict = "контур ВПЕРЕДИ" if delta > 0 else "контур ОТСТАЁТ"
        print(f"    Δ контур − холд = ${delta:.2f}  → {verdict}")
        print(f"    (у обоих остаток открыт; выходная комиссия не учтена "
              f"ни там, ни тут)")

    # ── Sanity: книга против API ─────────────────────────────────────────
    book_gross, book_size, book_avg = _replay_book(fills)
    api_gross = realized_net + fees_in_closes
    print(f"\n  Сверка: книга по ногам gross ${book_gross:.2f} vs "
          f"API gross ${api_gross:.2f} "
          f"(расх. ${book_gross - api_gross:+.2f})")
    print(f"  Книга: остаток {book_size:.4f} @ {book_avg:.2f} "
          f"vs биржа {pos_size:.4f} @ {pos_avg:.2f}")
    if closes and abs(book_gross - api_gross) > max(1.0,
                                                    abs(api_gross) * 0.02):
        print("  ⚠ расхождение >2%: в окне есть реализации от позиции, "
              "открытой ДО начала окна (или funding/settle). "
              "Расширь --days.")

    # ── Разбивка по дням: для наблюдения тенденции ────────────────────────
    if closes:
        per_day: dict[str, dict] = {}
        for c in closes:
            day = f"{_ms_to_utc(c['createdTime']):%Y-%m-%d}"
            agg = per_day.setdefault(day, {"n": 0, "net": 0.0})
            agg["n"] += 1
            agg["net"] += _fnum(c.get("closedPnl"))
        print("\n  По дням (реализации):")
        for day in sorted(per_day):
            print(f"    {day}  n={per_day[day]['n']:<3} "
                  f"net ${per_day[day]['net']:>10.2f}")

    return {
        "symbol": symbol,
        "realized_net": round(realized_net, 4),
        "fees_legs": round(fees_legs, 4),
        "funding_paid": round(funding_paid, 4),
        "n_legs": len(fills),
        "n_closes": len(closes),
        "pos_size": pos_size,
        "pos_avg": pos_avg,
        "mark": mark,
        "unrealized": unreal,
        "contour_total": round(contour_total, 4),
        "hold_qty": hold_qty,
        "hold_entry": hold_px,
        "hold_total": round(hold_total, 4) if hold_total is not None else "",
        "delta_vs_hold": (round(contour_total - hold_total, 4)
                          if hold_total is not None else ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="ETHUSDT,BTCUSDT")
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--category", default="linear")
    ap.add_argument("--snapshot", nargs="?", const="/data/hybrid_contour.csv",
                    default=None,
                    help="дописать строку-снапшот в CSV для наблюдения тренда")
    args = ap.parse_args()

    key = os.environ.get("SCALP_BYBIT_API_KEY", "")
    secret = os.environ.get("SCALP_BYBIT_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("нужны SCALP_BYBIT_API_KEY / SCALP_BYBIT_API_SECRET")
    demo = os.environ.get("SCALP_BYBIT_DEMO", "true").lower() in (
        "1", "true", "yes")

    sess = HTTP(demo=demo, api_key=key, api_secret=secret, recv_window=20000)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)

    print(f"H-HYBRID contour meter | demo={demo} | "
          f"источник: Bybit API (closed-pnl + execution/list + positions)")

    rows = []
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        rows.append(_report_symbol(sess, symbol, args.category,
                                   start_ms, end_ms))

    total = sum(r["contour_total"] for r in rows)
    print(f"\n{'=' * 78}\n  Сумма контуров по символам: ${total:.2f}\n"
          f"{'=' * 78}")

    if args.snapshot:
        fields = ["ts_utc", "window_days"] + list(rows[0].keys())
        exists = os.path.exists(args.snapshot)
        with open(args.snapshot, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow({"ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "window_days": args.days, **r})
        print(f"снапшот дописан: {args.snapshot}")


if __name__ == "__main__":
    main()
