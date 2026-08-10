#!/usr/bin/env python3
"""История ЗАЛИТЫХ сделок и торгуемой вселенной по дням.

Строки в ``trades`` со статусом `entry_Cancelled`/`entry_timeout` — это
невыставившиеся maker-лимитки, а не сделки: денег по ним не двигалось. Считать
«количество ставок» по всем строкам значит смешивать попытки с исполнениями,
поэтому здесь они разведены.

Вторая половина отчёта — сколько РАЗНЫХ символов реально торговалось в день.
Схлопывание вселенной выглядит одинаково с падением сигналов, но лечится
совершенно иначе, поэтому их надо различать.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in UNFILLED)

    print("=== по дням: попытки, заливы, исходы ===")
    head = (f"{'день':<12}{'попыток':>9}{'залито':>8}{'залив%':>8}"
            f"{'TP':>4}{'SL':>4}{'проч':>6}{'net':>10}{'символов':>10}")
    print(head)
    print("-" * len(head))
    rows = db.execute(
        f"""SELECT date(ts_open, 'unixepoch') AS d,
                   COUNT(*) AS attempts,
                   SUM(COALESCE(close_reason,'') NOT IN ({placeholders})) AS filled,
                   SUM(close_reason = 'tp_hit') AS tp,
                   SUM(close_reason = 'sl_hit') AS sl,
                   SUM(CASE WHEN status='closed' THEN pnl_usd ELSE 0 END) AS pnl,
                   COUNT(DISTINCT CASE
                       WHEN COALESCE(close_reason,'') NOT IN ({placeholders})
                       THEN symbol END) AS syms
            FROM trades
            WHERE ts_open >= strftime('%s', 'now', ?)
            GROUP BY d ORDER BY d""",
        (*UNFILLED, *UNFILLED, f"-{args.days} days")).fetchall()
    for r in rows:
        filled = r["filled"] or 0
        rate = f"{filled / r['attempts'] * 100:.0f}%" if r["attempts"] else "—"
        other = filled - (r["tp"] or 0) - (r["sl"] or 0)
        print(f"{r['d']:<12}{r['attempts']:>9}{filled:>8}{rate:>8}"
              f"{r['tp'] or 0:>4}{r['sl'] or 0:>4}{other:>6}"
              f"{r['pnl']:>10.2f}{r['syms']:>10}")

    print("\n=== какие символы РЕАЛЬНО торговались (заливы) по дням ===")
    rows = db.execute(
        f"""SELECT date(ts_open, 'unixepoch') AS d, strategy AS s,
                   GROUP_CONCAT(DISTINCT symbol) AS syms
            FROM trades
            WHERE ts_open >= strftime('%s', 'now', ?)
              AND COALESCE(close_reason,'') NOT IN ({placeholders})
            GROUP BY d, s ORDER BY d, s""",
        (f"-{args.days} days", *UNFILLED)).fetchall()
    for r in rows:
        print(f"{r['d']}  {r['s'] or '?':<17}{r['syms']}")


if __name__ == "__main__":
    main()
