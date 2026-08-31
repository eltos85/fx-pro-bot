"""Статистика scalp_bot: фактические издержки из API против телеметрии БД.

Зачем отдельный скрипт, хотя по scalp уже есть десятки отчётов. Все прежние
разборы считали валовой результат как «чистый + `fees_usd` из БД» либо как
«чистый + МОДЕЛЬНАЯ ставка». Оба пути уже один раз дали неверный вывод:

- `1a90ad2` (10.08): покрытие `fees_usd` разное у стратегий (30% у канона
  против 8% у sweep_fade), поэтому «валовой» занижался сильнее там, где дыр
  больше. Вывод «у канона валовой минус» был снят как неверный.
- Модельная ставка (`round_trip_fee_frac` 0.075%) верна не для всех
  контрактов: `symbol_fees` показывает символы с двойным тарифом
  (BANKUSDT, AKEUSDT, ALPINEUSDT — taker 0.11% вместо 0.055%).

Здесь комиссия берётся ТОЛЬКО из API: `openFee` + `closeFee` записи закрытия,
со сверкой против `execFee` по филлам (две независимые ручки должны сойтись).
Телеметрия БД не используется для расчёта — только для оценки её покрытия.

─── Особенность общего one-way счёта ───

`closedPnl` Bybit считается от `avgEntryPrice` ВСЕЙ позиции символа. На счёте,
который scalp делит со swing-ботом, это средняя нашей и чужой ноги, поэтому
для смешанного лота биржевой P&L описывает не нашу сделку (аудит `dfce2fa`:
+$2291 чужого P&L на 20 сделках BTC/ETH). Бот с `d8f9105` считает такие сделки
по собственной геометрии, и БД для них правдивее API.

Комиссия этой проблемы не имеет: она пропорциональна ОБЪЁМУ, а не цене входа,
поэтому `openFee`/`closeFee` наши в любом случае. Поэтому сверка PnL
показывается отдельно для символов общего лота (BTC/ETH) и для остальных.

Документация API:
- https://bybit-exchange.github.io/docs/v5/position/close-pnl
- https://bybit-exchange.github.io/docs/v5/order/execution
- https://bybit-exchange.github.io/docs/v5/order/order-list

Запуск на VPS:

    scp scripts/collect_scalp_stats.py root@VPS:/tmp/
    ssh root@VPS "docker run --rm \\
        --env-file /root/fx-pro-bot/.env \\
        -v fx-pro-bot_scalp_bot_data:/data:ro \\
        -v /tmp/collect_scalp_stats.py:/script.py:ro \\
        scalp-bot:local python /script.py --days 30"
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

try:
    from pybit.unified_trading import HTTP
except ImportError:
    print("ERROR: pybit не установлен. Запускай через scalp-bot:local.",
          file=sys.stderr)
    sys.exit(2)


DEMO = True
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
PREFIX = "scalp_"
# Технические закрытия — не торговые исходы (src/scalp_bot/state/db.py).
NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
             "entry_Deactivated", "entry_timeout", "entry_netted")
# Символы, где лот делится со swing-ботом (аудит dfce2fa).
SHARED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
# Стандартная сетка Bybit, из неё выведен cfg.round_trip_fee_frac.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
STD_MAKER = 0.0002
STD_TAKER = 0.00055
MATCH_TOL_SEC = 300


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _paged(fn, start_ms: int, end_ms: int, **kw) -> list[dict]:
    """Полный обход с pagination и нарезкой на 7-дневные окна."""
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


def t_test_vs_zero(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return 0.0, 1.0
    sd = statistics.stdev(xs)
    if sd == 0:
        return 0.0, 1.0
    t = statistics.fmean(xs) / (sd / math.sqrt(n))
    return t, math.erfc(abs(t) / math.sqrt(2))


def cluster_bootstrap_ci(values_by_cluster: dict[str, list[float]],
                         reps: int = 5000, seed: int = 12345
                         ) -> tuple[float, float]:
    """95% CI среднего с ресэмплом КЛАСТЕРОВ целиком (символо-дни).

    Сделки одного символа за день коррелированы: один уровень, один режим.
    Наивный CI по независимым наблюдениям в такой выборке слишком узок
    (Cameron & Miller 2015), и билдлог scalp уже опирается на кластерный
    бутстрап — здесь тот же метод, чтобы числа были сопоставимы.
    """
    keys = list(values_by_cluster)
    if len(keys) < 2:
        return (float("nan"), float("nan"))
    rnd = __import__("random").Random(seed)
    means: list[float] = []
    for _ in range(reps):
        pool: list[float] = []
        for _ in range(len(keys)):
            pool.extend(values_by_cluster[keys[rnd.randrange(len(keys))]])
        if pool:
            means.append(statistics.fmean(pool))
    if not means:
        return (float("nan"), float("nan"))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    return (lo, hi)


def load_db(db_path: str, since_ts: float) -> list[dict]:
    if not os.path.exists(db_path):
        print(f"  WARN: БД не найдена: {db_path}", file=sys.stderr)
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in NON_TRADE)
    rows = conn.execute(
        f"SELECT id, ts_open, ts_close, symbol, side, qty, entry, sl, tp, "
        f"strategy, close_reason, pnl_usd, fees_usd, pnl_verified, "
        f"pnl_provisional, exit, entry_order_id "
        f"FROM trades WHERE status='closed' AND mode='live' AND ts_close>=? "
        f"AND (close_reason IS NULL OR close_reason NOT IN ({ph})) "
        f"ORDER BY ts_close", (since_ts, *NON_TRADE)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


def attribute_fees(execs: list[dict], db: list[dict]
                   ) -> tuple[dict[int, float], dict[int, list[int]]]:
    """Комиссия и типы филлов по каждой сделке — точным матчем по метке ордера.

    Метки бота (`src/scalp_bot/trading/executor.py`): вход `scalp_{sym}_{ms}`
    лежит в `trades.entry_order_id`, выход `scalp_{reason}_{trade_id}` несёт id
    сделки прямо в метке. Биржевые SL/TP приходят без нашей метки — их матчим
    по символу и времени внутри жизни сделки.

    Разнесение по обороту (как в первой версии) стирало бы разницу между
    лимитным входом sweep_fade и рыночным входом канона, а именно эта разница
    и есть предмет спора: `1a90ad2` показал, что канон проигрывал не идеей, а
    ценой входа. Считаем по филлам.
    """
    by_link: dict[str, int] = {}
    for t in db:
        lid = (t.get("entry_order_id") or "").strip()
        if lid:
            by_link[lid] = t["id"]
    ids = {t["id"] for t in db}
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for t in db:
        by_symbol[t["symbol"]].append(t)

    fees: dict[int, float] = defaultdict(float)
    kinds: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # [maker, taker]
    for e in execs:
        lid = (e.get("orderLinkId") or "").strip()
        sym = e.get("symbol", "")
        fee = _f(e, "execFee")
        try:
            ts_e = int(e.get("execTime") or 0) / 1000
        except (TypeError, ValueError):
            continue

        tid = by_link.get(lid)
        if tid is None and lid.startswith(PREFIX):
            tail = lid.rsplit("_", 1)[-1]
            if tail.isdigit() and int(tail) in ids:
                tid = int(tail)
        share = 1.0
        if tid is None:
            # Биржевой SL/TP: своей метки нет. Ищем сделку, внутри жизни
            # которой пришёл филл. На общем лоте объём филла может включать
            # чужую ногу — тогда берём долю по объёму, как делает сам бот.
            cands = [t for t in by_symbol.get(sym, [])
                     if t["ts_open"] and t["ts_close"]
                     and t["ts_open"] - 5 <= ts_e <= t["ts_close"] + 60]
            if not cands:
                continue
            t = min(cands, key=lambda x: abs(ts_e - x["ts_close"]))
            tid = t["id"]
            eq = _f(e, "execQty")
            try:
                own = float(t["qty"] or 0)
            except (TypeError, ValueError):
                own = 0.0
            if eq > 0 and own > 0 and eq > own:
                share = own / eq
        fees[tid] += fee * share
        if str(e.get("isMaker")).lower() in ("true", "1"):
            kinds[tid][0] += 1
        else:
            kinds[tid][1] += 1
    return dict(fees), dict(kinds)


def wr(w: int, n: int) -> str:
    return f"{w / n * 100:.1f}%" if n else "—"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = ap.parse_args()

    key = os.environ.get("SCALP_BYBIT_API_KEY", "")
    sec = os.environ.get("SCALP_BYBIT_API_SECRET", "")
    if not key or not sec:
        print("ERROR: нет SCALP_BYBIT_API_KEY / SECRET", file=sys.stderr)
        return 2

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86400 * 1000
    since_ts = time.time() - args.days * 86400

    s = HTTP(demo=DEMO, api_key=key, api_secret=sec, recv_window=20000)

    print(f"[1/4] order_history за {args.days} дн ...", file=sys.stderr)
    orders = _paged(s.get_order_history, start_ms, end_ms, limit=50)
    print(f"  {len(orders)} ордеров", file=sys.stderr)
    print("[2/4] closed_pnl ...", file=sys.stderr)
    cpnl = _paged(s.get_closed_pnl, start_ms, end_ms, limit=200)
    print(f"  {len(cpnl)} записей на субсчёте", file=sys.stderr)
    print("[3/4] executions ...", file=sys.stderr)
    execs = _paged(s.get_executions, start_ms, end_ms, limit=100)
    print(f"  {len(execs)} филлов", file=sys.stderr)
    print("[4/4] локальная БД ...", file=sys.stderr)
    db = load_db(args.db, since_ts)
    print(f"  {len(db)} торговых сделок", file=sys.stderr)

    order_by_id = {(o.get("orderId") or "").strip(): o for o in orders}
    close_ts: dict[str, list[float]] = defaultdict(list)
    for t in db:
        if t["ts_close"]:
            close_ts[t["symbol"]].append(float(t["ts_close"]))

    # ─── Атрибуция ───────────────────────────────────────────────────
    ours: list[dict] = []
    foreign: list[dict] = []
    nontrade: list[dict] = []
    for row in cpnl:
        oid = (row.get("orderId") or "").strip()
        lid = (order_by_id.get(oid, {}).get("orderLinkId") or "").strip()
        sym = row.get("symbol", "?")
        try:
            upd = int(row.get("updatedTime") or 0) / 1000
        except (TypeError, ValueError):
            upd = 0.0
        is_ours = lid.startswith(PREFIX) or any(
            abs(upd - ts) <= MATCH_TOL_SEC for ts in close_ts.get(sym, []))
        if not is_ours:
            foreign.append(row)
            continue
        if (row.get("execType") or "Trade").strip() != "Trade":
            nontrade.append(row)
            continue
        ours.append(row)

    fee_api = sum(_f(r, "openFee") + _f(r, "closeFee") for r in ours)
    turnover = sum(_f(r, "cumEntryValue") + _f(r, "cumExitValue") for r in ours)

    # Комиссия по филлам — вторая независимая ручка.
    fee_exec = 0.0
    exec_turnover = 0.0
    maker_fills = taker_fills = 0
    for e in execs:
        lid = (e.get("orderLinkId") or "").strip()
        sym = e.get("symbol", "")
        try:
            ts_e = int(e.get("execTime") or 0) / 1000
        except (TypeError, ValueError):
            continue
        near = any(abs(ts_e - t) <= MATCH_TOL_SEC for t in close_ts.get(sym, []))
        if not (lid.startswith(PREFIX) or near):
            continue
        fee_exec += _f(e, "execFee")
        exec_turnover += _f(e, "execValue")
        if str(e.get("isMaker")).lower() in ("true", "1"):
            maker_fills += 1
        else:
            taker_fills += 1

    out: list[str] = []
    out.append("=" * 72)
    out.append(f"SCALP-BOT · последние {args.days} дн · demo={DEMO} · "
               f"{datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    out.append("=" * 72)

    out.append("")
    out.append("ИЗДЕРЖКИ ПО API (единственный источник, телеметрия БД не в счёт)")
    out.append(f"  Наших закрытий по API : {len(ours)}")
    out.append(f"  Сделок в БД           : {len(db)}")
    out.append(f"  Комиссия openFee+closeFee : ${fee_api:,.2f}")
    out.append(f"  Комиссия execFee по филлам: ${fee_exec:,.2f}")
    if fee_api > 0:
        drift = abs(fee_api - fee_exec) / fee_api * 100
        out.append(f"  Расхождение двух ручек    : {drift:.1f}% "
                   f"{'OK' if drift < 2 else '— разобраться'}")
    if turnover > 0:
        rate = fee_api / turnover * 100
        out.append(f"  Фактическая ставка        : {rate:.4f}% на сторону, "
                   f"{rate * 2:.4f}% за круг")
        model_rt = (STD_MAKER + STD_TAKER) * 100
        out.append(f"  Модель канона (maker+taker): {model_rt:.4f}% за круг  "
                   f"→ факт {'дороже' if rate * 2 > model_rt else 'дешевле'} "
                   f"в {rate * 2 / model_rt:.2f}×")
    out.append(f"  Филлы maker / taker       : {maker_fills} / {taker_fills}")
    if nontrade:
        out.append(f"  Отброшено не-Trade        : {len(nontrade)}")

    # ─── Покрытие телеметрии БД ──────────────────────────────────────
    fee_db = sum(float(t["fees_usd"] or 0) for t in db)
    covered = sum(1 for t in db if (t["fees_usd"] or 0) > 0)
    out.append("")
    out.append("ПОКРЫТИЕ ТЕЛЕМЕТРИИ fees_usd (почему её нельзя брать за основу)")
    out.append(f"  Сделок с записанной комиссией: {covered} из {len(db)} "
               f"({covered / len(db) * 100:.0f}%)" if db else "  нет сделок")
    out.append(f"  Сумма fees_usd в БД          : ${fee_db:,.2f}")
    out.append(f"  Сумма по API                 : ${fee_api:,.2f}")
    if fee_api > 0:
        out.append(f"  БД недосчитывает             : "
                   f"${fee_api - fee_db:,.2f} ({(1 - fee_db / fee_api) * 100:.0f}%)")
    by_strat_cov: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in db:
        c = by_strat_cov[t["strategy"]]
        c[1] += 1
        if (t["fees_usd"] or 0) > 0:
            c[0] += 1
    out.append("  По стратегиям:")
    for strat, (c, n) in sorted(by_strat_cov.items(), key=lambda kv: -kv[1][1]):
        out.append(f"    {strat:<20} {c:4d}/{n:4d}  ({c / n * 100:3.0f}%)")

    # ─── Результат по БД (net правдивее API на общем лоте) ───────────
    def risk_usd(t: dict) -> float:
        try:
            return abs(float(t["entry"]) - float(t["sl"])) * float(t["qty"])
        except (TypeError, ValueError):
            return 0.0

    rows = [t for t in db if risk_usd(t) > 0 and t["pnl_usd"] is not None]
    out.append("")
    out.append("РЕЗУЛЬТАТ (net из БД: на общем лоте геометрия правдивее API)")
    n = len(rows)
    w = sum(1 for t in rows if float(t["pnl_usd"]) > 0)
    net = sum(float(t["pnl_usd"]) for t in rows)
    out.append(f"  Сделок с валидным R : {n}")
    out.append(f"  Побед / WR          : {w} / {wr(w, n)}")
    out.append(f"  Net PnL             : ${net:+.2f}")
    if n:
        netR = [float(t["pnl_usd"]) / risk_usd(t) for t in rows]
        out.append(f"  Средний netR        : {statistics.fmean(netR):+.3f}")
        clusters: dict[str, list[float]] = defaultdict(list)
        for t, r in zip(rows, netR):
            day = datetime.fromtimestamp(t["ts_close"], tz=UTC).strftime("%m-%d")
            clusters[f"{t['symbol']}|{day}"].append(r)
        lo, hi = cluster_bootstrap_ci(clusters)
        out.append(f"  CI95 netR (кластеры символо-дней, {len(clusters)} шт): "
                   f"[{lo:+.3f}; {hi:+.3f}]")
        tt, pt = t_test_vs_zero(netR)
        out.append(f"  t (netR≠0)          : t={tt:+.2f} p={pt:.6f} "
                   f"{'значимо' if pt < 0.05 else 'не значимо'}")

    # ─── Валовой результат с ФАКТИЧЕСКОЙ комиссией ───────────────────
    fee_of, kind_of = attribute_fees(execs, db)
    matched = sum(fee_of.get(t["id"], 0.0) for t in rows)
    out.append("")
    out.append("ВАЛОВОЙ КРАЙ (комиссия из execFee, привязана к сделке по метке)")
    out.append(f"  Привязано комиссии  : ${matched:,.2f} из ${fee_api:,.2f} "
               f"({matched / fee_api * 100:.0f}%)" if fee_api else "")
    if n:
        grossR: list[float] = []
        feeR: list[float] = []
        for t in rows:
            fee_t = fee_of.get(t["id"], 0.0)
            r = risk_usd(t)
            grossR.append((float(t["pnl_usd"]) + fee_t) / r)
            feeR.append(fee_t / r)
        out.append(f"  Средняя комиссия в R : {statistics.fmean(feeR):.3f}R")
        out.append(f"  Средний валовой R    : {statistics.fmean(grossR):+.3f}")
        gclusters: dict[str, list[float]] = defaultdict(list)
        for t, r in zip(rows, grossR):
            day = datetime.fromtimestamp(t["ts_close"], tz=UTC).strftime("%m-%d")
            gclusters[f"{t['symbol']}|{day}"].append(r)
        glo, ghi = cluster_bootstrap_ci(gclusters)
        out.append(f"  CI95 валового R      : [{glo:+.3f}; {ghi:+.3f}]  "
                   f"{'край не отличим от нуля' if glo <= 0 <= ghi else 'край есть'}")
        tg, pg = t_test_vs_zero(grossR)
        out.append(f"  t (валовой R≠0)      : t={tg:+.2f} p={pg:.4f} "
                   f"{'значимо' if pg < 0.05 else 'НЕ значимо'}")

    # ─── По стратегиям ───────────────────────────────────────────────
    out.append("")
    out.append("ПО СТРАТЕГИЯМ (netR и валовой R с фактической комиссией)")
    out.append(f"  {'стратегия':<20} {'n':>4} {'WR':>7} {'netR':>8} "
               f"{'комис.R':>8} {'валR':>8} {'maker':>6}  CI95 валового R")
    by_strat: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        by_strat[t["strategy"]].append(t)
    for strat, ts_ in sorted(by_strat.items(), key=lambda kv: -len(kv[1])):
        nn = len(ts_)
        ww = sum(1 for t in ts_ if float(t["pnl_usd"]) > 0)
        nR = [float(t["pnl_usd"]) / risk_usd(t) for t in ts_]
        fR = [fee_of.get(t["id"], 0.0) / risk_usd(t) for t in ts_]
        gR = [a + b for a, b in zip(nR, fR)]
        mk = sum(kind_of.get(t["id"], [0, 0])[0] for t in ts_)
        tk = sum(kind_of.get(t["id"], [0, 0])[1] for t in ts_)
        mshare = f"{mk / (mk + tk) * 100:.0f}%" if mk + tk else "—"
        cl: dict[str, list[float]] = defaultdict(list)
        for t, r in zip(ts_, gR):
            day = datetime.fromtimestamp(t["ts_close"], tz=UTC).strftime("%m-%d")
            cl[f"{t['symbol']}|{day}"].append(r)
        lo2, hi2 = cluster_bootstrap_ci(cl, reps=2000)
        ci = (f"[{lo2:+.3f}; {hi2:+.3f}]" if lo2 == lo2 else "мало кластеров")
        out.append(f"  {strat:<20} {nn:4d} {wr(ww, nn):>7} "
                   f"{statistics.fmean(nR):+8.3f} {statistics.fmean(fR):8.3f} "
                   f"{statistics.fmean(gR):+8.3f} {mshare:>6}  {ci}")

    # ─── Лимитный вход против рыночного ──────────────────────────────
    # `1a90ad2` (10.08) заключил: канон — это sweep_fade с рыночным входом
    # вместо лимитного, и проигрывал он «не идеей входа, а ценой входа».
    # Проверяем это на свежем окне и на СОПОСТАВИМЫХ наблюдениях: страты
    # торгуют разные символы в разные дни, поэтому лобовое сравнение средних
    # смешивает эффект входа с эффектом режима.
    out.append("")
    out.append("ЛИМИТНЫЙ ВХОД (sweep_fade) ПРОТИВ РЫНОЧНОГО (канон)")
    grossR_of = {t["id"]: (float(t["pnl_usd"]) + fee_of.get(t["id"], 0.0))
                 / risk_usd(t) for t in rows}
    pair: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    for t in rows:
        if t["strategy"] not in ("sweep_fade", "sweep_fade_canon"):
            continue
        day = datetime.fromtimestamp(t["ts_close"], tz=UTC).strftime("%m-%d")
        pair[f"{t['symbol']}|{day}"][t["strategy"]].append(grossR_of[t["id"]])
    both = {k: v for k, v in pair.items()
            if v.get("sweep_fade") and v.get("sweep_fade_canon")}
    out.append(f"  Символо-дней всего        : {len(pair)}")
    out.append(f"  Где торговали ОБЕ         : {len(both)}")
    if both:
        diffs = {k: [statistics.fmean(v["sweep_fade_canon"])
                     - statistics.fmean(v["sweep_fade"])]
                 for k, v in both.items()}
        flat = [d[0] for d in diffs.values()]
        dlo, dhi = cluster_bootstrap_ci(diffs, reps=5000)
        out.append(f"  Парная разница валового R (канон − лимит), "
                   f"{len(flat)} общих кластеров:")
        out.append(f"    среднее {statistics.fmean(flat):+.3f}  "
                   f"CI95 [{dlo:+.3f}; {dhi:+.3f}]  "
                   f"{'разница есть' if dlo > 0 or dhi < 0 else 'НЕ отличима от нуля'}")
        sf_n = sum(len(v['sweep_fade']) for v in both.values())
        cn_n = sum(len(v['sweep_fade_canon']) for v in both.values())
        out.append(f"    сделок в паре: лимит {sf_n}, рынок {cn_n}")
    out.append("  Когда торговала каждая (сделок по неделям):")
    weeks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in rows:
        wk = datetime.fromtimestamp(t["ts_close"], tz=UTC).strftime("%Y-W%V")
        weeks[wk][t["strategy"]] += 1
    strat_names = sorted({t["strategy"] for t in rows})
    out.append("    неделя    " + "".join(f"{s[:16]:>17}" for s in strat_names))
    for wk in sorted(weeks):
        out.append(f"    {wk:<10}" + "".join(f"{weeks[wk].get(s, 0):>17d}"
                                             for s in strat_names))

    # ─── Общий лот: сверка БД против API ─────────────────────────────
    out.append("")
    out.append("ОБЩИЙ ЛОТ: BTC/ETH против остальных символов")
    for label, pred in (("общий лот (BTC/ETH)", lambda t: t["symbol"] in SHARED_SYMBOLS),
                        ("остальные символы", lambda t: t["symbol"] not in SHARED_SYMBOLS)):
        sub = [t for t in rows if pred(t)]
        if not sub:
            continue
        nn = len(sub)
        ww = sum(1 for t in sub if float(t["pnl_usd"]) > 0)
        pp = sum(float(t["pnl_usd"]) for t in sub)
        geo = 0.0
        for t in sub:
            sign = 1.0 if t["side"] == "Buy" else -1.0
            if t["exit"] and t["entry"]:
                geo += sign * (float(t["exit"]) - float(t["entry"])) * float(t["qty"])
        out.append(f"  {label:<22} n={nn:4d} W={ww:4d} ({wr(ww, nn):>6}) "
                   f"net ${pp:+9.2f}  валовая геометрия ${geo:+9.2f}")

    # ─── Break-even ──────────────────────────────────────────────────
    if n:
        wins = [float(t["pnl_usd"]) for t in rows if float(t["pnl_usd"]) > 0]
        loss = [abs(float(t["pnl_usd"])) for t in rows if float(t["pnl_usd"]) < 0]
        out.append("")
        out.append("АРИФМЕТИКА БЕЗУБЫТКА")
        if wins and loss:
            aw, al = statistics.fmean(wins), statistics.fmean(loss)
            out.append(f"  С комиссией : win ${aw:.2f} / loss ${al:.2f}  "
                       f"R:R {aw / al:.2f}  нужен WR {al / (aw + al) * 100:.1f}%  "
                       f"факт {wr(w, n)}")
        if fee_of:
            gr = [float(t["pnl_usd"]) + fee_of.get(t["id"], 0.0) for t in rows]
            gw = [x for x in gr if x > 0]
            gl = [abs(x) for x in gr if x < 0]
            if gw and gl:
                agw, agl = statistics.fmean(gw), statistics.fmean(gl)
                out.append(f"  Без комиссии: win ${agw:.2f} / loss ${agl:.2f}  "
                           f"R:R {agw / agl:.2f}  нужен WR "
                           f"{agl / (agw + agl) * 100:.1f}%  "
                           f"факт {wr(len(gw), len(gr))}")

    out.append("")
    out.append("ПОРОГИ sample-size.mdc")
    out.append(f"  Сделок : {n} / 100   {'OK' if n >= 100 else 'мало'}")
    out.append(f"  Недель : {args.days / 7:.1f} / 2   "
               f"{'OK' if args.days / 7 >= 2 else 'мало'}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
