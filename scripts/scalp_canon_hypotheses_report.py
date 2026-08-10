#!/usr/bin/env python3
"""Две канон-гипотезы sweep_fade_canon на теневых исходах (read-only).

Обе проверяются на `canon_rejection_shadow`: канон эмитит теневого кандидата
на КАЖДЫЙ свой сигнал ДО основных гейтов (main.py, «Evidence-first shadows
дренируются ДО resolve/main gates»), поэтому тень видит и те сетапы, которые
в бою зарезаны. Живая торговля при этом не меняется.

Гипотеза A — ЯКОРЬ ВОЗВРАТА. Канон Turtle Soup входит, когда цена вернулась
ЗА свипнутый уровень. В коде цель возврата считается от микро-экстремума, а
не от ключевого уровня, поэтому часть входов происходит, пока цена ещё за
уровнем. Делим исходы по тому, где оказалась цена входа относительно уровня.

Гипотеза B — ГЕЙТ ADX. Взятие дневного уровня импульсно по построению, из-за
чего у канона медиана ADX выше, чем у базы, и гейт режет большинство его
сигналов. Делим исходы по ADX на момент рождения кандидата: если группа
ADX≥порога не хуже, гейт отрезает годные сетапы.

Методика R повторяет `scalp_sl_widen_report.py`: барьер TP → +tp_r, барьер SL
→ −1, ни один барьер за горизонт → переоценка по последней цене (выбрасывать
этот случай нельзя, он даёт смещение). Комиссия в R = ставка / ширина стопа.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt

SETUP = "canon_rejection_shadow"
# Round-trip taker Bybit: 0.055% вход + 0.055% выход. Канон входит по рынку.
DEFAULT_FEE_PCT = 0.11


def realised_r(row: sqlite3.Row) -> float | None:
    risk = float(row["risk"] or 0.0)
    entry = float(row["entry"] or 0.0)
    if risk <= 0 or entry <= 0:
        return None
    if row["outcome_tp"] == "tp":
        return abs(float(row["tp"]) - entry) / risk
    if row["outcome_tp"] == "sl":
        return -1.0
    last = row["last_price"]
    if last is None:
        return None
    sign = 1.0 if row["side"] == "long" else -1.0
    return sign * (float(last) - entry) / risk


def mean_se(v: list[float]) -> tuple[float, float]:
    n = len(v)
    mu = sum(v) / n
    if n < 2:
        return mu, float("nan")
    return mu, sqrt(sum((x - mu) ** 2 for x in v) / (n - 1) / n)


def describe(name: str, rows: list[sqlite3.Row], fee_pct: float) -> dict | None:
    if not rows:
        return None
    r, sl_pcts, tps = [], [], 0
    for row in rows:
        val = realised_r(row)
        if val is not None:
            r.append(val)
        risk, entry = float(row["risk"] or 0), float(row["entry"] or 0)
        if risk > 0 and entry > 0:
            sl_pcts.append(100.0 * risk / entry)
        if row["outcome_tp"] == "tp":
            tps += 1
    if not r:
        return None
    sl_pct = sum(sl_pcts) / len(sl_pcts) if sl_pcts else 0.0
    fee_r = fee_pct / sl_pct if sl_pct > 0 else 0.0
    gross, se = mean_se(r)
    return {"name": name, "n": len(rows), "r": r, "tp_pct": tps / len(rows) * 100,
            "sl_pct": sl_pct, "fee_r": fee_r, "gross": gross,
            "net": gross - fee_r, "se": se}


def report(title: str, note: str, groups: list[dict | None]) -> None:
    groups = [g for g in groups if g]
    print(f"\n=== {title} ===")
    print(note)
    if not groups:
        print("нет досмотренных исходов")
        return
    hdr = (f"{'группа':<26}{'n':>6}{'TP%':>7}{'стоп%':>8}"
           f"{'ком.R':>8}{'валR':>9}{'чистR':>9}{'95% CI чист':>22}")
    print(hdr)
    print("-" * len(hdr))
    for g in groups:
        lo, hi = g["net"] - 1.96 * g["se"], g["net"] + 1.96 * g["se"]
        ci = f"[{lo:+.3f}; {hi:+.3f}]" if g["n"] > 1 else "—"
        print(f"{g['name']:<26}{g['n']:>6}{g['tp_pct']:>6.1f}%{g['sl_pct']:>8.3f}"
              f"{g['fee_r']:>8.3f}{g['gross']:>9.3f}{g['net']:>9.3f}{ci:>22}")
    if len(groups) == 2:
        a, b = groups
        diff = a["net"] - b["net"]
        se = sqrt(a["se"] ** 2 + b["se"] ** 2)
        lo, hi = diff - 1.96 * se, diff + 1.96 * se
        verdict = ("различие статистически значимо"
                   if lo > 0 or hi < 0 else "интервал включает ноль — различия нет")
        print(f"\nразница «{a['name']}» − «{b['name']}»: {diff:+.3f}R "
              f"[{lo:+.3f}; {hi:+.3f}] — {verdict}")
        need = min(a["n"], b["n"])
        if need < 100:
            print(f"выборка меньшей группы {need} из 100 — решение принимать рано")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT)
    ap.add_argument("--adx-max", type=float, default=30.0,
                    help="боевой порог гейта htf_adx_max")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT side, entry, sl, tp, risk, last_price, outcome_tp, level_price,
                  level_type, regime_adx, ts_candidate
           FROM counterfactual_setups
           WHERE setup_type = ? AND state = 'final' AND ts_candidate >= ?""",
        (SETUP, args.since)).fetchall()
    print(f"canon_rejection_shadow: {len(rows)} досмотренных кандидатов "
          f"(комиссия {args.fee_pct}% round-trip)")

    # ─── A. Якорь возврата ───────────────────────────────────────────────────
    back, still_out = [], []
    for row in rows:
        lvl = row["level_price"]
        if not lvl or not row["entry"]:
            continue
        canonical = (row["entry"] >= lvl if row["side"] == "long"
                     else row["entry"] <= lvl)
        (back if canonical else still_out).append(row)
    report(
        "A. Якорь возврата: вернулась ли цена за уровень к моменту входа",
        "Канон входит только после возврата ЗА уровень. Если группа «возврат\n"
        "состоялся» лучше — текущий якорь (микро-экстремум) отбирает сетапы хуже.",
        [describe("возврат состоялся", back, args.fee_pct),
         describe("цена ещё за уровнем", still_out, args.fee_pct)])

    # ─── B. Гейт ADX ─────────────────────────────────────────────────────────
    with_adx = [r for r in rows if r["regime_adx"] is not None]
    print(f"\nADX проставлен у {len(with_adx)} из {len(rows)} кандидатов "
          f"(поле добавлено в v0.18.55, копится только на новых)")
    strong = [r for r in with_adx if r["regime_adx"] >= args.adx_max]
    calm = [r for r in with_adx if r["regime_adx"] < args.adx_max]
    report(
        f"B. Гейт ADX: сетапы при ADX≥{args.adx_max:.0f} против остальных",
        "В бою группа «сильный тренд» ЗАРЕЗАНА гейтом. Если она не хуже\n"
        "спокойной — гейт отрезает годные сетапы и его стоит пересмотреть.",
        [describe(f"ADX≥{args.adx_max:.0f} (режется)", strong, args.fee_pct),
         describe(f"ADX<{args.adx_max:.0f} (торгуется)", calm, args.fee_pct)])

    # ─── справка: разбивка по типу уровня ────────────────────────────────────
    print("\n=== Справка: исходы по типу свипнутого уровня ===")
    by: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by.setdefault(row["level_type"] or "?", []).append(row)
    hdr = f"{'уровень':<16}{'n':>7}{'TP%':>7}{'валR':>9}{'чистR':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name in sorted(by, key=lambda k: -len(by[k])):
        g = describe(name, by[name], args.fee_pct)
        if g:
            print(f"{name:<16}{g['n']:>7}{g['tp_pct']:>6.1f}%"
                  f"{g['gross']:>9.3f}{g['net']:>9.3f}")


if __name__ == "__main__":
    main()
