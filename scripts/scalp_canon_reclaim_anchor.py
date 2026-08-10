#!/usr/bin/env python3
"""Проверка якоря «полного возврата» у sweep_fade_canon.

Канон CAP Rule 2 / Turtle Soup: возврат считается ЗА СВИПНУТЫЙ ЗНАЧИМЫЙ
УРОВЕНЬ (для канона это pdh/pdl/дневной экстремум). Вход Turtle Soup —
стоп-ордер за предыдущим экстремумом, то есть сделка открывается, только
когда цена вернулась ВНУТРЬ прежнего диапазона.

Реализация же наследует якорь от базового sweep_fade: цель возврата =
``swept + reclaim_frac × (prior − swept)`` = ``prior``, где ``prior`` —
экстремум 3-минутного микро-окна, а НЕ ключевой уровень. Если микро-экстремум
лежит по ту же сторону от ключевого уровня, что и свип, то вход происходит,
пока цена всё ещё за уровнем — возврата в канонном смысле не было.

Скрипт меряет, как часто это случается, и сравнивает результат двух групп.
Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def mean_ci(v: list[float]) -> tuple[float, float, float]:
    n = len(v)
    if not n:
        return float("nan"), float("nan"), float("nan")
    mu = sum(v) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    se = sqrt(sum((x - mu) ** 2 for x in v) / (n - 1) / n)
    return mu, mu - 1.96 * se, mu + 1.96 * se


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--strategy", default="sweep_fade_canon")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in UNFILLED)
    rows = db.execute(
        f"""SELECT t.side, t.entry, t.sl, t.qty, t.pnl_usd,
                   f.level_type, f.level_price, f.prior_price, f.swept_price
            FROM setup_features f JOIN trades t ON t.id = f.trade_id
            WHERE f.strategy = ? AND t.status = 'closed' AND t.ts_open >= ?
              AND COALESCE(t.close_reason,'') NOT IN ({ph})
              AND f.level_price > 0 AND f.prior_price > 0 AND f.swept_price > 0
              AND t.entry > 0 AND t.sl > 0 AND t.qty > 0
              AND t.pnl_usd IS NOT NULL""",
        (args.strategy, args.since, *UNFILLED)).fetchall()

    if not rows:
        print("нет сделок с полной геометрией уровня")
        return

    print(f"{args.strategy}: {len(rows)} сделок с записанной геометрией\n")

    真 = []   # цель возврата ЗА уровнем — канонный возврат
    ложь = []  # цель возврата НЕ доходит до уровня
    depth_level, depth_prior = [], []
    for r in rows:
        lvl, prior, swept = r["level_price"], r["prior_price"], r["swept_price"]
        rr = r["pnl_usd"] / (abs(r["entry"] - r["sl"]) * r["qty"]) \
            if abs(r["entry"] - r["sl"]) * r["qty"] > 0 else None
        # long: свип вниз, канонный возврат = цена снова ВЫШЕ уровня
        canonical = prior >= lvl if r["side"] == "long" else prior <= lvl
        if rr is not None:
            (真 if canonical else ложь).append(rr)
        depth_level.append(abs(lvl - swept) / lvl * 1e4)
        depth_prior.append(abs(prior - swept) / prior * 1e4)

    n_true, n_false = len(真), len(ложь)
    tot = n_true + n_false
    print("=== Куда целится «полный возврат» ===")
    print(f"цель возврата ЗА ключевым уровнем (канон):   {n_true:>4} "
          f"({n_true / tot * 100:.0f}%)")
    print(f"цель возврата НЕ достаёт до уровня:          {n_false:>4} "
          f"({n_false / tot * 100:.0f}%)")
    print("\nВо второй группе сделка открывается, пока цена ещё ЗА уровнем:")
    print("возврата в канонном смысле не произошло.\n")
    for label, v in (("канонный возврат", 真), ("возврата не было", ложь)):
        if not v:
            continue
        mu, lo, hi = mean_ci(v)
        ci = f"[{lo:+.3f}; {hi:+.3f}]" if len(v) > 1 else "—"
        print(f"{label:<20}n={len(v):<5}чистR {mu:+.3f}  {ci}")

    print("\n=== Глубина прокола: за уровень против за микро-экстремум ===")
    for label, v in (("за ключевой уровень", depth_level),
                     ("за микро-экстремум", depth_prior)):
        v = sorted(v)
        print(f"{label:<22}медиана {v[len(v) // 2]:>7.1f} bps"
              f"  =  {v[len(v) // 2] / 100:.3f}% цены")
    print("\nКанон: «чем глубже прокол, тем лучше сетап» (Connors/Raschke).")
    print("Стоп-пол 0.30% цены = 30 bps — сравните с медианами выше.")


if __name__ == "__main__":
    main()
