#!/usr/bin/env python3
"""Результат стратегий в R с доверительным интервалом — под пороги отключения.

`sample-size.mdc` требует для отключения одновременно: ≥100 сделок, ≥2 недели,
p<0.05 и величину эффекта ≥0.3R. Скрипт считает все четыре величины, чтобы
решение принималось по порогам, а не по последней серии убытков.

R-единица — риск сделки в долларах (|entry−SL| × qty), а не фиксированные $10:
при risk-based sizing лот пересчитывается под ширину стопа, и делить на
константу значило бы сравнивать разные единицы.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    """Среднее и 95% CI по нормальной аппроксимации (n здесь всегда велико).

    CI НЕ учитывает кластеризацию по символо-дням, поэтому он оптимистичен:
    сделки одного дня по одному символу коррелированы. Для решения об
    отключении это консервативно в опасную сторону, о чём и предупреждаем.
    """
    n = len(values)
    if n < 2:
        return (values[0] if values else 0.0, float("nan"), float("nan"))
    mu = sum(values) / n
    var = sum((v - mu) ** 2 for v in values) / (n - 1)
    se = sqrt(var / n)
    return (mu, mu - 1.96 * se, mu + 1.96 * se)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in UNFILLED)
    rows = db.execute(
        f"""SELECT strategy, symbol, ts_open, close_reason, pnl_usd, fees_usd,
                   abs(entry - sl) * qty AS risk_usd
            FROM trades
            WHERE status = 'closed' AND ts_open >= ?
              AND COALESCE(close_reason,'') NOT IN ({ph})
              AND entry > 0 AND sl > 0 AND qty > 0 AND pnl_usd IS NOT NULL""",
        (args.since, *UNFILLED)).fetchall()

    by: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by.setdefault(r["strategy"] or "?", []).append(r)

    head = (f"{'стратегия':<18}{'n':>5}{'дней':>7}{'TP%':>7}{'SL%':>7}"
            f"{'чистR':>9}{'95% CI':>20}{'вердикт':>12}")
    print(head)
    print("-" * len(head))
    for name in sorted(by):
        trades = [t for t in by[name] if t["risk_usd"]]
        if not trades:
            continue
        n = len(trades)
        net = [t["pnl_usd"] / t["risk_usd"] for t in trades]
        mu, lo, hi = mean_ci(net)
        span = (max(t["ts_open"] for t in trades)
                - min(t["ts_open"] for t in trades)) / 86400.0
        tp = sum(1 for t in trades if t["close_reason"] == "tp_hit") / n * 100
        sl = sum(1 for t in trades if t["close_reason"] == "sl_hit") / n * 100
        # Все четыре порога sample-size.mdc сразу.
        ok = (n >= 100 and span >= 14 and hi < 0 and abs(mu) >= 0.3)
        verdict = "порог взят" if ok else "не дотягивает"
        print(f"{name:<18}{n:>5}{span:>7.1f}{tp:>7.1f}{sl:>7.1f}{mu:>9.3f}"
              f"{f'[{lo:+.3f}; {hi:+.3f}]':>20}{verdict:>12}")

    print("\nПорог отключения: n≥100 И дней≥14 И CI не включает ноль И |R|≥0.3.")
    print("CI не учитывает кластеризацию по символо-дням — он оптимистичен.")


if __name__ == "__main__":
    main()
