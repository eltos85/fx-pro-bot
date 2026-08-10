#!/usr/bin/env python3
"""Заливы по стратегиям до и после рубежа: отделить эффект правки от рынка.

Считает ПОПЫТКИ и ЗАЛИВЫ раздельно (невыставившаяся maker-лимитка — не сделка)
и нормирует на сутки, потому что периоды до и после рубежа разной длины.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")
STRATEGIES = ("sweep_fade", "density_break", "density_bounce",
              "sweep_fade_canon")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--cutoff", type=float, required=True)
    ap.add_argument("--days", type=float, default=10.0)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    now = db.execute("SELECT MAX(ts_open) AS m FROM trades").fetchone()["m"]
    since = args.cutoff - args.days * 86400.0
    ph = ",".join("?" for _ in UNFILLED)

    head = (f"{'стратегия':<18}{'период':<8}{'попыток':>9}{'залито':>8}"
            f"{'залив%':>8}{'зал/сут':>9}{'TP':>4}{'SL':>4}{'net':>10}")
    print(head)
    print("-" * len(head))
    for strat in STRATEGIES:
        printed = False
        for label, lo, hi in (("до", since, args.cutoff),
                              ("после", args.cutoff, now + 1)):
            hours = max((hi - lo) / 3600.0, 1e-9)
            r = db.execute(
                f"""SELECT COUNT(*) AS a,
                           SUM(COALESCE(close_reason,'') NOT IN ({ph})) AS f,
                           SUM(close_reason = 'tp_hit') AS tp,
                           SUM(close_reason = 'sl_hit') AS sl,
                           SUM(CASE WHEN status = 'closed'
                                    THEN pnl_usd ELSE 0 END) AS pnl
                    FROM trades
                    WHERE strategy = ? AND ts_open >= ? AND ts_open < ?""",
                (*UNFILLED, strat, lo, hi)).fetchone()
            if not r["a"]:
                continue
            filled = r["f"] or 0
            print(f"{strat:<18}{label:<8}{r['a']:>9}{filled:>8}"
                  f"{filled / r['a'] * 100:>7.0f}%{filled / hours * 24:>9.1f}"
                  f"{r['tp'] or 0:>4}{r['sl'] or 0:>4}{r['pnl']:>10.2f}")
            printed = True
        if printed:
            print()


if __name__ == "__main__":
    main()
