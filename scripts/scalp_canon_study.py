"""Глубокий разбор sweep_fade_canon (cutoff 2026-06-14): MFE/MAE + regime.
Read-only. НЕ правит логику (no-data-fitting.mdc).

Тот же анализ что scalp_sf_study.py, но для canon-страты — основа под вторую
проверочную стратегию (run-вариант canon). Запуск на VPS:

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_canon_study.py
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from statistics import median

DB = "/data/scalp_bot.sqlite"
STRAT = "sweep_fade_canon"
SINCE = datetime.fromisoformat("2026-06-14").replace(tzinfo=UTC).timestamp()
HORIZON_MIN = 45
CAT = "linear"
BASE = "https://api.bybit.com/v5/market/kline"

_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout")


def _day(ts):
    return datetime.fromtimestamp(ts or 0, UTC).strftime("%Y-%m-%d")


def klines(sym, s, e, interval="1"):
    url = (f"{BASE}?category={CAT}&symbol={sym}&interval={interval}"
           f"&start={int(s*1000)}&end={int(e*1000)}&limit=1000")
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.load(r)
            out = [(int(x[0])/1000.0, float(x[1]), float(x[2]), float(x[3]),
                    float(x[4])) for x in d.get("result", {}).get("list", []) or []]
            out.sort()
            return out
        except Exception:
            time.sleep(0.5)
    return []


def mfe_mae(side, entry, sl, bars):
    R = abs(entry - sl)
    if R <= 0:
        return None
    mfe = mae = 0.0
    for (ts, o, h, l, c) in bars:
        dead = (l <= sl) if side == "long" else (h >= sl)
        if dead:
            break
        fav = (h - entry) if side == "long" else (entry - l)
        adv = (entry - l) if side == "long" else (h - entry)
        if fav > mfe:
            mfe = fav
        if adv > mae:
            mae = adv
    return mfe / R, mae / R


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def report(name, vals, tag=""):
    n = len(vals)
    if not n:
        print(f"  [{name}] нет данных"); return
    print(f"  {name} (n={n}) {tag}")
    print("    перцентили: " + "  ".join(
        f"p{p}={pct(vals,p):.2f}" for p in (10, 25, 50, 70, 75, 90)))
    print(f"    медиана={median(vals):.2f}  среднее={sum(vals)/n:.2f}")
    for L in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        share = sum(1 for v in vals if v >= L) / n * 100
        print(f"    ≥{L:.1f}: {share:4.0f}%")


def day_regime(sym, day_start_ts):
    bars = klines(sym, day_start_ts, day_start_ts + 86400, "15")
    if len(bars) < 8:
        return None, None, None
    op = bars[0][1]; cl = bars[-1][4]
    move = abs(cl - op)
    atr = sum(abs(b[2] - b[3]) for b in bars) / len(bars)
    if atr <= 0:
        return None, None, None
    return move / atr, ("up" if cl > op else "down"), move / op * 100


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [r["name"] for r in con.execute("PRAGMA table_info(trades)")]
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trades WHERE strategy=? AND ts_open>=?",
        (STRAT, SINCE))]
    real = [r for r in rows
            if r["status"] == "closed"
            and str(r["close_reason"] or "") not in _NON_TRADE
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["pnl_usd"] is not None]
    print(f"\n=== {STRAT} с 2026-06-14 | сделок: {len(real)} ===")

    ec, sc, sidc, symc, tsc = "entry", "sl", "side", "symbol", "ts_open"

    # payoff
    wins = [r for r in real if r["pnl_usd"] > 0]
    losses = [r for r in real if r["pnl_usd"] <= 0]
    avgW = sum(r["pnl_usd"] for r in wins) / len(wins) if wins else 0
    avgL = sum(r["pnl_usd"] for r in losses) / len(losses) if losses else 0
    n = len(real); wr = 100 * len(wins) / n if n else 0
    exp = (wr / 100) * avgW + (1 - wr / 100) * avgL
    be = -avgL / (avgW - avgL) * 100 if (avgW - avgL) != 0 else 0
    print(f"WR={wr:.1f}% | avgW=${avgW:+.2f} avgL=${avgL:+.2f} | "
          f"expectancy=${exp:+.2f} | break-even WR={be:.1f}% | "
          f"R:R={avgW/abs(avgL):.2f}:1" if avgL else "")

    # close_reason
    print("\n--- по close_reason ---")
    by_cr = defaultdict(list)
    for r in real:
        by_cr[str(r["close_reason"])].append(r)
    for cr in sorted(by_cr, key=lambda k: -len(by_cr[k])):
        g = by_cr[cr]; net = sum(x["pnl_usd"] for x in g)
        print(f"  {cr:<18} n={len(g):<4} net=${net:+.2f} avg=${net/len(g):+.2f}")

    # символ × сторона
    print("\n--- символ × сторона ---")
    print(f"{'symbol':<10}{'side':<8}{'n':>4}{'WR%':>6}{'net$':>9}")
    by_ss = defaultdict(list)
    for r in real:
        by_ss[(r[symc], r[sidc])].append(r)
    for k in sorted(by_ss, key=lambda k: sum(x["pnl_usd"] for x in by_ss[k])):
        g = by_ss[k]; w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        print(f"{k[0]:<10}{k[1]:<8}{len(g):>4}{100*w/len(g):>5.0f}%{net:>9.2f}")

    # сторона
    print("\n--- по стороне ---")
    by_sd = defaultdict(list)
    for r in real:
        by_sd[r[sidc]].append(r)
    for sd in sorted(by_sd):
        g = by_sd[sd]; w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        print(f"  {sd:<8} n={len(g):<4} WR={100*w/len(g):.0f}% net=${net:+.2f}")

    # MFE/MAE
    print(f"\n--- MFE/MAE (горизонт {HORIZON_MIN}м, до SL) ---")
    win_mfe, loss_mfe, all_mfe, all_mae = [], [], [], []
    long_mfe, short_mfe = [], []
    by_cr_mfe = defaultdict(list)
    miss = 0
    for i, r in enumerate(real):
        bars = klines(r[symc], r[tsc], r[tsc] + HORIZON_MIN * 60)
        if not bars:
            miss += 1; continue
        mm = mfe_mae(r[sidc], float(r[ec]), float(r[sc]), bars)
        if mm is None:
            continue
        mfe, mae = mm
        all_mfe.append(mfe); all_mae.append(mae)
        (long_mfe if r[sidc] == "long" else short_mfe).append(mfe)
        by_cr_mfe[str(r["close_reason"])].append(mfe)
        (win_mfe if r["pnl_usd"] > 0 else loss_mfe).append(mfe)
        if (i + 1) % 60 == 0:
            print(f"  ...{i+1}/{len(real)}")
    print(f"  без клинов: {miss}")
    print("\nMFE ВСЕ:"); report("all", all_mfe)
    print("\nMFE winners:"); report("win", win_mfe, "← основа для TP/exit")
    print("\nMFE losers:"); report("loss", loss_mfe)
    print("\nMAE ВСЕ:"); report("mae", all_mae)
    print("\nMFE по стороне:"); report("long", long_mfe); report("short", short_mfe)
    print("\nMFE по close_reason:")
    for cr in sorted(by_cr_mfe):
        report(cr, by_cr_mfe[cr])

    # regime
    print("\n--- regime дня vs P&L ---")
    by_d = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0,
                                "long_net": 0.0, "short_net": 0.0})
    for r in real:
        d = _day(r["ts_close"] or r["ts_open"]); k = (r[symc], d)
        by_d[k]["n"] += 1; by_d[k]["net"] += r["pnl_usd"]
        if r["pnl_usd"] > 0: by_d[k]["wins"] += 1
        (by_d[k].__setitem__("long_net", by_d[k]["long_net"] + r["pnl_usd"])
         if r[sidc] == "long" else
         by_d[k].__setitem__("short_net", by_d[k]["short_net"] + r["pnl_usd"]))
    reg_cache = {}
    rows_out = []
    for (sym, d), v in by_d.items():
        ds = datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()
        if (sym, d) not in reg_cache:
            reg_cache[(sym, d)] = day_regime(sym, ds)
        ratio, direction, movepct = reg_cache[(sym, d)]
        regime = "TREND" if (ratio or 0) > 1.5 else ("FLAT" if (ratio or 0) < 0.8 else "mix")
        rows_out.append((d, sym, ratio, direction, movepct, v, regime))
    rows_out.sort(key=lambda x: x[0])
    print(f"{'day':<12}{'sym':<10}{'reg':>6}{'dir':>5}{'n':>4}{'WR%':>6}"
          f"{'net$':>9}{'long$':>8}{'short$':>8}")
    for (d, sym, ratio, direction, movepct, v, regime) in rows_out:
        wr = 100 * v["wins"] / v["n"] if v["n"] else 0
        rp = f"{ratio:.2f}" if ratio is not None else "  --"
        print(f"{d:<12}{sym:<10}{rp:>6}{direction or '-':>5}{v['n']:>4}"
              f"{wr:>5.0f}%{v['net']:>9.2f}{v['long_net']:>8.2f}"
              f"{v['short_net']:>8.2f}  {regime}")

    # fade по/против тренда
    print("\n--- fade по тренду vs против ---")
    with_t = {"n": 0, "wins": 0, "net": 0.0}
    against = {"n": 0, "wins": 0, "net": 0.0}
    for r in real:
        d = _day(r["ts_close"] or r["ts_open"])
        ratio, direction, _ = reg_cache.get((r[symc], d), (None, None, None))
        if direction is None:
            continue
        in_trend = (r[sidc] == "long" and direction == "up") or \
                   (r[sidc] == "short" and direction == "down")
        b = with_t if in_trend else against
        b["n"] += 1; b["net"] += r["pnl_usd"]
        if r["pnl_usd"] > 0: b["wins"] += 1
    for lbl, b in (("ПО тренду", with_t), ("ПРОТИВ тренда", against)):
        wr = 100 * b["wins"] / b["n"] if b["n"] else 0
        print(f"  {lbl:<16} n={b['n']:<4} WR={wr:4.0f}% net=${b['net']:+.2f} "
              f"({b['net']/b['n']:+.2f}/сделку)" if b["n"] else f"  {lbl}: нет")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
