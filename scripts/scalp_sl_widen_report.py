#!/usr/bin/env python3
"""Read-only отчёт по shadow-эксперименту с шириной стопа (v0.18.45).

Гипотеза (аудит 2026-07-26, BUILDLOG_SCALP): при фиксированном $-риске
комиссия в R-единицах равна ``fee_rate / SL%``. При боевом SL 0.300% это
0.34R, что съедает gross edge +0.114R/сделку. Более широкий стоп уменьшает
qty → ноционал → комиссию в R, но одновременно меняет вероятности дойти до
TP/SL. Знак суммарного эффекта неизвестен — скрипт его измеряет.

Ветка ``x1`` — контроль: та же боевая геометрия, измеренная тем же
механизмом, поэтому разница между ветками не смешивается с разницей методик.

Скрипт НИЧЕГО не активирует. Решение об изменении стопа требует прохождения
`scripts/scalp_forward_checkpoint.py` (≥100 исходов И ≥14 дней) плюс порогов
из `sample-size.mdc`.

Usage:
  python scripts/scalp_sl_widen_report.py data/scalp_bot.sqlite \
      --since 2026-07-26T09:00:00Z [--filled-only] [--fee-pct 0.1016]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# Эмпирическая round-turn ставка, измеренная на 1015 bracket-выходах
# (2026-07-26): медиана 0.1016% от ноционала. Taker Bybit 0.055% в сторону;
# ниже за счёт maker-входов части сделок.
DEFAULT_FEE_PCT = 0.1016

# Порог, ниже которого выводы не делаем (sample-size.mdc).
MIN_DECIDED = 100


def _timestamp(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / d, (centre + half) / d)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _load(conn: sqlite3.Connection, since: float,
          filled_only: bool) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.id, c.variant, c.strategy, c.symbol, c.side, c.entry, "
        "       c.risk, c.tp, c.state, c.outcome_tp, c.source_trade_id, "
        "       t.close_reason "
        "FROM counterfactual_setups c "
        "LEFT JOIN trades t ON t.id = c.source_trade_id "
        "WHERE c.setup_type='sl_widen' AND c.ts_candidate >= ?"
    )
    if filled_only:
        # Сделки, которые так и не вошли в рынок, не несут информации о том,
        # как повёл бы себя стоп: там нет ни fill-а, ни исполненной геометрии.
        sql += (" AND t.close_reason IS NOT NULL AND t.close_reason NOT IN "
                "('entry_Cancelled','entry_timeout','entry_Rejected')")
    return conn.execute(sql, (since,)).fetchall()


def _summarise(rows: list[sqlite3.Row], fee_pct: float) -> dict[str, dict]:
    arms: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "final": 0, "tp": 0, "sl": 0,
                 "tp_r": [], "sl_pct": []})
    for row in rows:
        arm = arms[row["variant"]]
        arm["n"] += 1
        if row["state"] == "final":
            arm["final"] += 1
        if row["outcome_tp"] == "tp":
            arm["tp"] += 1
        elif row["outcome_tp"] == "sl":
            arm["sl"] += 1
        risk = float(row["risk"] or 0.0)
        entry = float(row["entry"] or 0.0)
        if risk > 0 and entry > 0:
            arm["tp_r"].append(abs(float(row["tp"]) - entry) / risk)
            arm["sl_pct"].append(100.0 * risk / entry)

    out: dict[str, dict] = {}
    for name, arm in arms.items():
        decided = arm["tp"] + arm["sl"]
        tp_r = (sum(arm["tp_r"]) / len(arm["tp_r"])) if arm["tp_r"] else 0.0
        sl_pct = ((sum(arm["sl_pct"]) / len(arm["sl_pct"]))
                  if arm["sl_pct"] else 0.0)
        fee_r = (fee_pct / sl_pct) if sl_pct > 0 else None
        if decided:
            p_tp = arm["tp"] / decided
            gross_r = p_tp * tp_r - (1.0 - p_tp) * 1.0
            lo, hi = _wilson(arm["tp"], decided)
        else:
            p_tp = gross_r = None
            lo = hi = 0.0
        out[name] = {
            **arm, "decided": decided, "tp_r": tp_r, "sl_pct": sl_pct,
            "fee_r": fee_r, "p_tp": p_tp, "gross_r": gross_r,
            "net_r": (None if gross_r is None or fee_r is None
                      else gross_r - fee_r),
            "ci": (lo, hi),
        }
    return out


def _paired(rows: list[sqlite3.Row], control: str) -> dict[str, tuple[int, int, int]]:
    """Парное сравнение веток на ОДНИХ И ТЕХ ЖЕ сделках.

    Парный дизайн снимает разброс по рынку/символу/времени: сравниваем не две
    выборки, а два исхода одного и того же сигнала. Возвращает по ветке
    (обе решены, ветка выиграла где контроль проиграл, наоборот).
    """
    by_trade: dict[int, dict[str, str]] = defaultdict(dict)
    for row in rows:
        tid = row["source_trade_id"]
        if tid is not None and row["outcome_tp"] in ("tp", "sl"):
            by_trade[int(tid)][row["variant"]] = row["outcome_tp"]
    out: dict[str, tuple[int, int, int]] = {}
    for arm in {r["variant"] for r in rows} - {control}:
        both = wins = losses = 0
        for outcomes in by_trade.values():
            base, other = outcomes.get(control), outcomes.get(arm)
            if base is None or other is None:
                continue
            both += 1
            if other == "tp" and base == "sl":
                wins += 1
            elif other == "sl" and base == "tp":
                losses += 1
        out[arm] = (both, wins, losses)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--since", default="1970-01-01T00:00:00+00:00")
    parser.add_argument("--fee-pct", type=float, default=DEFAULT_FEE_PCT,
                        help="round-turn комиссия в %% от ноционала")
    parser.add_argument("--filled-only", action="store_true",
                        help="только сделки, реально вошедшие в рынок")
    parser.add_argument("--control", default="x1")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = _load(conn, _timestamp(args.since), args.filled_only)
    print(f"source={args.db} since={args.since} fee={args.fee_pct}% "
          f"round-turn filled_only={args.filled_only} observational-only")
    if not rows:
        print("NO DATA")
        return 0

    arms = _summarise(rows, args.fee_pct)
    print()
    print(f"{'ветка':7} {'N':>5} {'final':>6} {'решено':>7} {'TP':>5} "
          f"{'SL':>5} {'TP%':>6} {'95% CI':>14} {'SL%цены':>8} "
          f"{'ком.R':>7} {'grossR':>8} {'netR':>8}")
    for name in sorted(arms, key=lambda k: arms[k]["sl_pct"]):
        a = arms[name]
        lo, hi = a["ci"]
        ci = f"{100*lo:.1f}-{100*hi:.1f}" if a["decided"] else "n/a"
        wr = f"{100*a['p_tp']:.1f}" if a["p_tp"] is not None else "n/a"
        print(f"{name:7} {a['n']:>5} {a['final']:>6} {a['decided']:>7} "
              f"{a['tp']:>5} {a['sl']:>5} {wr:>6} {ci:>14} "
              f"{a['sl_pct']:>8.3f} {_fmt(a['fee_r'])!s:>7} "
              f"{_fmt(a['gross_r'])!s:>8} {_fmt(a['net_r'])!s:>8}")

    print()
    print(f"=== парное сравнение против контроля {args.control} ===")
    control = arms.get(args.control)
    if control is None:
        print(f"контрольной ветки {args.control} нет — сравнивать не с чем")
    else:
        pairs = _paired(rows, args.control)
        if not pairs:
            print("нет ветки, решённой одновременно с контролем")
        for arm in sorted(pairs):
            both, wins, losses = pairs[arm]
            delta = (arms[arm]["net_r"] - control["net_r"]
                     if arms[arm]["net_r"] is not None
                     and control["net_r"] is not None else None)
            print(f"{arm}: пар={both} ветка_лучше={wins} "
                  f"контроль_лучше={losses} ΔnetR={_fmt(delta)}")

    decided_max = max((a["decided"] for a in arms.values()), default=0)
    print()
    if decided_max < MIN_DECIDED:
        print(f"СТАТУС: COLLECTING — максимум {decided_max} решённых исходов "
              f"при пороге {MIN_DECIDED}. Выводы и изменения запрещены "
              f"(sample-size.mdc).")
    else:
        print(f"СТАТУС: выборка ≥{MIN_DECIDED}. Прежде чем что-то менять — "
              f"scalp_forward_checkpoint.py (нужны ещё ≥14 дней) и OOS-проверка.")
    print("Напоминание: netR предполагает, что $-риск на сделку фиксирован, "
          "а qty пересчитывается под ширину стопа.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
