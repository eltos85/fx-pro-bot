"""sweep_fade: чем лузы отличаются от винов (поиск отсекаемого паттерна для WR).

Только данные из БД (без подгонки): срез по монете, стороне, score, набору
сигналов (reasons) и СЕРИЙНОСТИ (вход после недавнего SL по той же монете).
Окно задаётся --from/--to. Read-only.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime


def wr(items):
    n = len(items)
    w = sum(1 for x in items if x["pnl_usd"] > 0)
    net = sum(x["pnl_usd"] for x in items)
    return n, w, (100 * w / n if n else 0), net


def line(label, items, flag_small=True):
    n, w, r, net = wr(items)
    mark = "  (мало!)" if flag_small and n < 8 else ""
    print(f"  {label:<22}{n:>4}{w:>5}{r:>5.0f}%{net:>9.2f}{mark}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-07-10T08:05")
    p.add_argument("--to", default="2026-06-08T00:00")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()
    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,score,reasons,pnl_usd,close_reason,ts_open "
        "FROM trades WHERE ts_open>=? AND ts_open<? AND strategy='sweep_fade' "
        "AND status='closed' AND close_reason NOT LIKE 'entry_%' "
        "AND close_reason!='restart_flat' AND pnl_usd IS NOT NULL "
        "ORDER BY ts_open", (frm, to))]
    con.close()

    n, w, r, net = wr(rows)
    hdr = f"{'':<22}{'сд':>4}{'вин':>5}{'WR':>5}{'net':>9}"
    print(f"=== sweep_fade {args.frm}→{args.to} | {n} сд, WR {r:.0f}%, net {net:+.2f} ===")

    print("\n-- по стороне --\n" + hdr)
    for s in ("long", "short"):
        line(s, [x for x in rows if x["side"] == s])

    print("\n-- по монете --\n" + hdr)
    syms = {}
    for x in rows:
        syms.setdefault(x["symbol"], []).append(x)
    for s in sorted(syms, key=lambda k: wr(syms[k])[3]):
        line(s, syms[s])

    print("\n-- по score (bucket) --\n" + hdr)
    def sb(v):
        v = v or 0
        return "score<3" if v < 3 else "score 3-4" if v < 4 else "score>=4"
    bk = {}
    for x in rows:
        bk.setdefault(sb(x["score"]), []).append(x)
    for k in ("score<3", "score 3-4", "score>=4"):
        if k in bk:
            line(k, bk[k])

    print("\n-- по набору сигналов (reasons) --\n" + hdr)
    rs = {}
    for x in rows:
        rs.setdefault(x["reasons"] or "?", []).append(x)
    for k in sorted(rs, key=lambda z: wr(rs[z])[3]):
        line(k[:22], rs[k])

    # серийность: вход, у которого предыдущий вход по ЭТОЙ монете был SL за <90 мин
    print("\n-- серийность: вход после недавнего SL по той же монете (<90м) --\n" + hdr)
    last_sl_ts = {}
    fresh, after_sl = [], []
    for x in rows:
        prev = last_sl_ts.get(x["symbol"])
        if prev is not None and (x["ts_open"] - prev) < 90 * 60:
            after_sl.append(x)
        else:
            fresh.append(x)
        if x["close_reason"] == "sl_hit":
            last_sl_ts[x["symbol"]] = x["ts_open"]
    line("свежий вход", fresh, flag_small=False)
    line("после SL <90м", after_sl, flag_small=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
