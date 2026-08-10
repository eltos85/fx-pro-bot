#!/usr/bin/env python3
"""Валовой и чистый результат стратегий с честной пометкой покрытия комиссии.

Ловушка, ради которой написан: `fees_usd` заполняется не у всех сделок (maker-
входы часто приходят с нулём), поэтому «валовой = чистый + комиссия» по сырой
телеметрии занижает валовой там, где комиссия не записалась. Сравнивать так две
стратегии с РАЗНЫМ типом входа нельзя: у taker-стратегии покрытие полное, у
maker-стратегии дырявое, и разница в «валовом» окажется артефактом учёта.

Поэтому считаем два варианта: по факту (там, где комиссия записана) и по
модели (ставка выводится из типа входа стратегии и ширины стопа). Рядом
печатается покрытие, чтобы было видно, какому числу можно верить.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt

from scalp_bot.analysis.fees import STANDARD_MAKER_FEE, STANDARD_TAKER_FEE

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")

# Тип входа по стратегии (см. settings: *_entry_order_type). Выход всегда
# taker: биржевые SL/TP и flow_exit исполняются по рынку.
ENTRY_IS_MAKER = {
    "sweep_fade": True,
    "density_bounce": False,
    "density_break": False,
    "sweep_fade_canon": False,
}


def mean_ci(v: list[float]) -> tuple[float, float, float]:
    n = len(v)
    mu = sum(v) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    se = sqrt(sum((x - mu) ** 2 for x in v) / (n - 1) / n)
    return mu, mu - 1.96 * se, mu + 1.96 * se


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in UNFILLED)
    rows = db.execute(
        f"""SELECT strategy, entry, sl, qty, pnl_usd, fees_usd
            FROM trades
            WHERE status = 'closed' AND ts_open >= ?
              AND COALESCE(close_reason,'') NOT IN ({ph})
              AND entry > 0 AND sl > 0 AND qty > 0 AND pnl_usd IS NOT NULL""",
        (args.since, *UNFILLED)).fetchall()

    by: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by.setdefault(r["strategy"] or "?", []).append(r)

    head = (f"{'стратегия':<18}{'n':>5}{'покрытие':>10}{'чистR':>9}"
            f"{'ком.факт':>10}{'ком.модель':>12}{'валR модель':>13}{'95% CI':>20}")
    print(head)
    print("-" * len(head))
    for name in sorted(by):
        trades = [t for t in by[name] if abs(t["entry"] - t["sl"]) * t["qty"] > 0]
        if not trades:
            continue
        n = len(trades)
        net, fee_fact, fee_model, gross = [], [], [], []
        covered = 0
        maker = ENTRY_IS_MAKER.get(name, False)
        rate = (STANDARD_MAKER_FEE if maker else STANDARD_TAKER_FEE) \
            + STANDARD_TAKER_FEE
        for t in trades:
            risk = abs(t["entry"] - t["sl"]) * t["qty"]
            net.append(t["pnl_usd"] / risk)
            if t["fees_usd"]:
                covered += 1
                fee_fact.append(t["fees_usd"] / risk)
            # Модель: комиссия в R = ставка round-trip / (ширина стопа в долях).
            sl_frac = abs(t["entry"] - t["sl"]) / t["entry"]
            fm = rate / sl_frac if sl_frac > 0 else 0.0
            fee_model.append(fm)
            gross.append(t["pnl_usd"] / risk + fm)
        mu_net = sum(net) / n
        mu_ff = sum(fee_fact) / len(fee_fact) if fee_fact else float("nan")
        mu_fm = sum(fee_model) / n
        g, lo, hi = mean_ci(gross)
        print(f"{name:<18}{n:>5}{covered / n * 100:>9.0f}%{mu_net:>9.3f}"
              f"{mu_ff:>10.3f}{mu_fm:>12.3f}{g:>13.3f}"
              f"{f'[{lo:+.3f}; {hi:+.3f}]':>20}")

    print("\nМодель ставки: maker-вход 0.02% + taker-выход 0.055% для sweep_fade;")
    print("taker обе ноги 0.11% для остальных. Двойные тарифы модель не знает,")
    print("поэтому у стратегий, торговавших BANK/ESPORTS, валовой чуть занижен.")


if __name__ == "__main__":
    main()
