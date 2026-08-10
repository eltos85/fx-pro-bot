#!/usr/bin/env python3
"""Сравнение торговли до и после конкретного деплоя: объём и результат.

Нужен, чтобы отвечать на вопрос «после правок стало хуже?» числами, а не
впечатлением: считает сделки по дням и стратегиям, а также разбирает, на каких
гейтах сигналы отсекаются (``shadow_signals``) по обе стороны от рубежа.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def per_day(db: sqlite3.Connection, since: float) -> None:
    print("=== сделки по дням и стратегиям (по времени ОТКРЫТИЯ, UTC) ===")
    head = (f"{'день':<12}{'стратегия':<17}{'откр':>5}{'закр':>6}{'WR':>7}"
            f"{'net':>10}")
    print(head)
    print("-" * len(head))
    rows = db.execute(
        """SELECT date(ts_open, 'unixepoch') AS d, strategy AS s,
                  COUNT(*) AS n,
                  SUM(status = 'closed') AS cl,
                  SUM(CASE WHEN status = 'closed' THEN pnl_usd ELSE 0 END) AS pnl,
                  SUM(CASE WHEN status = 'closed' AND pnl_usd > 0
                           THEN 1 ELSE 0 END) AS w
           FROM trades WHERE ts_open >= ?
           GROUP BY d, s ORDER BY d, s""", (since,)).fetchall()
    for r in rows:
        wr = f"{r['w'] / r['cl'] * 100:.0f}%" if r["cl"] else "—"
        print(f"{r['d']:<12}{r['s'] or '?':<17}{r['n']:>5}{r['cl']:>6}{wr:>7}"
              f"{r['pnl']:>10.2f}")


def per_day_total(db: sqlite3.Connection, since: float) -> None:
    print("\n=== итого по дням ===")
    for r in db.execute(
        """SELECT date(ts_open, 'unixepoch') AS d, COUNT(*) AS n,
                  SUM(CASE WHEN status = 'closed' THEN pnl_usd ELSE 0 END) AS pnl
           FROM trades WHERE ts_open >= ? GROUP BY d ORDER BY d""",
            (since,)).fetchall():
        print(f"{r['d']}  сделок={r['n']:<4} net={r['pnl']:.2f}")


def gates(db: sqlite3.Connection, cutoff: float, since: float) -> None:
    """На каких гейтах режутся сигналы — до и после рубежа.

    Если объём упал из-за новой правки, это видно как рост доли конкретного
    гейта, а не как равномерное падение всего funnel-а.
    """
    print("\n=== отсев по гейтам (shadow_signals), до | после рубежа ===")
    try:
        rows = db.execute(
            """SELECT blocked_by AS gate,
                      SUM(ts < :cut) AS before_n, SUM(ts >= :cut) AS after_n
               FROM shadow_signals WHERE ts >= :since
               GROUP BY blocked_by ORDER BY after_n DESC""",
            {"cut": cutoff, "since": since}).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"нет данных: {exc}")
        return
    # Периоды разной длины, поэтому сравнивать надо ИНТЕНСИВНОСТЬ, а не суммы.
    now = db.execute("SELECT MAX(ts) AS m FROM shadow_signals").fetchone()["m"]
    h_before = max((cutoff - since) / 3600.0, 1e-9)
    h_after = max((now - cutoff) / 3600.0, 1e-9)
    head = f"{'гейт':<20}{'до':>8}{'после':>8}{'до/сут':>9}{'после/сут':>11}"
    print(head)
    print("-" * len(head))
    for r in rows:
        b, a = r["before_n"] or 0, r["after_n"] or 0
        print(f"{r['gate'] or '?':<20}{b:>8}{a:>8}"
              f"{b / h_before * 24:>9.1f}{a / h_after * 24:>11.1f}")


def split(db: sqlite3.Connection, cutoff: float, since: float) -> None:
    print(f"\n=== агрегат до | после {fmt_ts(cutoff)} UTC ===")
    head = f"{'период':<10}{'часов':>7}{'сделок':>8}{'в сутки':>9}{'net':>10}"
    print(head)
    print("-" * len(head))
    now = db.execute("SELECT MAX(ts_open) AS m FROM trades").fetchone()["m"]
    for label, lo, hi in (("до", since, cutoff), ("после", cutoff, now + 1)):
        r = db.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN status = 'closed' THEN pnl_usd ELSE 0 END) AS pnl
               FROM trades WHERE ts_open >= ? AND ts_open < ?""",
            (lo, hi)).fetchone()
        hours = (hi - lo) / 3600.0
        rate = r["n"] / hours * 24.0 if hours > 0 else 0.0
        print(f"{label:<10}{hours:>7.1f}{r['n']:>8}{rate:>9.1f}"
              f"{(r['pnl'] or 0.0):>10.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--cutoff", type=float, required=True,
                    help="epoch-секунды рубежа (момент деплоя)")
    ap.add_argument("--days", type=float, default=9.0)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    since = args.cutoff - args.days * 86400.0
    per_day(db, since)
    per_day_total(db, since)
    split(db, args.cutoff, since)
    gates(db, args.cutoff, since)


if __name__ == "__main__":
    main()
