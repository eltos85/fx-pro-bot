#!/usr/bin/env python3
"""Отчёт по теневой вселенной (v0.18.48): стоил ли чего-то порог оборота.

Только чтение SQLite. Скрипт НЕ меняет порог и не включает торговлю —
он лишь показывает, что произошло бы с монетами, которые
``universe_min_turnover_usd`` не пускает в бой, хотя range- и spread-стражи
они проходят.

Повод для замера (2026-07-29): авто-вселенная выродилась хронически
(07-08..07-23 торговался ровно один ZECUSDT, с 07-28 фильтр отдаёт 0 монет),
при этом 94 из 299 отсечённых оборотом монет проходят spread-страж. Гипотеза
«на низколиквидных теряем» никогда не проверялась: 93% сделок прошли при
спреде <1 bps, и именно эта корзина даёт netR −0.127 (range restriction).

Сравнение честное только внутри стратегии: у sweep_fade и density_break разные
R:R и частота, поэтому агрегат по всем теням смешал бы разные распределения.
Контроль — боевые сделки той же стратегии за то же окно.

ВАЖНОЕ ОГРАНИЧЕНИЕ ИНТЕРПРЕТАЦИИ. Тень фиксирует сигнал детектора и обходит
боевые гейты, которые в живом цикле стоят после ``resolve``: dead_market, HTF
EMA200, DMI long-gate, no_long_symbols, rate-limit, max_open_positions. Значит
теневая выборка — ВЕРХНЯЯ оценка возможностей: часть этих сетапов живой бот не
взял бы даже при достаточном обороте. Поэтому «тень лучше контроля» само по
себе НЕ доказывает, что порог оборота вреден: разницу может давать отсутствие
гейтов. Вывод про порог правомерен только если тень проигрывает или сравнима —
тогда порог точно ничего не стоит нам упускать. Комиссия в тень тоже не
заложена, поэтому netR боевых сделок строже теневого MFE/MAE.

Решения принимать нельзя, пока не пройден ``scalp_forward_checkpoint.py``
(≥100 исходов И ≥14 дней) — см. sample-size.mdc.

Usage:
  python scripts/scalp_shadow_universe_report.py --db /data/scalp_bot.sqlite
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

MIN_DECIDED = 100
REJECTED = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def _wilson(wins: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    z = 1.96
    p = wins / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half) * 100.0, min(1.0, centre + half) * 100.0)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def shadow_arms(con: sqlite3.Connection, cutoff: float) -> dict[str, dict]:
    """Тени по стратегиям: исходы гипотетических входов."""
    arms: dict[str, dict] = {}
    rows = con.execute(
        """SELECT variant,symbol,outcome_tp,mfe_r,mae_r,state,ts_candidate
           FROM counterfactual_setups
           WHERE setup_type='shadow_universe' AND ts_candidate>=?""",
        (cutoff,),
    ).fetchall()
    for variant, symbol, outcome, mfe, mae, state, ts in rows:
        arm = arms.setdefault(variant, {
            "n": 0, "decided": 0, "tp": 0, "sl": 0, "symbols": {},
            "mfe": [], "mae": [], "first": None, "last": None})
        arm["n"] += 1
        arm["symbols"][symbol] = arm["symbols"].get(symbol, 0) + 1
        if mfe is not None:
            arm["mfe"].append(float(mfe))
        if mae is not None:
            arm["mae"].append(float(mae))
        if outcome in ("tp", "sl"):
            arm["decided"] += 1
            arm["tp" if outcome == "tp" else "sl"] += 1
        for key, comparator in (("first", min), ("last", max)):
            current = arm[key]
            arm[key] = ts if current is None else comparator(current, ts)
    return arms


def live_control(con: sqlite3.Connection, cutoff: float) -> dict[str, dict]:
    """Контроль: боевые сделки тех же стратегий за то же окно.

    R-множитель считаем по факту (pnl / риск), чтобы сравнивать с теневым
    outcome_tp в одних единицах. Отклонённые входы исключаем — они не сделки.
    """
    control: dict[str, dict] = {}
    placeholders = ",".join("?" * len(REJECTED))
    rows = con.execute(
        f"""SELECT strategy,pnl_usd,entry,sl,qty,close_reason
            FROM trades
            WHERE ts_open>=? AND pnl_usd IS NOT NULL
              AND close_reason NOT IN ({placeholders})""",
        (cutoff, *REJECTED),
    ).fetchall()
    for strategy, pnl, entry, sl, qty, reason in rows:
        arm = control.setdefault(strategy, {"n": 0, "wins": 0, "r": []})
        arm["n"] += 1
        if (pnl or 0.0) > 0:
            arm["wins"] += 1
        risk = abs(float(entry or 0.0) - float(sl or 0.0)) * float(qty or 0.0)
        if risk > 0:
            arm["r"].append(float(pnl) / risk)
    return control


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/scalp_bot.sqlite")
    parser.add_argument("--cutoff", default="2026-07-29T00:00:00Z",
                        help="ISO UTC; окно наблюдения теневой вселенной")
    args = parser.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    arms = shadow_arms(con, _ts(args.cutoff))
    control = live_control(con, _ts(args.cutoff))
    con.close()

    print(f"cutoff={args.cutoff}  порог решения={MIN_DECIDED} решённых исходов")
    if not arms:
        print("\nтеней ещё нет: либо вселенная не пустеет (тогда наблюдать "
              "нечего), либо сетапы не сработали. Это не ошибка.")
        return 2

    print("\n=== ТЕНИ: монеты, отсечённые порогом оборота ===\n")
    header = (f"{'стратегия':18} {'N':>5} {'решено':>7} {'TP':>4} {'SL':>4} "
              f"{'TP%':>6} {'95% CI':>14} {'MFE_R':>7} {'MAE_R':>7} {'дней':>6}")
    print(header)
    for variant, arm in sorted(arms.items()):
        decided = arm["decided"]
        tp_rate = arm["tp"] / decided * 100.0 if decided else 0.0
        low, high = _wilson(arm["tp"], decided)
        span = ((arm["last"] - arm["first"]) / 86_400.0
                if arm["first"] and arm["last"] else 0.0)
        print(f"{variant:18} {arm['n']:>5} {decided:>7} {arm['tp']:>4} "
              f"{arm['sl']:>4} {tp_rate:5.1f}% [{low:5.1f};{high:5.1f}] "
              f"{_mean(arm['mfe']):7.3f} {_mean(arm['mae']):7.3f} {span:6.2f}")

    print("\n=== КОНТРОЛЬ: боевые сделки тех же стратегий, то же окно ===\n")
    print(f"{'стратегия':18} {'N':>5} {'WR':>7} {'netR/сделку':>13}")
    for variant in sorted(arms):
        arm = control.get(variant)
        if not arm or not arm["n"]:
            print(f"{variant:18} {'—':>5}  боевых сделок нет — сравнивать не с чем")
            continue
        print(f"{variant:18} {arm['n']:>5} "
              f"{arm['wins'] / arm['n'] * 100:6.1f}% {_mean(arm['r']):13.3f}")

    print("\n=== монеты в тенях ===\n")
    for variant, arm in sorted(arms.items()):
        top = sorted(arm["symbols"].items(), key=lambda x: -x[1])
        print(f"  {variant}: " + ", ".join(f"{s}×{n}" for s, n in top))

    ready = max((a["decided"] for a in arms.values()), default=0)
    print(f"\nСТАТУС: {'READY_FOR_STATS' if ready >= MIN_DECIDED else 'COLLECTING'}"
          f" — максимум {ready} решённых исходов при пороге {MIN_DECIDED}.")
    if ready < MIN_DECIDED:
        print("Выводы и изменение порога оборота запрещены (sample-size.mdc).")
    print("Ограничение: тень обходит боевые гейты (dead_market, HTF, DMI, "
          "rate-limit) и не платит комиссию — это ВЕРХНЯЯ оценка. «Тень лучше "
          "контроля» может объясняться отсутствием гейтов, а не порогом.")
    return 0 if ready >= MIN_DECIDED else 2


if __name__ == "__main__":
    raise SystemExit(main())
