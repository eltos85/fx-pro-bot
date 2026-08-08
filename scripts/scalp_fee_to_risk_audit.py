#!/usr/bin/env python3
"""Аудит инварианта «комиссия ≤ 1/min_risk_fee_mult доля R» по фактам.

Код заявляет инвариант (settings.min_risk_fee_mult): пол ширины стопа равен
``min_risk_fee_mult × round_trip_fee_frac × entry``, откуда комиссия должна
составлять не более ``1/mult`` риска. При mult=4 это ≤0.25R.

Инвариант держится только если ``round_trip_fee_frac`` равен ФАКТИЧЕСКОЙ
стоимости round-trip. Скрипт измеряет по закрытым сделкам, чему она равна на
самом деле — раздельно по стратегиям, потому что тип входа у них разный
(maker-лимитка против market), а ставки maker и taker различаются втрое.

Только чтение. Выводит на каждую стратегию: ширину стопа в долях цены,
фактическую ставку round-trip, комиссию в R и валовой/чистый R.
"""

from __future__ import annotations

import argparse
import sqlite3
from statistics import median


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True,
                    help="epoch-секунды: считать сделки, закрытые позже")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT strategy, symbol, entry, sl, qty, pnl_usd, fees_usd, close_reason
           FROM trades
           WHERE status = 'closed' AND ts_close >= ?
             AND entry > 0 AND sl > 0 AND qty > 0
             AND pnl_usd IS NOT NULL AND fees_usd IS NOT NULL""",
        (args.since,),
    ).fetchall()

    by_strat: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_strat.setdefault(r["strategy"] or "?", []).append(r)

    print(f"закрытых сделок с телеметрией комиссии: {len(rows)}\n")
    head = (f"{'стратегия':<20}{'n':>5}{'стоп,%':>9}{'ставка,%':>10}"
            f"{'fee,R':>8}{'валR':>8}{'чистR':>8}")
    print(head)
    print("-" * len(head))

    for name in sorted(by_strat):
        trades = by_strat[name]
        sl_fracs, fee_rates, fee_rs, gross_rs, net_rs = [], [], [], [], []
        for r in trades:
            # R в долларах = размер позиции × дистанция стопа. Это та же
            # величина, от которой считается риск на сделку при risk-sizing.
            risk_usd = abs(r["entry"] - r["sl"]) * r["qty"]
            if risk_usd <= 0:
                continue
            notional = r["entry"] * r["qty"]
            sl_fracs.append(abs(r["entry"] - r["sl"]) / r["entry"])
            # Ставка round-trip = комиссия / оборот одной стороны. Приближение:
            # объём входа и выхода близки, поэтому делим на входной notional.
            fee_rates.append(r["fees_usd"] / notional)
            fee_rs.append(r["fees_usd"] / risk_usd)
            # pnl_usd в БД — уже чистый (сверено с выпиской Bybit), поэтому
            # валовой = чистый + комиссия.
            net_rs.append(r["pnl_usd"] / risk_usd)
            gross_rs.append((r["pnl_usd"] + r["fees_usd"]) / risk_usd)

        if not fee_rs:
            continue
        n = len(fee_rs)
        print(f"{name:<20}{n:>5}{median(sl_fracs) * 100:>9.3f}"
              f"{median(fee_rates) * 100:>10.4f}{sum(fee_rs) / n:>8.3f}"
              f"{sum(gross_rs) / n:>8.3f}{sum(net_rs) / n:>8.3f}")

    print("\nЗаявленный инвариант при min_risk_fee_mult=4: fee ≤ 0.250R")


if __name__ == "__main__":
    main()
