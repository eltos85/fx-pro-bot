"""Исследование sweep_fade (база, cutoff 2026-06-17): MFE + MAE + regime
дня, из БД scalp_bot. Read-only. НЕ правит логику (no-data-fitting.mdc).

Артефакт под дизайн новой изолированной стратегии: как далеко winners реально
уходят в плюс (MFE) и лузеры в минус (MAE) ДО исхода, и как дневной P&L зависит
от трендовости дня (|close-open|/ATR по 15m klines).

Запуск на VPS (контейнер имеет доступ к Bybit public API):
    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_sf_study.py

Тянет 1m klines Bybit public (api.bybit.com) — лимит 1000/запрос, горизонт
HORIZON_MIN. Без ключей (public). По символу/стороне/исходу.
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
STRAT = "sweep_fade"
SINCE = datetime.fromisoformat("2026-06-17").replace(tzinfo=UTC).timestamp()
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
    """(mfeR, maeR) до первого достижения SL. R=|entry-sl|."""
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
    """Трендовость дня = |close-open|/ATR(15m). >1.5 — тренд, <0.8 — флет.
    ATR = средний |high-low| по 15m-барам дня (proxy)."""
    bars = klines(sym, day_start_ts, day_start_ts + 86400, "15")
    if len(bars) < 8:
        return None, None, None
    op = bars[0][1]
    cl = bars[-1][4]
    move = abs(cl - op)
    atr = sum(abs(b[2] - b[3]) for b in bars) / len(bars)
    if atr <= 0:
        return None, None, None
    ratio = move / atr
    direction = "up" if cl > op else "down"
    return ratio, direction, move / op * 100  # % дневного хода


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [r["name"] for r in con.execute("PRAGMA table_info(trades)")]
    print("trades cols:", cols)
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trades WHERE strategy=? AND ts_open>=?",
        (STRAT, SINCE))]
    real = [r for r in rows
            if r["status"] == "closed"
            and str(r["close_reason"] or "") not in _NON_TRADE
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["pnl_usd"] is not None]
    print(f"\n=== {STRAT} с 2026-06-17 | сделок: {len(real)} ===")

    # Нужны entry/sl — ищем возможные имена колонок
    ec = next((c for c in cols if c in ("entry_price", "entry", "open_price")), None)
    sc = next((c for c in cols if c in ("sl_price", "sl", "stop_loss")), None)
    print(f"entry col={ec}  sl col={sc}")
    if not ec or not sc:
        print("НЕТ entry/sl колонок — MFE/MAE невозможны. Только regime-блок.")
    sidc = next((c for c in cols if c in ("side", "direction")), "side")
    symc = "symbol"
    tsc = "ts_open"

    # ── 1. MFE / MAE ──
    if ec and sc:
        print(f"\n--- MFE/MAE (горизонт {HORIZON_MIN}м, до SL) ---")
        win_mfe, loss_mfe, all_mfe, all_mae = [], [], [], []
        long_mfe, short_mfe = [], []
        by_cr_mfe = defaultdict(list)
        miss = 0
        for i, r in enumerate(real):
            bars = klines(r[symc], r[tsc], r[tsc] + HORIZON_MIN * 60)
            if not bars:
                miss += 1
                continue
            mm = mfe_mae(r[sidc], float(r[ec]), float(r[sc]), bars)
            if mm is None:
                continue
            mfe, mae = mm
            all_mfe.append(mfe); all_mae.append(mae)
            (long_mfe if r[sidc] == "long" else short_mfe).append(mfe)
            by_cr_mfe[str(r["close_reason"])].append(mfe)
            if r["pnl_usd"] > 0:
                win_mfe.append(mfe)
            else:
                loss_mfe.append(mfe)
            if (i + 1) % 80 == 0:
                print(f"  ...{i+1}/{len(real)}")
        print(f"  без клинов: {miss}")
        print("\nMFE ВСЕ:")
        report("all", all_mfe)
        print("\nMFE winners (pnl>0) — до куда реально доходят:")
        report("win", win_mfe, "← основа для TP/flow_exit")
        print("\nMFE losers (pnl<=0):")
        report("loss", loss_mfe)
        print("\nMAE ВСЕ (глубина убытка до SL):")
        report("mae", all_mae)
        print("\nMFE по стороне:")
        report("long", long_mfe)
        report("short", short_mfe)
        print("\nMFE по close_reason:")
        for cr in sorted(by_cr_mfe):
            report(cr, by_cr_mfe[cr])

    # ── 2. regime дня vs P&L ──
    print("\n--- regime дня vs P&L (трендовость = |close-open|/ATR15m) ---")
    # символ-дневные агрегаты
    by_sd = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0,
                                 "long_net": 0.0, "short_net": 0.0})
    for r in real:
        d = _day(r["ts_close"] or r["ts_open"])
        k = (r[symc], d)
        by_sd[k]["n"] += 1
        by_sd[k]["net"] += r["pnl_usd"]
        if r["pnl_usd"] > 0:
            by_sd[k]["wins"] += 1
        if r[sidc] == "long":
            by_sd[k]["long_net"] += r["pnl_usd"]
        else:
            by_sd[k]["short_net"] += r["pnl_usd"]

    print(f"{'day':<12}{'sym':<10}{'regime':>7}{'dir':>5}{'move%':>7}"
          f"{'n':>4}{'WR%':>6}{'net$':>9}{'long$':>8}{'short$':>8}")
    # кэш режимов по (sym,day)
    reg_cache = {}
    rows_out = []
    for (sym, d), v in by_sd.items():
        day_start = datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()
        if (sym, d) not in reg_cache:
            reg_cache[(sym, d)] = day_regime(sym, day_start)
        ratio, direction, movepct = reg_cache[(sym, d)]
        regime = "TREND" if (ratio or 0) > 1.5 else ("FLAT" if (ratio or 0) < 0.8 else "mix")
        rows_out.append((d, sym, ratio, direction, movepct, v, regime))
    # сортировка по дню, затем по net
    rows_out.sort(key=lambda x: (x[0], x[5]["net"]))
    for (d, sym, ratio, direction, movepct, v, regime) in rows_out:
        wr = 100 * v["wins"] / v["n"] if v["n"] else 0
        rp = f"{ratio:.2f}" if ratio is not None else "  --"
        mp = f"{movepct:.1f}" if movepct is not None else " --"
        dr = direction or "-"
        print(f"{d:<12}{sym:<10}{rp:>7}{dr:>5}{mp:>7}{v['n']:>4}{wr:>5.0f}%"
              f"{v['net']:>9.2f}{v['long_net']:>8.2f}{v['short_net']:>8.2f}  {regime}")

    # ── 3. сводка regime: TREND vs FLAT дни ──
    print("\n--- сводка: P&L в TREND vs FLAT дни (по всем символам) ---")
    trend_net = flat_net = mix_net = 0.0
    trend_n = flat_n = mix_n = 0
    trend_w = flat_w = mix_w = 0
    for (d, sym, ratio, direction, movepct, v, regime) in rows_out:
        if regime == "TREND":
            trend_net += v["net"]; trend_n += v["n"]; trend_w += v["wins"]
        elif regime == "FLAT":
            flat_net += v["net"]; flat_n += v["n"]; flat_w += v["wins"]
        else:
            mix_net += v["net"]; mix_n += v["n"]; mix_w += v["wins"]
    for lbl, net, n, w in (("TREND", trend_net, trend_n, trend_w),
                           ("FLAT", flat_net, flat_n, flat_w),
                           ("mix", mix_net, mix_n, mix_w)):
        wr = 100 * w / n if n else 0
        print(f"  {lbl:<6} n={n:<4} WR={wr:4.0f}%  net=${net:+.2f}")

    # ── 4. fade-по-тренду vs fade-против-тренда ──
    print("\n--- fade по тренду vs против тренда (гипотеза) ---")
    with_trend = {"n": 0, "wins": 0, "net": 0.0}   # long в up-день, short в down-день
    against = {"n": 0, "wins": 0, "net": 0.0}
    for r in real:
        d = _day(r["ts_close"] or r["ts_open"])
        ratio, direction, _ = reg_cache.get((r[symc], d), (None, None, None))
        if direction is None:
            continue
        in_trend = (r[sidc] == "long" and direction == "up") or \
                   (r[sidc] == "short" and direction == "down")
        bucket = with_trend if in_trend else against
        bucket["n"] += 1
        bucket["net"] += r["pnl_usd"]
        if r["pnl_usd"] > 0:
            bucket["wins"] += 1
    for lbl, b in (("FADE ПО тренду (long↑/short↓)", with_trend),
                   ("FADE ПРОТИВ тренда", against)):
        wr = 100 * b["wins"] / b["n"] if b["n"] else 0
        print(f"  {lbl:<32} n={b['n']:<4} WR={wr:4.0f}%  net=${b['net']:+.2f}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
