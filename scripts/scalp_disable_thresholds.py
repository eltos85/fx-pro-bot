"""Пороги отключения по sample-size.mdc: берёт ли их какая-нибудь стратегия.

Правило требует ВСЕХ условий сразу: ≥100 сделок, ≥2 недель, p<0.05 и размер
эффекта (|R| ≥ 0.3 либо дефицит винрейта ≥ 10 п.п.). Последнее условие и есть
то, обо что спотыкались прежние решения: значимый минус ещё не повод
отключать, если он мелкий.

Повторяет методику `scalp_sample_readiness_check.py` (артефакт решения
`42c2fff`), чтобы числа были сопоставимы:

- **карантин BTC/ETH с 18.08 11:19 UTC** — до фиксов `c9effac`/`d8f9105` на
  этих символах в статистику затекал P&L соседнего бота с общего one-way лота
  (аудит `dfce2fa`: +$2291 чужого на 20 сделках);
- **исключение контрактов с двойным тарифом** — у них комиссия ~0.5R против
  0.25R, это другая экономика (гейт `fee_tariff_guard`, v0.18.61);
- **кластеризация по символо-дням** — сделки одного символа за день
  коррелированы, независимыми их считать нельзя (Cameron & Miller 2015);
- **исключение стратегий с BE-lock** — у них в БД сдвинутый стоп, знаменатель
  R занижен, R-мультипликаторы раздуты (BUILDLOG 2026-08-10).

Дефицит винрейта считается как «сколько не хватает до безубытка при
фактической геометрии выигрышей и проигрышей».
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime

DB = "/data/scalp_bot.sqlite"
NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
             "entry_Deactivated", "entry_timeout", "entry_netted")
BAD_R = ("sweep_fade_run",)
# Контаминация общего лота лечилась c9effac (24.08) и d8f9105 (25.08); карантин
# берём с момента, зафиксированного в 42c2fff.
QUARANTINE_TS = 1787051940.0  # 2026-08-18 11:19 UTC
QUARANTINE_SYMBOLS = ("BTCUSDT", "ETHUSDT")
STD_TAKER = 0.00055


def cluster_bootstrap_ci(by_cluster: dict[str, list[float]], reps: int = 5000,
                         seed: int = 11) -> tuple[float, float]:
    keys = list(by_cluster)
    if len(keys) < 2:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    means: list[float] = []
    for _ in range(reps):
        pool: list[float] = []
        for _ in range(len(keys)):
            pool.extend(by_cluster[keys[rnd.randrange(len(keys))]])
        if pool:
            means.append(statistics.fmean(pool))
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1])


def p_from_ci(values: list[float]) -> float:
    """Двусторонний t-тест против нуля (для порядка величины p)."""
    n = len(values)
    if n < 2:
        return 1.0
    sd = statistics.stdev(values)
    if sd == 0:
        return 1.0
    t = statistics.fmean(values) / (sd / math.sqrt(n))
    return math.erfc(abs(t) / math.sqrt(2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--days", type=int, default=0, help="0 = вся история")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=20)
    conn.row_factory = sqlite3.Row
    q = conn.execute

    dual = {r["symbol"] for r in q(
        "SELECT symbol FROM symbol_fees WHERE taker_rate > ?", (STD_TAKER,))}

    ph = ",".join("?" for _ in NON_TRADE)
    bad = ",".join("?" for _ in BAD_R)
    sql = (f"SELECT id, ts_close, symbol, side, strategy, entry, sl, qty, "
           f"pnl_usd, fees_usd, close_reason FROM trades "
           f"WHERE status='closed' AND mode='live' AND entry>0 AND sl>0 "
           f"AND pnl_usd IS NOT NULL AND ts_close IS NOT NULL "
           f"AND (close_reason IS NULL OR close_reason NOT IN ({ph})) "
           f"AND strategy NOT IN ({bad})")
    params: list = [*NON_TRADE, *BAD_R]
    if args.days:
        import time
        sql += " AND ts_close>=?"
        params.append(time.time() - args.days * 86400)
    rows = [dict(r) for r in q(sql + " ORDER BY ts_close", params)]
    conn.close()

    kept: list[dict] = []
    drop_quar = drop_dual = drop_risk = 0
    for r in rows:
        if r["symbol"] in dual:
            drop_dual += 1
            continue
        if (r["symbol"] in QUARANTINE_SYMBOLS
                and float(r["ts_close"]) >= QUARANTINE_TS):
            drop_quar += 1
            continue
        risk = abs(float(r["entry"]) - float(r["sl"])) * float(r["qty"] or 0)
        if risk <= 0:
            drop_risk += 1
            continue
        r["netR"] = float(r["pnl_usd"]) / risk
        kept.append(r)

    out: list[str] = []
    out.append("=" * 78)
    out.append("ПОРОГИ ОТКЛЮЧЕНИЯ ПО sample-size.mdc"
               + (f" · последние {args.days} дн" if args.days else " · вся история"))
    out.append("=" * 78)
    out.append(f"  Сделок после фильтров : {len(kept)} из {len(rows)}")
    out.append(f"  Отброшено: карантин BTC/ETH {drop_quar}, "
               f"двойной тариф {drop_dual}, без риска {drop_risk}")
    if dual:
        out.append(f"  Контрактов с двойным тарифом: {len(dual)}")

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_strat[r["strategy"]].append(r)

    out.append("")
    out.append(f"  {'стратегия':<20} {'n':>5} {'класт':>6} {'дней':>5} {'WR':>7} "
               f"{'netR':>8} {'CI95':>20} {'p':>9} {'деф.WR':>8} {'порог':>7}")
    verdicts: list[tuple[str, bool, str]] = []
    for strat, rs in sorted(by_strat.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        netR = [r["netR"] for r in rs]
        wins = [r for r in rs if float(r["pnl_usd"]) > 0]
        wr = len(wins) / n * 100
        cl: dict[str, list[float]] = defaultdict(list)
        days = set()
        for r in rs:
            d = datetime.fromtimestamp(r["ts_close"], tz=UTC).strftime("%Y-%m-%d")
            days.add(d)
            cl[f"{r['symbol']}|{d}"].append(r["netR"])
        lo, hi = cluster_bootstrap_ci(cl)
        mean_r = statistics.fmean(netR)
        p = p_from_ci(netR)

        # Дефицит винрейта: сколько не хватает до безубытка при своей геометрии.
        w = [float(r["pnl_usd"]) for r in rs if float(r["pnl_usd"]) > 0]
        l = [abs(float(r["pnl_usd"])) for r in rs if float(r["pnl_usd"]) < 0]
        if w and l:
            aw, al = statistics.fmean(w), statistics.fmean(l)
            need = al / (aw + al) * 100
            deficit = need - wr
        else:
            deficit = float("nan")

        # Условия правила: все сразу.
        c_n = n >= 100
        c_days = len(days) >= 14
        c_p = p < 0.05
        c_size = abs(mean_r) >= 0.3 or (deficit == deficit and deficit >= 10.0)
        takes = c_n and c_days and c_p and c_size and mean_r < 0
        ci_s = f"[{lo:+.3f}; {hi:+.3f}]"
        out.append(f"  {strat:<20} {n:5d} {len(cl):6d} {len(days):5d} {wr:6.1f}% "
                   f"{mean_r:+8.3f} {ci_s:>20} {p:9.2e} {deficit:7.1f}п "
                   f"{'ДА' if takes else 'нет':>7}")
        if not takes:
            why = []
            if not c_n:
                why.append(f"сделок {n}<100")
            if not c_days:
                why.append(f"дней {len(days)}<14")
            if not c_p:
                why.append(f"p={p:.3f}≥0.05")
            if not c_size:
                why.append(f"эффект мал: |R|={abs(mean_r):.3f}<0.3 и "
                           f"дефицит {deficit:.1f}п<10п")
            verdicts.append((strat, False, "; ".join(why)))
        else:
            verdicts.append((strat, True, "все условия выполнены"))

    out.append("")
    out.append("ВЕРДИКТ ПО КАЖДОЙ")
    for strat, takes, why in verdicts:
        mark = "порог ВЗЯТ — отключение обосновано" if takes else "порог не взят"
        out.append(f"  {strat:<20} {mark}")
        if not takes:
            out.append(f"    {why}")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
