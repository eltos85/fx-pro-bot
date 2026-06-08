"""Разбор sweep_fade: почему лузил 5-7 июня. По дням, по сессиям, по вин/луз.

Окно с отсечкой ПОСЛЕ деплоя v0.18.10 (5 июня 11:00 UTC) — чистая конфигурация.
Read-only.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime


SESS = [("Азия 00-07", 0, 7), ("Лондон 07-12", 7, 12),
        ("Лонд+NY 12-16", 12, 16), ("NY 16-21", 16, 21), ("вечер 21-24", 21, 24)]


def _stat(items):
    n = len(items)
    wins = [x["pnl_usd"] for x in items if x["pnl_usd"] > 0]
    loss = [x["pnl_usd"] for x in items if x["pnl_usd"] <= 0]
    net = sum(x["pnl_usd"] for x in items)
    wr = 100 * len(wins) / n if n else 0
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(loss) / len(loss) if loss else 0
    return n, len(wins), wr, net, aw, al


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-06-05T11:00")
    p.add_argument("--to", default="2026-06-08T00:00")
    p.add_argument("--strategy", default="sweep_fade")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()

    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,pnl_usd,close_reason,strategy,ts_open,ts_close,status "
        "FROM trades WHERE ts_open>=? AND ts_open<? AND strategy=?",
        (frm, to, args.strategy))]
    con.close()
    real = [r for r in rows if r["status"] == "closed"
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["close_reason"] != "restart_flat" and r["pnl_usd"] is not None]

    n, w, wr, net, aw, al = _stat(real)
    print(f"=== {args.strategy}  {args.frm} → {args.to} (UTC) ===")
    print(f"сделок {n} | WR {wr:.0f}% | net {net:.2f} | ср.вин {aw:.2f} | ср.луз {al:.2f}")
    pf = (aw * w) / abs(al * (n - w)) if (al and n - w) else 0
    print(f"профит-фактор {pf:.2f}  (нужно >1; вин×кол vs луз×кол)")

    print("\n-- по дням --")
    days = {}
    for r in real:
        d = datetime.fromtimestamp(r["ts_close"] or r["ts_open"], UTC).strftime("%m-%d")
        days.setdefault(d, []).append(r)
    print(f"{'день':<7}{'сд':>4}{'WR':>5}{'net':>9}{'ср.вин':>8}{'ср.луз':>8}")
    for d in sorted(days):
        n, w, wr, net, aw, al = _stat(days[d])
        print(f"{d:<7}{n:>4}{wr:>4.0f}%{net:>9.2f}{aw:>8.2f}{al:>8.2f}")

    print("\n-- по сессиям (час входа UTC) --")
    print(f"{'сессия':<16}{'сд':>4}{'WR':>5}{'net':>9}")
    for name, h0, h1 in SESS:
        bucket = [r for r in real
                  if h0 <= datetime.fromtimestamp(r["ts_open"], UTC).hour < h1]
        n, w, wr, net, aw, al = _stat(bucket)
        print(f"{name:<16}{n:>4}{wr:>4.0f}%{net:>9.2f}")

    print("\n-- по причине закрытия --")
    rs = {}
    for r in real:
        rs.setdefault(r["close_reason"] or "?", []).append(r)
    print(f"{'reason':<14}{'сд':>4}{'WR':>5}{'net':>9}")
    for k in sorted(rs, key=lambda x: _stat(rs[x])[3]):
        n, w, wr, net, aw, al = _stat(rs[k])
        print(f"{k:<14}{n:>4}{wr:>4.0f}%{net:>9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
