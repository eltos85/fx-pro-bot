#!/usr/bin/env python3
"""Построчный разбор последних сделок: чем закрылись и на чём потеряли.

Нужен, когда агрегаты показывают аномалию (например ноль побед подряд) и надо
увидеть, это рынок или механика: причина закрытия, символ, сторона, R и
комиссия в R по каждой сделке.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import UTC, datetime


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT id, ts_open, ts_close, symbol, side, strategy, status,
                  entry, sl, tp, exit, qty, pnl_usd, fees_usd, close_reason
           FROM trades WHERE ts_open >= ? ORDER BY ts_open DESC LIMIT ?""",
        (args.since, args.limit)).fetchall()

    head = (f"{'id':>6} {'открыт':<17}{'символ':<13}{'сторона':<8}"
            f"{'стратегия':<16}{'причина':<22}{'мин':>6}{'R':>7}{'комис.R':>9}"
            f"{'net$':>9}")
    print(head)
    print("-" * len(head))
    reasons: Counter[str] = Counter()
    for r in reversed(rows):
        risk = abs(r["entry"] - r["sl"]) * r["qty"] if r["entry"] and r["sl"] else 0.0
        pnl = r["pnl_usd"]
        fee = r["fees_usd"] or 0.0
        rr = f"{pnl / risk:+.2f}" if (risk and pnl is not None) else "—"
        fr = f"{fee / risk:.3f}" if risk else "—"
        held = ((r["ts_close"] - r["ts_open"]) / 60.0
                if r["ts_close"] else float("nan"))
        reason = r["close_reason"] or r["status"]
        reasons[reason] += 1
        print(f"{r['id']:>6} "
              f"{datetime.fromtimestamp(r['ts_open'], UTC).strftime('%m-%d %H:%M:%S'):<17}"
              f"{r['symbol']:<13}{r['side']:<8}{r['strategy'] or '?':<16}"
              f"{reason:<22}{held:>6.0f}{rr:>7}{fr:>9}"
              f"{(pnl if pnl is not None else 0.0):>9.2f}")

    print("\n=== причины закрытия ===")
    for reason, n in reasons.most_common():
        print(f"  {reason:<24}{n:>4}")


if __name__ == "__main__":
    main()
