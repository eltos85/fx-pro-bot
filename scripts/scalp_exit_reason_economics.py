#!/usr/bin/env python3
"""Экономика выходов: сколько приносит каждая причина закрытия.

Вопрос, ради которого написан: у sweep_fade теневой брекет на ТЕХ ЖЕ входах
доходит до цели в 27% случаев, а живая стратегия — в 2.2%. Разницу делает
ранний выход (flow_exit / scratch / time_stop). Скрипт показывает, сколько
сделок уходит по каждой причине и с каким результатом в R, чтобы понять, эти
выходы спасают от убытка или режут будущих победителей.

R-единица — риск сделки в долларах (|entry−SL| × qty).

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt
from statistics import median

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--strategy", default=None)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in UNFILLED)
    where = "AND strategy = ?" if args.strategy else ""
    params: tuple = (args.since, *UNFILLED)
    if args.strategy:
        params += (args.strategy,)
    rows = db.execute(
        f"""SELECT strategy, symbol, close_reason, pnl_usd, fees_usd,
                   abs(entry - sl) * qty AS risk_usd,
                   (ts_close - ts_open) / 60.0 AS held_min
            FROM trades
            WHERE status = 'closed' AND ts_open >= ?
              AND COALESCE(close_reason,'') NOT IN ({ph})
              AND entry > 0 AND sl > 0 AND qty > 0 AND pnl_usd IS NOT NULL
              {where}""", params).fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if not r["risk_usd"]:
            continue
        groups.setdefault(r["close_reason"] or "?", []).append(r)

    total = sum(len(v) for v in groups.values())
    title = args.strategy or "все стратегии"
    print(f"{title}: {total} залитых сделок\n")
    head = (f"{'причина':<22}{'n':>5}{'доля':>7}{'чистR':>9}{'вкладR':>9}"
            f"{'медиана мин':>13}")
    print(head)
    print("-" * len(head))

    rows_out = []
    for reason, g in groups.items():
        n = len(g)
        rs = [t["pnl_usd"] / t["risk_usd"] for t in g]
        mu = sum(rs) / n
        # Вклад — вот что важно для решения: средний R, взвешенный на долю.
        # Причина с плохим средним, но редкая, портит меньше частой и посредственной.
        rows_out.append((mu * n / total, reason, n, mu,
                         median(t["held_min"] for t in g)))
    for contrib, reason, n, mu, held in sorted(rows_out):
        print(f"{reason:<22}{n:>5}{n / total * 100:>6.0f}%{mu:>9.3f}"
              f"{contrib:>9.3f}{held:>13.0f}")

    all_r = [t["pnl_usd"] / t["risk_usd"] for g in groups.values() for t in g]
    mu = sum(all_r) / len(all_r)
    se = sqrt(sum((v - mu) ** 2 for v in all_r) / (len(all_r) - 1) / len(all_r))
    print(f"\nитого чистR {mu:+.3f} 95% CI [{mu - 1.96 * se:+.3f}; "
          f"{mu + 1.96 * se:+.3f}]  (сумма вкладов = среднему)")


if __name__ == "__main__":
    main()
