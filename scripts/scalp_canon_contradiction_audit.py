#!/usr/bin/env python3
"""Аудит sweep_fade_canon: сверка реализации с каноном Turtle Soup.

Канон (Connors/Raschke «Street Smarts» 1996, глава Turtle Soup) требует:
  1. Экстремум 20-ДНЕВНЫЙ (не вчерашний и не внутридневной).
  2. Предыдущий такой же экстремум — минимум за 4 торговые сессии до текущего
     («Important!» в оригинале): фильтр против шума.
  3. Вход стоп-ордером за предыдущий экстремум = вход ПО возврату.
  4. Стоп — сразу ЗА сегодняшним экстремумом (структурный).
  5. Сопровождение трейлингом; сделки живут от часов до дней.

Скрипт меряет, что из этого выполняется на живых сделках, и раскладывает
результат по типу уровня. Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from math import sqrt

UNFILLED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")
DAY_SEC = 86_400.0


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
        f"""SELECT t.id, t.entry, t.sl, t.tp, t.qty, t.pnl_usd, t.close_reason,
                   t.ts_open, t.ts_close,
                   f.level_type, f.level_age_sec, f.level_touches,
                   f.sweep_depth_bps, f.reclaim_duration_sec
            FROM trades t LEFT JOIN setup_features f ON f.trade_id = t.id
            WHERE t.strategy = ? AND t.status = 'closed' AND t.ts_open >= ?
              AND COALESCE(t.close_reason,'') NOT IN ({ph})
              AND t.entry > 0 AND t.sl > 0 AND t.qty > 0
              AND t.pnl_usd IS NOT NULL""",
        (args.strategy, args.since, *UNFILLED)).fetchall()

    if not rows:
        print("нет сделок")
        return
    print(f"{args.strategy}: {len(rows)} залитых сделок\n")

    # ─── 1. Какой уровень фейдим: вчерашний (канон-подобный) или сегодняшний ──
    print("=== 1. Тип свипнутого уровня ===")
    print("Канон Turtle Soup фейдит 20-ДНЕВНЫЙ экстремум. pdh/pdl — вчерашний")
    print("(1 день), day_high/day_low — БЕГУЩИЙ экстремум сегодняшнего дня.\n")
    hdr = f"{'уровень':<14}{'n':>5}{'доля':>7}{'чистR':>9}{'95% CI':>22}{'ср.возраст':>12}{'касаний':>9}"
    print(hdr)
    print("-" * len(hdr))
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["level_type"] or "нет данных", []).append(r)
    for name in sorted(groups, key=lambda k: -len(groups[k])):
        g = groups[name]
        rr = [r["pnl_usd"] / (abs(r["entry"] - r["sl"]) * r["qty"]) for r in g
              if abs(r["entry"] - r["sl"]) * r["qty"] > 0]
        mu, lo, hi = mean_ci(rr)
        ages = [r["level_age_sec"] for r in g if r["level_age_sec"] is not None]
        tch = [r["level_touches"] for r in g if r["level_touches"] is not None]
        age_s = f"{sum(ages) / len(ages) / 3600:.1f}ч" if ages else "—"
        tch_s = f"{sum(tch) / len(tch):.1f}" if tch else "—"
        ci = f"[{lo:+.3f}; {hi:+.3f}]" if rr and len(rr) > 1 else "—"
        print(f"{name:<14}{len(g):>5}{len(g) / len(rows) * 100:>6.0f}%"
              f"{mu:>9.3f}{ci:>22}{age_s:>12}{tch_s:>9}")

    # ─── 2. Правило четырёх сессий ───────────────────────────────────────────
    print("\n=== 2. Правило «предыдущий экстремум ≥4 сессий назад» ===")
    ages = [r["level_age_sec"] for r in rows if r["level_age_sec"] is not None]
    if ages:
        ages.sort()
        ok4 = sum(1 for a in ages if a >= 4 * DAY_SEC)
        ok1 = sum(1 for a in ages if a >= DAY_SEC)
        print(f"замеров возраста уровня: {len(ages)}")
        print(f"медиана возраста:        {ages[len(ages) // 2] / 3600:.1f} ч")
        print(f"90-й перцентиль:         {ages[int(len(ages) * 0.9)] / 3600:.1f} ч")
        print(f"уровню ≥4 суток:         {ok4} ({ok4 / len(ages) * 100:.1f}%)")
        print(f"уровню ≥1 суток:         {ok1} ({ok1 / len(ages) * 100:.1f}%)")
    else:
        print("нет данных о возрасте уровня")

    # ─── 3. Стоп: структурный или пол по комиссии ────────────────────────────
    print("\n=== 3. Стоп: структура (за свипом) или пол по комиссии ===")
    print("Канон: стоп сразу за сегодняшним экстремумом. У нас структурный R")
    print("заменяется полом min_risk_fee_mult×round_trip = 0.30% цены.\n")
    widths = sorted(abs(r["entry"] - r["sl"]) / r["entry"] * 100 for r in rows)
    at_floor = sum(1 for w in widths if abs(w - 0.30) < 0.005)
    print(f"медиана ширины стопа: {widths[len(widths) // 2]:.3f}% цены")
    print(f"мин / макс:           {widths[0]:.3f}% / {widths[-1]:.3f}%")
    print(f"ровно на полу 0.30%:  {at_floor} из {len(widths)} "
          f"({at_floor / len(widths) * 100:.0f}%)")
    depths = [r["sweep_depth_bps"] for r in rows
              if r["sweep_depth_bps"] is not None]
    if depths:
        depths.sort()
        print(f"медиана глубины свипа: {depths[len(depths) // 2]:.1f} bps "
              f"= {depths[len(depths) // 2] / 100:.3f}% цены")
        print("  (если глубина свипа меньше 30 bps — структурный стоп ýже пола,")
        print("   и пол его подменяет: стоп уезжает ГЛУБЖЕ сегодняшнего экстремума)")

    # ─── 4. Цель против профит-лока ──────────────────────────────────────────
    print("\n=== 4. Достижима ли заявленная цель ===")
    tp_r = [abs(r["tp"] - r["entry"]) / abs(r["entry"] - r["sl"]) for r in rows
            if r["tp"] and abs(r["entry"] - r["sl"]) > 0]
    if tp_r:
        tp_r.sort()
        print(f"медиана цели: {tp_r[len(tp_r) // 2]:.2f}R "
              f"при профит-локе flow_exit на 1.5R")
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["close_reason"] or "?"] = reasons.get(r["close_reason"] or "?", 0) + 1
    for k in sorted(reasons, key=lambda k: -reasons[k]):
        print(f"  {k:<14}{reasons[k]:>5}  {reasons[k] / len(rows) * 100:>5.1f}%")

    # ─── 5. Время жизни сделки ───────────────────────────────────────────────
    print("\n=== 5. Время удержания ===")
    print("Канон: «от нескольких часов до нескольких дней», трейлинг.\n")
    holds = sorted((r["ts_close"] - r["ts_open"]) / 60.0 for r in rows
                   if r["ts_close"] and r["ts_open"])
    if holds:
        print(f"медиана: {holds[len(holds) // 2]:.0f} мин, "
              f"90-й перцентиль: {holds[int(len(holds) * 0.9)]:.0f} мин, "
              f"максимум: {holds[-1]:.0f} мин")
        over_2h = sum(1 for h in holds if h >= 120)
        print(f"дольше 2 часов: {over_2h} ({over_2h / len(holds) * 100:.0f}%)")


if __name__ == "__main__":
    main()
