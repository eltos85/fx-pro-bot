"""Статистика impulse-bot: полная атрибуция на общем субсчёте + точные комиссии.

Зачем отдельный скрипт, а не `collect_bybit_3bots_stats.py`: тот делит
общий субсчёт только по префиксу `ai_` и валит impulse в кучу «bybit-bot».
Плюс он не поднимает `execFee`, без которых по impulse нельзя отделить
результат сигнала от издержек.

─── Почему атрибуция сложнее, чем «отфильтровать по orderLinkId» ───

`get_closed_pnl` отдаёт `orderId` **закрывающего** ордера. Наш префикс
`impulse_` стоит только на ордерах, которые бот отправляет сам: вход
(Market) и scratch-выход (Market reduce-only). Закрытия по биржевому
SL/TP создаёт Bybit — у них `orderLinkId` пустой, и фильтр по префиксу
их теряет. На выборке 2026-08-21..31 это занижало выборку вдвое
(38 сделок вместо 80) и убыток втрое ($200 вместо $638).

Поэтому закрытия матчим двумя проходами:
  A) `orderLinkId` начинается с `impulse_`      → наш scratch-выход;
  B) (symbol, время) попадает в окно сделки из локальной SQLite → биржевой
     SL/TP. Окно ±`MATCH_TOL_SEC` вокруг `ts_close`.

Проверка чистоты встроена: число входных филлов должно совпасть с числом
выходных и с числом сделок в БД. Если не совпало — печатается warning,
выводам верить нельзя.

Иерархия источников — `.cursor/rules/stats-collection.mdc`: API главнее
локальной SQLite. SQLite тут нужна только как след цикла (reason,
время удержания, окна для матча), её `pnl_usd` считается без комиссий
и правдой не является.

Документация API:
- https://bybit-exchange.github.io/docs/v5/position/close-pnl
- https://bybit-exchange.github.io/docs/v5/order/execution
- https://bybit-exchange.github.io/docs/v5/order/order-list

Запуск на VPS:

    scp scripts/collect_impulse_stats.py root@VPS:/tmp/
    ssh root@VPS "docker run --rm \\
        --env-file /root/fx-pro-bot/.env \\
        -v fx-pro-bot_impulse_bot_data:/data:ro \\
        -v /tmp/collect_impulse_stats.py:/script.py:ro \\
        impulse-bot:local python /script.py --days 10"

Том называется `fx-pro-bot_impulse_bot_data` (префикс проекта compose),
не `impulse_bot_data` — иначе БД смонтируется пустой и разделы про
reason/удержание молча покажут нули.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

try:
    from pybit.unified_trading import HTTP
except ImportError:
    print("ERROR: pybit не установлен. Запускай через impulse-bot:local.",
          file=sys.stderr)
    sys.exit(2)


# ─── Константы ───────────────────────────────────────────────────────────

DEMO = True
# get_closed_pnl / get_executions принимают окно ≤7 дней за вызов.
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
PREFIX = "impulse_"
# Допуск матча закрытия с записью БД. Бот пишет ts_close в момент, когда
# увидел broker.size==0, то есть с лагом до одного цикла поллинга (15с).
MATCH_TOL_SEC = 300
# Канон STRATEGY_RATIONALE_IMPULSE.md.
TP_PCT = 0.45
SL_PCT = 0.25


# ─── API ─────────────────────────────────────────────────────────────────

def _paged(fn, start_ms: int, end_ms: int, **kw) -> list[dict]:
    """Полный обход с pagination и нарезкой на 7-дневные окна.

    Без `while cursor` API отдаёт только первую страницу — запрещено
    правилом stats-collection.mdc.
    """
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        nxt = min(cur + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            try:
                r = fn(category="linear", startTime=cur, endTime=nxt,
                       cursor=cursor, **kw)
            except Exception as e:
                print(f"  WARN {getattr(fn, '__name__', fn)}: {e}", file=sys.stderr)
                break
            if r.get("retCode") != 0:
                print(f"  WARN retCode={r.get('retCode')} {r.get('retMsg')}",
                      file=sys.stderr)
                break
            res = r.get("result", {}) or {}
            out.extend(res.get("list", []) or [])
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
            time.sleep(0.05)
        cur = nxt
        time.sleep(0.05)
    return out


# ─── Статистика ──────────────────────────────────────────────────────────

def binom_p_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Точный двусторонний биномиальный тест. Без scipy — его нет в образе."""
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))

    obs = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= obs * 1.0000001))


def t_test_vs_zero(xs: list[float]) -> tuple[float, float]:
    """t-статистика и двустороннее p (нормальное приближение) для H0: mean==0."""
    n = len(xs)
    if n < 2:
        return 0.0, 1.0
    sd = statistics.stdev(xs)
    if sd == 0:
        return 0.0, 1.0
    t = statistics.fmean(xs) / (sd / math.sqrt(n))
    return t, math.erfc(abs(t) / math.sqrt(2))


# ─── Разбор записи закрытия ──────────────────────────────────────────────

def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def pos_side(row: dict) -> str:
    """Сторона ПОЗИЦИИ. В closed-pnl `side` — сторона закрывающего ордера,
    то есть противоположная. Long закрывается Sell-ордером."""
    return "Buy" if (row.get("side") or "").strip() == "Sell" else "Sell"


def price_move_pct(row: dict) -> float | None:
    """Ход цены от входа к выходу в сторону позиции, %. По ценам биржи."""
    entry = _f(row, "avgEntryPrice")
    exit_ = _f(row, "avgExitPrice")
    if entry <= 0 or exit_ <= 0:
        return None
    sign = 1.0 if pos_side(row) == "Buy" else -1.0
    return sign * (exit_ / entry - 1.0) * 100.0


def fees_of(row: dict) -> float:
    """Комиссия круга из самой записи закрытия (openFee + closeFee)."""
    return _f(row, "openFee") + _f(row, "closeFee")


# ─── Локальная БД ────────────────────────────────────────────────────────

def load_db_trades(db_path: str, since_ts: int) -> tuple[list[dict], int]:
    if not os.path.exists(db_path):
        print(f"  WARN: БД не найдена: {db_path}. Проверь имя docker-тома.",
              file=sys.stderr)
        return [], 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        base = ["ts_open", "ts_close", "symbol", "side", "qty", "entry",
                "exit", "pnl_usd", "reason"]
        extra = [c for c in ("entry_real", "exit_real", "pnl_net", "burst_usd",
                             "move_pct", "tape_buy", "tape_sell",
                             "cluster_frac", "turnover24h") if c in have]
        cols = base + extra
        trades = [
            dict(zip(["ts_open", "ts_close", "symbol", "side", "qty", "entry",
                      "exit", "pnl", "reason"] + extra, row))
            for row in conn.execute(
                f"SELECT {', '.join(cols)} FROM trades "
                f"WHERE ts_close >= ? ORDER BY ts_close", (since_ts,))
        ]
        n_open = int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
        return trades, n_open
    finally:
        conn.close()


def signal_breakdown(trades: list[dict]) -> list[str]:
    """Исход сделки против силы сигнала на входе.

    Поля появились 2026-08-31; у более старых сделок они NULL, такие строки
    пропускаются. Пока накопленных сделок мало, разбивка показывается как
    наблюдение — решения по ней принимать нельзя (sample-size.mdc).
    """
    out: list[str] = []
    fields = (("burst_usd", "сила удара, $"),
              ("move_pct", "ход на входе, %"),
              ("cluster_frac", "кластер"),
              ("turnover24h", "оборот инструмента, $"))
    for field, label in fields:
        rows = [t for t in trades
                if t.get(field) is not None and t.get("exit_real")
                and t.get("entry_real")]
        if len(rows) < 8:
            continue
        rows.sort(key=lambda t: t[field])
        half = len(rows) // 2
        out.append(f"  {label}")
        for name, grp in (("нижняя половина", rows[:half]),
                          ("верхняя половина", rows[half:])):
            wins = sum(1 for t in grp if (t.get("pnl_net") or 0) > 0)
            pnl = sum(t.get("pnl_net") or 0 for t in grp)
            lo = min(t[field] for t in grp)
            hi = max(t[field] for t in grp)
            out.append(f"    {name:<18} n={len(grp):3d}  побед {wins:3d} "
                       f"({wins/len(grp)*100:3.0f}%)  PnL ${pnl:+.2f}  "
                       f"диапазон {lo:.4g}…{hi:.4g}")
    if not out:
        return ["  Пока нет сделок со снимком сигнала — поля пишутся с 2026-08-31."]
    return out


# ─── Основная логика ─────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10, help="окно назад, дней")
    ap.add_argument("--db", default="/data/impulse_bot.sqlite")
    args = ap.parse_args()

    key = (os.environ.get("IMPULSE_BYBIT_API_KEY")
           or os.environ.get("SCALP_BYBIT_API_KEY") or "")
    sec = (os.environ.get("IMPULSE_BYBIT_API_SECRET")
           or os.environ.get("SCALP_BYBIT_API_SECRET") or "")
    if not key or not sec:
        print("ERROR: нет IMPULSE_BYBIT_API_KEY / SCALP_BYBIT_API_KEY", file=sys.stderr)
        return 2

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 24 * 60 * 60 * 1000
    since_ts = int((datetime.now(tz=UTC) - timedelta(days=args.days)).timestamp())

    s = HTTP(demo=DEMO, api_key=key, api_secret=sec, recv_window=20000)

    print("[1/4] order_history ...", file=sys.stderr)
    orders = _paged(s.get_order_history, start_ms, end_ms, limit=50)
    print(f"  {len(orders)} ордеров", file=sys.stderr)

    print("[2/4] closed_pnl ...", file=sys.stderr)
    cpnl = _paged(s.get_closed_pnl, start_ms, end_ms, limit=200)
    print(f"  {len(cpnl)} closed-pnl записей на субсчёте", file=sys.stderr)

    print("[3/4] executions ...", file=sys.stderr)
    execs = _paged(s.get_executions, start_ms, end_ms, limit=100)
    print(f"  {len(execs)} филлов", file=sys.stderr)

    print("[4/4] локальная БД ...", file=sys.stderr)
    db_trades, db_open = load_db_trades(args.db, since_ts)
    print(f"  {len(db_trades)} сделок, {db_open} открытых", file=sys.stderr)

    order_by_id = {(o.get("orderId") or "").strip(): o for o in orders}
    windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    close_ts: dict[str, list[int]] = defaultdict(list)
    for t in db_trades:
        windows[t["symbol"]].append(
            (t["ts_open"] - MATCH_TOL_SEC, t["ts_close"] + MATCH_TOL_SEC))
        close_ts[t["symbol"]].append(t["ts_close"])

    # ─── Атрибуция closed_pnl ────────────────────────────────────────
    scratch_rows: list[dict] = []
    stop_rows: list[dict] = []
    foreign_rows: list[dict] = []
    nontrade_rows: list[dict] = []
    stop_kinds: Counter[str] = Counter()
    nontrade_kinds: Counter[str] = Counter()

    for row in cpnl:
        oid = (row.get("orderId") or "").strip()
        o = order_by_id.get(oid, {})
        lid = (o.get("orderLinkId") or "").strip()
        sym = row.get("symbol", "?")
        try:
            upd = int(row.get("updatedTime") or 0) // 1000
        except (TypeError, ValueError):
            upd = 0

        if lid.startswith(PREFIX):
            kind = "scratch"
        elif any(abs(upd - ts) <= MATCH_TOL_SEC for ts in close_ts.get(sym, [])):
            kind = "stop"
        else:
            foreign_rows.append(row)
            continue

        # execType: обычная сделка — только `Trade`. Ликвидации (BustTrade),
        # расчёты (Settle, SessionSettlePnL) и переносы (MovePosition) в
        # статистику стратегии мешать нельзя.
        # https://bybit-exchange.github.io/docs/v5/position/close-pnl
        etype = (row.get("execType") or "Trade").strip()
        if etype != "Trade":
            nontrade_rows.append(row)
            nontrade_kinds[etype] += 1
            continue

        if kind == "scratch":
            scratch_rows.append(row)
        else:
            stop_rows.append(row)
            stop_kinds[(o.get("stopOrderType") or "?").strip() or "?"] += 1

    impulse_rows = scratch_rows + stop_rows

    # ─── Комиссии из execFee ─────────────────────────────────────────
    fee_total = 0.0
    turnover = 0.0
    n_entry = n_exit = 0
    for e in execs:
        lid = (e.get("orderLinkId") or "").strip()
        sym = e.get("symbol", "")
        try:
            fee = float(e.get("execFee") or 0.0)
            val = float(e.get("execValue") or 0.0)
            ts = int(e.get("execTime") or 0) // 1000
        except (TypeError, ValueError):
            continue
        is_ours = lid.startswith(PREFIX)
        in_window = any(lo <= ts <= hi for lo, hi in windows.get(sym, []))
        if not (is_ours or in_window):
            continue
        fee_total += fee
        turnover += val
        closed = str(e.get("closedSize") or "0")
        if is_ours and closed in ("0", "", "None"):
            n_entry += 1
        else:
            n_exit += 1

    def agg(rows: list[dict]) -> tuple[int, int, float]:
        n = len(rows)
        wins = 0
        tot = 0.0
        for r in rows:
            try:
                v = float(r.get("closedPnl") or 0.0)
            except (ValueError, TypeError):
                v = 0.0
            tot += v
            if v > 0:
                wins += 1
        return n, wins, tot

    n_all, w_all, pnl_all = agg(impulse_rows)

    # ─── Вывод ───────────────────────────────────────────────────────
    def wr(w: int, n: int) -> str:
        return f"{w / n * 100:.1f}%" if n else "—"

    out: list[str] = []
    out.append("=" * 70)
    out.append(f"IMPULSE-BOT · последние {args.days} дн · demo={DEMO} · "
               f"{datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    out.append("=" * 70)

    fee_cpnl = sum(fees_of(r) for r in impulse_rows)
    fill_count = sum(int(_f(r, "fillCount")) for r in impulse_rows)

    out.append("")
    out.append("ПРОВЕРКА ЧИСТОТЫ ВЫБОРКИ")
    out.append(f"  Сделок по API      : {n_all}")
    out.append(f"  Сделок в локальной БД: {len(db_trades)}")
    out.append(f"  Филлов вход/выход  : {n_entry} / {n_exit}")
    out.append(f"  fillCount по API   : {fill_count}")
    consistent = (n_all == len(db_trades) == n_entry == n_exit)
    out.append(f"  Согласовано        : {'да' if consistent else 'НЕТ — выводам не верить'}")
    if nontrade_rows:
        _, _, nt_pnl = agg(nontrade_rows)
        kinds = ", ".join(f"{k}={v}" for k, v in nontrade_kinds.most_common())
        out.append(f"  Отброшено не-Trade : {len(nontrade_rows)} ({kinds}) "
                   f"на ${nt_pnl:+.2f} — не результат стратегии")
    else:
        out.append("  Отброшено не-Trade : 0 — ликвидаций и расчётов нет")
    if fee_cpnl > 0:
        drift = abs(fee_cpnl - fee_total) / fee_cpnl * 100
        out.append(f"  Комиссия: closed-pnl ${fee_cpnl:,.2f} vs execFee "
                   f"${fee_total:,.2f}  расхождение {drift:.1f}% "
                   f"{'OK' if drift < 2 else '— разобраться'}")

    # WR по деньгам и по цене — разные числа: комиссия переводит часть
    # сделок из плюса в минус.
    moves_api = [(r, price_move_pct(r)) for r in impulse_rows]
    moves_ok = [(r, m) for r, m in moves_api if m is not None]
    w_price = sum(1 for _, m in moves_ok if m > 0)
    sign_mismatch = sum(1 for r, m in moves_ok
                        if (m > 0) != (_f(r, "closedPnl") + fees_of(r) > 0))

    out.append("")
    out.append("РЕЗУЛЬТАТ (API net, с комиссиями и фандингом)")
    out.append(f"  Сделок             : {n_all}")
    out.append(f"  Побед по деньгам   : {w_all} / {wr(w_all, n_all)}")
    if moves_ok:
        out.append(f"  Побед по цене      : {w_price} / {wr(w_price, len(moves_ok))}"
                   f"   (цена ушла в нашу сторону)")
        out.append(f"  Комиссия убила     : {w_price - w_all} сделок "
                   f"из плюса по цене в минус по деньгам")
        if sign_mismatch:
            out.append(f"  WARN: знак хода и знак gross расходятся в "
                       f"{sign_mismatch} записях — проверить сторону позиции")
    out.append(f"  Net PnL            : ${pnl_all:+.2f}")
    if n_all:
        out.append(f"  На сделку          : ${pnl_all / n_all:+.2f}")

    out.append("")
    out.append("ИЗДЕРЖКИ (факт из API, не оценка)")
    out.append(f"  Оборот             : ${turnover:,.0f}")
    out.append(f"  Комиссия всего     : ${fee_cpnl:,.2f}")
    if turnover > 0:
        rate = fee_cpnl / turnover * 100
        out.append(f"  Ставка на сторону  : {rate:.4f}%   круг: {rate * 2:.4f}%")
    if n_all:
        out.append(f"  Комиссия на сделку : ${fee_cpnl / n_all:.2f}")
        out.append(f"  Средний нотионал   : ${turnover / n_all / 2:,.0f}")
    gross = pnl_all + fee_cpnl
    out.append(f"  PnL без комиссий   : ${gross:+.2f}")
    if pnl_all < 0:
        out.append(f"  Доля комиссии в убытке: {fee_cpnl / abs(pnl_all) * 100:.0f}%")

    # Long и short разделяем: смешанная статистика прячет одностороннюю поломку.
    out.append("")
    out.append("ПО СТОРОНЕ ПОЗИЦИИ")
    for side_label, side_key in (("long (Buy)", "Buy"), ("short (Sell)", "Sell")):
        rows = [r for r in impulse_rows if pos_side(r) == side_key]
        n, w, p = agg(rows)
        if not n:
            continue
        mv = [m for r, m in moves_ok if pos_side(r) == side_key]
        mv_txt = f"  средний ход {statistics.fmean(mv):+.4f}%" if mv else ""
        out.append(f"  {side_label:<13} n={n:3d}  W={w:3d} ({wr(w, n):>6s})  "
                   f"${p:+.2f}{mv_txt}")

    # Стабильность по дням: один плохой день не должен решать за всю выборку.
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in impulse_rows:
        try:
            day = datetime.fromtimestamp(
                int(r.get("updatedTime") or 0) / 1000, tz=UTC).strftime("%m-%d")
        except (TypeError, ValueError, OSError):
            continue
        by_day[day].append(_f(r, "closedPnl"))
    if by_day:
        pos_days = sum(1 for v in by_day.values() if sum(v) > 0)
        out.append("")
        out.append(f"ПО ДНЯМ (прибыльных дней {pos_days} из {len(by_day)})")
        for day in sorted(by_day):
            v = by_day[day]
            w = sum(1 for x in v if x > 0)
            bar = "+" if sum(v) > 0 else "-"
            out.append(f"  {day}  n={len(v):3d}  W={w:3d} ({wr(w, len(v)):>6s})  "
                       f"${sum(v):+8.2f} {bar}")

    out.append("")
    out.append("АТРИБУЦИЯ ЗАКРЫТИЙ")
    for label, rows in (("scratch (наш reduce-only)", scratch_rows),
                        ("биржевой SL/TP", stop_rows),
                        ("чужие боты на субсчёте", foreign_rows)):
        n, w, p = agg(rows)
        out.append(f"  {label:<28} n={n:3d}  W={w:3d} ({wr(w, n):>6s})  ${p:+.2f}")
    if stop_kinds:
        out.append("    из них: " + ", ".join(f"{k}={v}" for k, v in stop_kinds.most_common()))

    if db_trades:
        out.append("")
        out.append("ПО reason (локальная БД: PnL расчётный, БЕЗ комиссий)")
        by_reason: dict[str, dict] = {}
        for t in db_trades:
            d = by_reason.setdefault(t["reason"], {"n": 0, "w": 0, "hold": []})
            d["n"] += 1
            d["hold"].append(t["ts_close"] - t["ts_open"])
            if t["pnl"] > 0:
                d["w"] += 1
        for reason, d in sorted(by_reason.items(), key=lambda kv: -kv[1]["n"]):
            med = statistics.median(d["hold"]) if d["hold"] else 0
            out.append(f"  {reason:<16} n={d['n']:3d}  W={d['w']:3d} "
                       f"({wr(d['w'], d['n']):>6s})  медиана удержания {med:.0f}с")

    # Ход цены — по ценам исполнения биржи (avgEntry/avgExitPrice), не по
    # локальным: в БД до 2026-08-31 писалась цена момента обнаружения.
    moves = [m for _, m in moves_ok]
    if moves:
        reached_tp = sum(1 for m in moves if m >= TP_PCT * 0.95)
        reached_sl = sum(1 for m in moves if m <= -SL_PCT * 0.95)
        middle = len(moves) - reached_tp - reached_sl
        tot = len(moves)
        out.append("")
        out.append(f"ХОД ЦЕНЫ ОТ ВХОДА (канон TP {TP_PCT}% / SL {SL_PCT}%, цены биржи)")
        out.append(f"  Дошли до TP        : {reached_tp:3d} ({reached_tp/tot*100:.0f}%)")
        out.append(f"  Дошли до SL        : {reached_sl:3d} ({reached_sl/tot*100:.0f}%)")
        out.append(f"  Обрезаны между     : {middle:3d} ({middle/tot*100:.0f}%)")
        out.append(f"  Средний ход        : {statistics.fmean(moves):+.4f}%")
        out.append(f"  Медиана хода       : {statistics.median(moves):+.4f}%")
        if turnover > 0:
            need = fee_cpnl / turnover * 200
            out.append(f"  Нужно для нуля     : {need:+.4f}%  "
                       f"(разрыв {statistics.fmean(moves) - need:+.4f} п.п.)")

    if db_trades:
        out.append("")
        out.append("ИСХОД ПРОТИВ СИЛЫ СИГНАЛА (наблюдение, не основание для фильтра)")
        out.extend(signal_breakdown(db_trades))

        n_real = sum(1 for t in db_trades if t.get("pnl_net") is not None)
        if n_real:
            real = [t["pnl_net"] for t in db_trades if t.get("pnl_net") is not None]
            out.append("")
            out.append(f"NET PnL ИЗ БД (фактический, пишется с 2026-08-31): "
                       f"n={n_real}  сумма ${sum(real):+.2f}")

    # ─── Стат-проверка ───────────────────────────────────────────────
    pnls: list[float] = []
    for r in impulse_rows:
        try:
            pnls.append(float(r.get("closedPnl") or 0.0))
        except (ValueError, TypeError):
            pass
    if pnls:
        n = len(pnls)
        w = sum(1 for x in pnls if x > 0)
        wins_l = [x for x in pnls if x > 0]
        loss_l = [x for x in pnls if x < 0]
        out.append("")
        out.append("СТАТ-ПРОВЕРКА (пороги sample-size.mdc)")
        out.append(f"  Сделок      : {n:3d} / 100   {'OK' if n >= 100 else 'мало'}")
        weeks = args.days / 7
        out.append(f"  Недель      : {weeks:.1f} / 2   {'OK' if weeks >= 2 else 'мало'}")
        pb = binom_p_two_sided(w, n, 0.5)
        out.append(f"  p (WR≠50%)  : {pb:.4f}   {'значимо' if pb < 0.05 else 'не значимо'}")
        t, pt = t_test_vs_zero(pnls)
        out.append(f"  t (PnL≠0)   : t={t:+.2f} p={pt:.4f}   "
                   f"{'значимо' if pt < 0.05 else 'не значимо'}")
        if wins_l and loss_l:
            aw = statistics.fmean(wins_l)
            al = abs(statistics.fmean(loss_l))
            out.append(f"  Средний win : ${aw:.2f}   средний loss: ${al:.2f}   R:R {aw/al:.2f}")
            out.append(f"  Break-even WR: {al/(aw+al)*100:.1f}%   факт {wr(w, n)}")

        # Тот же расчёт без издержек: показывает, сколько из требуемого
        # винрейта создаёт комиссия, а сколько — сама форма сделки.
        gr = [_f(r, "closedPnl") + fees_of(r) for r in impulse_rows]
        gw = [x for x in gr if x > 0]
        gl = [abs(x) for x in gr if x < 0]
        if gw and gl:
            agw = statistics.fmean(gw)
            agl = statistics.fmean(gl)
            be_gross = agl / (agw + agl) * 100
            out.append(f"  Без комиссии : win ${agw:.2f} / loss ${agl:.2f}  "
                       f"R:R {agw/agl:.2f}  break-even WR {be_gross:.1f}%  "
                       f"факт {wr(len(gw), len(gr))}")
            tg, ptg = t_test_vs_zero(gr)
            out.append(f"  t (gross≠0) : t={tg:+.2f} p={ptg:.4f}   "
                       f"{'значимо' if ptg < 0.05 else 'НЕ значимо — сигнал неотличим от нуля'}")
            out.append(f"    сумма gross ${sum(gr):+.2f} = "
                       f"{len(gw)} плюс / {sum(1 for x in gr if x < 0)} минус / "
                       f"{sum(1 for x in gr if x == 0)} в ноль")

    out.append("")
    out.append(f"Открытых позиций impulse: {db_open}")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
