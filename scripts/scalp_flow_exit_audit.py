"""flow_exit контрфактуал: крадёт прибыль или спасает от убытка?

Для каждой sweep_fade-сделки, закрытой flow_exit, мотаем 1m-клины Bybit ПОСЛЕ
момента выхода (ts_close) и смотрим, что было бы при УДЕРЖАНИИ родного бакета
(tp/sl из БД): цена раньше дошла до TP (→ flow_exit УКРАЛ прибыль) или до SL
(→ flow_exit СПАС от убытка). Горизонт ограничен (по умолч. 180 мин).

R = |entry - sl|. captured_R — что flow_exit реально снял. Read-only, public
kline API (без ключей). https://bybit-exchange.github.io/docs/v5/market/kline
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import UTC, datetime


def klines(sym: str, s: float, e: float) -> list[tuple[float, float, float]]:
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
           f"&interval=1&start={int(s*1000)}&end={int(e*1000)}&limit=1000")
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.load(r)
            out = [(int(x[0]) / 1000.0, float(x[2]), float(x[3]))
                   for x in d.get("result", {}).get("list", []) or []]
            out.sort()
            return out
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return []


def outcome(side: str, tp: float, sl: float,
            bars: list[tuple[float, float, float]]) -> str:
    """Что случилось бы при удержании: 'tp' / 'sl' / 'none' (в пределах горизонта)."""
    for (_ts, hi, lo) in bars:
        if side == "long":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp
        # если в одном баре задело и TP и SL — консервативно считаем SL (хуже)
        if hit_sl:
            return "sl"
        if hit_tp:
            return "tp"
    return "none"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-07-10T08:05")
    p.add_argument("--to", default="2026-07-15T07:30")
    p.add_argument("--horizon-min", type=int, default=180)
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()

    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,entry,sl,tp,exit,pnl_usd,ts_close,qty FROM trades "
        "WHERE ts_open>=? AND ts_open<? AND strategy='sweep_fade' "
        "AND close_reason='flow_exit' AND status='closed'", (frm, to))]
    con.close()
    print(f"flow_exit сделок: {len(rows)}  горизонт {args.horizon_min}м")

    steal_r = save_r = 0.0
    n_tp = n_sl = n_none = miss = 0
    capt_rs = []
    for r in rows:
        entry, sl, tp, ex = r["entry"], r["sl"], r["tp"], r["exit"]
        side = r["side"]
        R = abs(entry - sl)
        if R <= 0 or not tp or not sl or not ex:
            miss += 1
            continue
        capt = ((ex - entry) if side == "long" else (entry - ex)) / R
        capt_rs.append(capt)
        bars = klines(r["symbol"], r["ts_close"], r["ts_close"] + args.horizon_min * 60)
        if not bars:
            miss += 1
            continue
        oc = outcome(side, tp, sl, bars)
        tp_r = abs(tp - entry) / R
        if oc == "tp":
            n_tp += 1
            steal_r += (tp_r - capt)  # недополученная прибыль
        elif oc == "sl":
            n_sl += 1
            save_r += (capt - (-1.0))  # спасённый убыток (capt − (−1R))
        else:
            n_none += 1

    n = n_tp + n_sl + n_none
    print(f"\nобработано {n} (без клинов/данных: {miss})")
    if capt_rs:
        print(f"flow_exit снимал в среднем: {sum(capt_rs)/len(capt_rs):+.2f}R")
    if not n:
        return 0
    print(f"\nЧТО БЫЛО БЫ ПРИ УДЕРЖАНИИ (родной TP/SL):")
    print(f"  дошло бы до TP (flow_exit УКРАЛ прибыль): {n_tp}  ({100*n_tp/n:.0f}%)")
    print(f"  дошло бы до SL (flow_exit СПАС от убытка): {n_sl}  ({100*n_sl/n:.0f}%)")
    print(f"  ни TP ни SL за горизонт (не определено):  {n_none}  ({100*n_none/n:.0f}%)")
    print(f"\nИТОГ в R (только определённые случаи):")
    print(f"  украдено (могли получить ещё): {steal_r:+.2f}R на {n_tp} сделках")
    print(f"  спасено (избежали стопа):      {save_r:+.2f}R на {n_sl} сделках")
    net = save_r - steal_r
    verdict = "flow_exit ПОЛЕЗЕН (спасает больше, чем крадёт)" if net > 0 \
        else "flow_exit ВРЕДЕН (крадёт больше, чем спасает)"
    print(f"  ЧИСТО: {net:+.2f}R → {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
