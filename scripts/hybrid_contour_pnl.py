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

# Схема CSV-накопителя фиксирована явно, а не выводится из порядка ключей dict:
# `csv.DictWriter` пишет значения в порядке `fieldnames` и не проверяет, какой
# заголовок уже стоит в файле. Любая правка набора метрик иначе тихо сдвинула бы
# колонки в уже накопленном ряду.
SNAPSHOT_FIELDS = [
    "ts_utc", "window_days", "symbol",
    "realized_net", "fees_legs", "funding_paid", "n_legs", "n_closes",
    "pos_size", "pos_avg", "mark", "unrealized", "contour_total",
    "hold_qty", "hold_entry", "hold_total", "delta_vs_hold",
    "core_gross", "tactic_gross", "forced_core_pnl", "forced_core_n",
    "core_time_share", "mixed_time_share", "reentry_cost", "reentry_n",
    "drift_above_share", "cushion_pct",
    "book_aligned", "book_skipped", "book_gross_delta",
]


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


def _replay_book(fills: list[dict]) -> dict:
    """Проигрывает книгу one-way по avg-cost (как считает биржа) и попутно
    разлагает лот на «ядро» и «тактику» по атрибуции ног.

    Зачем: биржа держит ОДИН лот, поэтому вопрос «сколько из P&L — ход ядра, а
    сколько геометрия тактики» напрямую из API не читается. Состав лота
    восстанавливается по orderLinkId входов, а закрываемый объём делится
    **pro-rata** доле ядра и тактики в лоте на момент закрытия. Pro-rata, а не
    FIFO, потому что биржа использует avg-cost и порядок лотов не наблюдаем;
    любая FIFO-модель добавила бы произвольное допущение.
    """
    size = 0.0  # >0 long, <0 short
    avg = 0.0
    core_qty = 0.0  # часть лота, пришедшая от horizon
    tac_qty = 0.0  # часть лота от скальпа
    realized = realized_core = realized_tac = 0.0
    forced_core_pnl = forced_core_n = 0.0
    volunt_core_pnl = volunt_core_n = 0.0
    mixed_sec = core_sec = 0.0
    prev_ts: int | None = None
    core_entries: list[tuple[int, float, float]] = []
    core_exits: list[tuple[int, float]] = []
    reentries: list[dict] = []
    last_core_flat: tuple[int, float] | None = None

    for f in sorted(fills, key=lambda r: int(r["execTime"])):
        qty = _fnum(f.get("execQty"))
        px = _fnum(f.get("execPrice"))
        ts = int(f["execTime"])
        if qty <= 0 or px <= 0:
            continue
        if prev_ts is not None:
            dt = (ts - prev_ts) / 1000.0
            if core_qty > 1e-12:
                core_sec += dt
                if tac_qty > 1e-12:
                    mixed_sec += dt
        prev_ts = ts

        cls = _actor(f)[0]
        is_core = cls == ACTOR_CORE
        signed = qty if f.get("side") == "Buy" else -qty
        opening = size == 0.0 or (size > 0) == (signed > 0)

        if opening:
            if size == 0.0:
                avg = px
            else:
                avg = (avg * abs(size) + px * qty) / (abs(size) + qty)
            size += signed
            if is_core:
                core_qty += qty
                core_entries.append((ts, qty, px))
                if last_core_flat is not None:
                    exit_ts, exit_px = last_core_flat
                    reentries.append({
                        "gap_pct": (px / exit_px - 1.0) * 100.0,
                        "gap_usd": (exit_px - px) * qty,
                        "pause_min": (ts - exit_ts) / 60000.0,
                    })
                    last_core_flat = None
            else:
                tac_qty += qty
            continue

        closing = min(qty, abs(size))
        sign = 1.0 if size > 0 else -1.0
        pnl = (px - avg) * closing * sign
        total_qty = core_qty + tac_qty
        core_part = closing * (core_qty / total_qty) if total_qty > 1e-12 else 0.0
        pnl_core = (px - avg) * core_part * sign
        realized += pnl
        realized_core += pnl_core
        realized_tac += pnl - pnl_core
        if core_part > 1e-12:
            if is_core:
                volunt_core_pnl += pnl_core
                volunt_core_n += 1
            else:
                forced_core_pnl += pnl_core
                forced_core_n += 1
        core_qty = max(0.0, core_qty - core_part)
        tac_qty = max(0.0, tac_qty - (closing - core_part))
        if core_qty <= 1e-9 and core_part > 1e-12:
            core_exits.append((ts, px))
            last_core_flat = (ts, px)
        size -= closing * sign
        if qty > closing:  # флип через ноль
            size = (qty - closing) * (1.0 if signed > 0 else -1.0)
            avg = px
            if is_core:
                core_qty = qty - closing
            else:
                tac_qty = qty - closing
        elif abs(size) < 1e-12:
            size, avg = 0.0, 0.0

    # Знаменатель времени — от ПЕРВОГО входа ядра, а не от начала окна:
    # иначе в «ядро вне рынка» попадает период, когда ядра ещё не существовало,
    # и доля занижается на величину, зависящую от --days.
    span = 0.0
    if fills:
        ts_all = [int(f["execTime"]) for f in fills]
        base = core_entries[0][0] if core_entries else min(ts_all)
        span = max(0.0, (max(ts_all) - base) / 1000.0)

    return {
        "realized": realized, "size": size, "avg": avg,
        "realized_core": realized_core, "realized_tac": realized_tac,
        "forced_core_pnl": forced_core_pnl, "forced_core_n": forced_core_n,
        "volunt_core_pnl": volunt_core_pnl, "volunt_core_n": volunt_core_n,
        "core_sec": core_sec, "mixed_sec": mixed_sec, "span_sec": span,
        "core_entries": core_entries, "core_exits": core_exits,
        "reentries": reentries, "core_qty": core_qty, "tac_qty": tac_qty,
    }


def _replay_aligned(fills: list[dict], signed_pos: float,
                    pos_avg: float) -> dict:
    """Реплей с выравниванием старта по фактической позиции на бирже.

    Окно API плавающее, поэтому первой ногой легко оказывается ЗАКРЫТИЕ
    позиции, открытой раньше начала окна. Тогда реплей с нуля открывает
    фантомную позицию в обратную сторону и все производные метрики врут.
    Ищем первую ногу, начиная с которой книга сходится с биржей по остатку и
    средней цене; всё, что раньше, — хвост прошлой позиции.
    """
    ordered = sorted(fills, key=lambda r: int(r["execTime"]))
    for i in range(len(ordered)):
        book = _replay_book(ordered[i:])
        size_ok = abs(book["size"] - signed_pos) <= max(1e-6,
                                                        abs(signed_pos) * 1e-4)
        avg_ok = pos_avg <= 0 or abs(book["avg"] - pos_avg) <= pos_avg * 5e-4
        if size_ok and avg_ok:
            book["skipped"] = i
            book["aligned"] = True
            return book
    book = _replay_book(ordered)
    book["skipped"] = 0
    book["aligned"] = False
    return book


def _mae_mfe(sess: HTTP, symbol: str, category: str, since_ms: int,
             anchor: float) -> tuple[float, float] | None:
    """Худший и лучший ход от якоря с момента `since_ms` (15m свечи).

    Закрывает слепую зону «как выглядел риск»: contour_total показывает только
    итог, а не просадку, которую позиция прошла по пути.
    https://bybit-exchange.github.io/docs/v5/market/kline (limit <= 1000)
    """
    if anchor <= 0:
        return None
    resp = sess.get_kline(category=category, symbol=symbol, interval="15",
                          start=since_ms, limit=1000)
    time.sleep(THROTTLE_SEC)
    rows = ((resp.get("result") or {}).get("list") or [])
    if not rows:
        return None
    highs = [_fnum(r[2]) for r in rows]
    lows = [_fnum(r[3]) for r in rows if _fnum(r[3]) > 0]
    if not highs or not lows:
        return None
    return ((min(lows) / anchor - 1.0) * 100.0,
            (max(highs) / anchor - 1.0) * 100.0)


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

    # ── Асимметрия по якорю: центральное утверждение гипотезы ────────────
    drifts = [(_fnum(c.get("avgExitPrice")) / _fnum(c.get("avgEntryPrice")) - 1.0)
              * 100.0
              for c in closes if _fnum(c.get("avgEntryPrice")) > 0]
    if drifts:
        above = [d for d in drifts if d > 0]
        pnl_above = sum(_fnum(c.get("closedPnl")) for c in closes
                        if _fnum(c.get("avgEntryPrice")) > 0
                        and _fnum(c.get("avgExitPrice"))
                        > _fnum(c.get("avgEntryPrice")))
        srt = sorted(drifts)
        med = srt[len(srt) // 2]
        print(f"\n  Асимметрия (лок−якорь): выше якоря {len(above)}/"
              f"{len(drifts)} = {100.0 * len(above) / len(drifts):.0f}%, "
              f"медиана {med:+.3f}%, разброс "
              f"{min(drifts):+.3f}%…{max(drifts):+.3f}%")
        if realized_net:
            print(f"    P&L фиксаций выше якоря ${pnl_above:.2f} из "
                  f"${realized_net:.2f} "
                  f"({100.0 * pnl_above / realized_net:.0f}%)")

    # ── Декомпозиция лота: ядро против тактики ───────────────────────────
    signed_pos = pos_size if pos.get("side") != "Sell" else -pos_size
    book = _replay_aligned(fills, signed_pos, pos_avg)
    if not book["aligned"]:
        print("\n  ⚠ книга не сошлась с биржей ни с одного старта: метрики "
              "ниже недостоверны, расширь --days")
    elif book["skipped"]:
        print(f"\n  Старт книги выровнен: отброшено {book['skipped']} ног — "
              f"хвост позиции, открытой до начала окна")
    dec_total = book["realized_core"] + book["realized_tac"]
    if abs(dec_total) > 1e-9:
        share = 100.0 * book["realized_core"] / dec_total
        print(f"\n  Декомпозиция реализованного gross (pro-rata по составу "
              f"лота):")
        print(f"    ядро    ${book['realized_core']:>10.2f}  ({share:.0f}%)")
        print(f"    тактика ${book['realized_tac']:>10.2f}  "
              f"({100.0 - share:.0f}%)")
    if book["forced_core_n"] or book["volunt_core_n"]:
        tot_n = book["forced_core_n"] + book["volunt_core_n"]
        print(f"    из ядра закрыто ПРИНУДИТЕЛЬНО (не horizon): "
              f"{book['forced_core_n']:.0f}/{tot_n:.0f} событий, "
              f"${book['forced_core_pnl']:.2f}; "
              f"своим решением {book['volunt_core_n']:.0f}, "
              f"${book['volunt_core_pnl']:.2f}")
    if book["span_sec"] > 0:
        print(f"    с первого входа ядра ({book['span_sec'] / 3600:.1f}ч): "
              f"ядро в рынке "
              f"{100.0 * book['core_sec'] / book['span_sec']:.0f}%, "
              f"лот смешан с тактикой "
              f"{100.0 * book['mixed_sec'] / book['span_sec']:.0f}%")

    # ── Стоимость перезаходов ────────────────────────────────────────────
    if book["reentries"]:
        gaps = [r["gap_pct"] for r in book["reentries"]]
        cost = sum(r["gap_usd"] for r in book["reentries"])
        gaps_str = " / ".join("%+.3f%%" % g for g in gaps)
        pause_str = " / ".join("%.0fм" % r["pause_min"]
                               for r in book["reentries"])
        print(f"\n  Перезаходы ядра: {len(gaps)}, гэп лок→вход {gaps_str}")
        print(f"    суммарно ${cost:+.2f} (>0 = перезашли дешевле, "
              f"<0 = дороже), пауза {pause_str}")

    # ── Sizing drag: лот ядра в тренде ───────────────────────────────────
    if len(book["core_entries"]) >= 2:
        q_first = book["core_entries"][0][1]
        q_last = book["core_entries"][-1][1]
        drag = (q_last / q_first - 1.0) * 100.0 if q_first else 0.0
        lost = (mark - book["core_entries"][-1][2]) * (q_first - q_last)
        print(f"\n  Sizing drag: первый вход ядра {q_first:.4f} → последний "
              f"{q_last:.4f} ({drag:+.1f}%)")
        print(f"    недобрано на текущем ходе ${lost:.2f} "
              f"(15% equity делится на выросшую цену)")

    # ── Риск открытого остатка: чего мы не видим в итоговом P&L ──────────
    if pos_size > 0 and pos_avg > 0:
        cushion = (mark / pos_avg - 1.0) * 100.0
        print(f"\n  Риск остатка: подушка {cushion:+.2f}% "
              f"(mark {mark:.2f} vs якорь {pos_avg:.2f}); "
              f"полный лот {pos_size:.4f} = ${pos_size * mark:,.0f} нотионала")
        if book["core_entries"]:
            since = book["core_entries"][-1][0]
            mm = _mae_mfe(sess, symbol, category, since, pos_avg)
            if mm:
                print(f"    от последнего входа ядра: худший ход "
                      f"{mm[0]:+.2f}%, лучший {mm[1]:+.2f}% "
                      f"(в $: {mm[0] / 100 * pos_avg * pos_size:+.0f} / "
                      f"{mm[1] / 100 * pos_avg * pos_size:+.0f})")

    # ── Sanity: книга против API ─────────────────────────────────────────
    book_gross, book_size, book_avg = book["realized"], book["size"], book["avg"]
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
        "core_gross": round(book["realized_core"], 4),
        "tactic_gross": round(book["realized_tac"], 4),
        "forced_core_pnl": round(book["forced_core_pnl"], 4),
        "forced_core_n": int(book["forced_core_n"]),
        "core_time_share": (round(book["core_sec"] / book["span_sec"], 4)
                            if book["span_sec"] else ""),
        "mixed_time_share": (round(book["mixed_sec"] / book["span_sec"], 4)
                             if book["span_sec"] else ""),
        "reentry_cost": round(sum(r["gap_usd"] for r in book["reentries"]), 4),
        "reentry_n": len(book["reentries"]),
        "drift_above_share": (round(sum(1 for d in drifts if d > 0)
                                    / len(drifts), 4) if drifts else ""),
        "cushion_pct": (round((mark / pos_avg - 1.0) * 100.0, 4)
                        if pos_size > 0 and pos_avg > 0 else ""),
        # Два независимых признака достоверности строки. `book_aligned` —
        # сошёлся ли ОСТАТОК с биржей, `book_gross_delta` — сошёлся ли
        # реализованный gross с API. Второе ловит реализации от позиции,
        # открытой до начала окна: остаток при этом может совпасть, а
        # декомпозиция ядро/тактика — врать. Без обоих чисел строку в
        # накопителе не проверить постфактум.
        "book_aligned": int(bool(book.get("aligned"))),
        "book_skipped": int(book.get("skipped") or 0),
        "book_gross_delta": round(book_gross - api_gross, 4),
    }


def _append_snapshot(path: str, rows: list[dict], *,
                     ts_utc: str, window_days: float) -> str | None:
    """Дописывает снапшот в CSV, не ломая уже накопленный ряд.

    Если заголовок в файле не совпадает со `SNAPSHOT_FIELDS` (набор метрик
    расширили), старый файл отложить в сторону и начать новый — сдвинутые
    колонки хуже разрыва в истории. Возвращает путь отложенного файла.
    """
    if not rows:
        return None

    rotated: str | None = None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="") as fh:
            header = (fh.readline() or "").strip().split(",")
        if header != SNAPSHOT_FIELDS:
            stamp = ts_utc.replace("-", "").replace(":", "")
            rotated = f"{path}.{stamp}.bak"
            os.replace(path, rotated)

    fresh = not (os.path.exists(path) and os.path.getsize(path) > 0)
    with open(path, "a", newline="") as fh:
        # extrasaction="raise": новая метрика без правки схемы должна падать
        # громко, а не терять колонку молча.
        w = csv.DictWriter(fh, fieldnames=SNAPSHOT_FIELDS,
                           restval="", extrasaction="raise")
        if fresh:
            w.writeheader()
        for r in rows:
            w.writerow({"ts_utc": ts_utc, "window_days": window_days, **r})
    return rotated


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
        rotated = _append_snapshot(
            args.snapshot, rows,
            ts_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            window_days=args.days)
        if rotated:
            print(f"схема снапшота изменилась, старый файл отложен: {rotated}")
        print(f"снапшот дописан: {args.snapshot}")


if __name__ == "__main__":
    main()
