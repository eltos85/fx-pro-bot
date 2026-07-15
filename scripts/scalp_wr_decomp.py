"""WR-декомпозиция sweep_fade: где проседает винрейт.

Read-only разбор закрытых sweep_fade-сделок за чистое окно:
- по стороне (long/short): n, WR, net, avgW, avgL, R:R;
- long-side и short-side по символам (локализуем просадку / проверяем overfit-
  концентрацию: дребезг одного инструмента vs широкий эффект);
- по score (5 vs прочие);
- частота reasons у ВИНОВ vs ЛУЗОВ (коррелирует ли наличие фильтра с исходом).

Цель — НЕ принять решение (n<100 = sample-size), а локализовать кандидата на
WR-просадку. Никакой подгонки порогов: только разбор фактических исходов.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime


def wr_block(rows: list[dict]) -> dict:
    n = len(rows)
    wins = [r for r in rows if (r["pnl_usd"] or 0) > 0]
    loss = [r for r in rows if (r["pnl_usd"] or 0) <= 0]
    net = sum((r["pnl_usd"] or 0) for r in rows)
    avg_w = (sum(r["pnl_usd"] for r in wins) / len(wins)) if wins else 0.0
    avg_l = (sum(r["pnl_usd"] for r in loss) / len(loss)) if loss else 0.0
    rr = (avg_w / abs(avg_l)) if avg_l else 0.0
    wr = (len(wins) / n * 100) if n else 0.0
    return {"n": n, "wr": wr, "net": net, "avg_w": avg_w, "avg_l": avg_l,
            "rr": rr, "nw": len(wins), "nl": len(loss)}


def fmt(b: dict) -> str:
    return (f"n={b['n']:3d}  WR={b['wr']:5.1f}% ({b['nw']}/{b['n']})  "
            f"net={b['net']:+8.2f}  avgW={b['avg_w']:+6.2f}  avgL={b['avg_l']:+6.2f}  "
            f"R:R={b['rr']:.2f}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-07-10T08:05")
    p.add_argument("--to", default="2026-06-10T00:00")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()
    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,score,reasons,pnl_usd,close_reason FROM trades "
        "WHERE ts_open>=? AND ts_open<? AND strategy='sweep_fade' "
        "AND status='closed' "
        "AND close_reason IN ('flow_exit','sl_hit','tp_hit','flow_scratch','time_stop')",
        (frm, to))]
    con.close()
    print(f"sweep_fade FILLED: {len(rows)}  окно {args.frm}→{args.to}")
    print("(исключены entry_Cancelled/entry_timeout — не налились, pnl=0)\n")
    print("ИТОГО:        ", fmt(wr_block(rows)))

    for sd in ("long", "short"):
        print(f"\n=== {sd.upper()} ===")
        sr = [r for r in rows if r["side"] == sd]
        print("  всё:        ", fmt(wr_block(sr)))
        by_sym = defaultdict(list)
        for r in sr:
            by_sym[r["symbol"]].append(r)
        for sym, rs in sorted(by_sym.items(), key=lambda kv: wr_block(kv[1])["net"]):
            print(f"   {sym:12s}", fmt(wr_block(rs)))

    print("\n=== ПО SCORE ===")
    by_sc = defaultdict(list)
    for r in rows:
        by_sc[r["score"]].append(r)
    for sc, rs in sorted(by_sc.items()):
        print(f"  score={sc}:    ", fmt(wr_block(rs)))

    print("\n=== REASONS: частота у ВИНОВ vs ЛУЗОВ ===")
    win_re = defaultdict(int)
    los_re = defaultdict(int)
    nw = nl = 0
    for r in rows:
        try:
            rs = json.loads(r["reasons"]) if r["reasons"] else []
        except Exception:  # noqa: BLE001
            rs = [r["reasons"]] if r["reasons"] else []
        keys = set()
        for x in rs:
            keys.add(str(x).split(":")[0].split("=")[0].strip())
        won = (r["pnl_usd"] or 0) > 0
        if won:
            nw += 1
        else:
            nl += 1
        for k in keys:
            (win_re if won else los_re)[k] += 1
    allk = sorted(set(win_re) | set(los_re))
    print(f"  (винов {nw}, лузов {nl})")
    for k in allk:
        wp = 100 * win_re[k] / nw if nw else 0
        lp = 100 * los_re[k] / nl if nl else 0
        print(f"   {k:22s} вины {wp:5.1f}%  лузы {lp:5.1f}%  Δ={wp-lp:+5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
