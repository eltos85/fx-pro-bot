#!/usr/bin/env python3
"""Read-only отчёт по shadow-эксперименту с шириной стопа (v0.18.45).

Гипотеза (аудит 2026-07-26, BUILDLOG_SCALP): при фиксированном $-риске
комиссия в R-единицах равна ``fee_rate / SL%``. При боевом SL 0.300% это
0.34R, что съедает gross edge +0.114R/сделку. Более широкий стоп уменьшает
qty → ноционал → комиссию в R, но одновременно меняет вероятности дойти до
TP/SL. Знак суммарного эффекта неизвестен — скрипт его измеряет.

Ветка ``x1`` — контроль: та же боевая геометрия, измеренная тем же
механизмом, поэтому разница между ветками не смешивается с разницей методик.

── Исправление смещения (v0.18.51) ────────────────────────────────────────
Раньше знаменателем было ``decided = tp + sl``, а ветки, не коснувшиеся ни
одного барьера за горизонт, просто выбрасывались. Их доля растёт с
множителем — TP уезжает вместе со стопом, а горизонт фиксирован 6ч: из 128
досмотренных не коснулись 2 (x1), 10 (x1.5), 21 (x2), **58 (x3)**. Отбрасывая
их, мы сравнивали разные популяции: у x3 в выборку попадали только быстро
разрешившиеся случаи.

Ключ к исправлению: «не коснулся барьера за 6ч» — это НАБЛЮДАЕМЫЙ третий
исход, а не пропуск. Мы досмотрели горизонт до конца и знаем результат: он
равен переоценке по цене на конце горизонта (``last_price``). Поэтому
знаменатель теперь — все досмотренные (``state='final'``), а исход считается
в R по трём случаям. Настоящее цензурирование — только ``pending`` (ещё идёт)
и ``abandoned``; их доля одинакова у всех веток (13 и 14 из 155), потому что
ветки живут на одних и тех же исходных сделках, и они честно исключаются.

Скрипт НИЧЕГО не активирует. Решение об изменении стопа требует прохождения
`scripts/scalp_forward_checkpoint.py` (исходы + независимые символо-дни +
покрытие режимов) плюс порогов из `sample-size.mdc`.

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


def _load(conn: sqlite3.Connection, since: float, filled_only: bool,
          strategy: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.id, c.variant, c.strategy, c.symbol, c.side, c.entry, "
        "       c.risk, c.tp, c.state, c.outcome_tp, c.source_trade_id, "
        "       c.last_price, t.close_reason "
        "FROM counterfactual_setups c "
        "LEFT JOIN trades t ON t.id = c.source_trade_id "
        "WHERE c.setup_type='sl_widen' AND c.ts_candidate >= ?"
    )
    params: list = [since]
    if strategy:
        # Агрегат по всем стратегиям тянут те, у кого теней больше. Вывод
        # «стоп расширять не надо» может не переноситься на стратегию с иной
        # геометрией и иной ставкой комиссии, поэтому её надо уметь выделить.
        sql += " AND c.strategy = ?"
        params.append(strategy)
    if filled_only:
        # Сделки, которые так и не вошли в рынок, не несут информации о том,
        # как повёл бы себя стоп: там нет ни fill-а, ни исполненной геометрии.
        sql += (" AND t.close_reason IS NOT NULL AND t.close_reason NOT IN "
                "('entry_Cancelled','entry_timeout','entry_Rejected')")
    return conn.execute(sql, tuple(params)).fetchall()


def realised_r(row) -> float | None:
    """Исход ветки в R-единицах. Три случая, и третий — НАБЛЮДАЕМЫЙ.

    Барьер TP → +tp_r, барьер SL → −1. Если за горизонт не задет ни один
    барьер, это не пропуск данных: горизонт досмотрен, результат равен
    переоценке по цене на его конце. Именно выбрасывание этого случая давало
    смещение, растущее с множителем (см. докстринг модуля).
    """
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


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _summarise(rows: list[sqlite3.Row], fee_pct: float) -> dict[str, dict]:
    arms: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "final": 0, "censored": 0, "tp": 0, "sl": 0,
                 "no_touch": 0, "r": [], "tp_r": [], "sl_pct": []})
    for row in rows:
        arm = arms[row["variant"]]
        arm["n"] += 1
        risk = float(row["risk"] or 0.0)
        entry = float(row["entry"] or 0.0)
        if risk > 0 and entry > 0:
            arm["tp_r"].append(abs(float(row["tp"]) - entry) / risk)
            arm["sl_pct"].append(100.0 * risk / entry)
        if row["state"] != "final":
            # Настоящее цензурирование: наблюдение ещё не досмотрено. Доля
            # одинакова у всех веток (общие исходные сделки), смещения нет.
            arm["censored"] += 1
            continue
        arm["final"] += 1
        if row["outcome_tp"] == "tp":
            arm["tp"] += 1
        elif row["outcome_tp"] == "sl":
            arm["sl"] += 1
        else:
            arm["no_touch"] += 1
        value = realised_r(row)
        if value is not None:
            arm["r"].append(value)

    out: dict[str, dict] = {}
    for name, arm in arms.items():
        tp_r = _mean(arm["tp_r"]) or 0.0
        sl_pct = _mean(arm["sl_pct"]) or 0.0
        # Комиссия в R = ставка / ширина стопа в % цены: при фиксированном
        # $-риске широкий стоп уменьшает ноционал и удешевляет сделку в R.
        fee_r = (fee_pct / sl_pct) if sl_pct > 0 else None
        gross_r = _mean(arm["r"])
        decided = arm["tp"] + arm["sl"]
        lo, hi = _wilson(arm["tp"], arm["final"]) if arm["final"] else (0.0, 0.0)
        out[name] = {
            **arm, "decided": decided, "tp_r": tp_r, "sl_pct": sl_pct,
            "fee_r": fee_r,
            # Доля TP считается от ВСЕХ досмотренных, а не от коснувшихся:
            # знаменатель должен быть одинаков у веток, иначе сравниваем
            # разные популяции.
            "p_tp": (arm["tp"] / arm["final"]) if arm["final"] else None,
            "gross_r": gross_r,
            "net_r": (None if gross_r is None or fee_r is None
                      else gross_r - fee_r),
            "ci": (lo, hi),
        }
    return out


def _paired(rows: list[sqlite3.Row], control: str,
            fee: dict[str, float | None]) -> dict[str, dict]:
    """Парное сравнение веток на ОДНИХ И ТЕХ ЖЕ сделках.

    Парный дизайн снимает разброс по рынку/символу/времени: сравниваем не две
    выборки, а два исхода одного и того же сигнала. Пары строятся по R-исходу
    (включая «не коснулся»), а не по категории TP/SL — иначе из пар выпадали бы
    ровно те случаи, из-за которых и возникало смещение.

    Возвращает по ветке: число пар, средняя разница netR, её стандартная
    ошибка и знаковый счёт (ветка лучше / контроль лучше).
    """
    by_trade: dict[int, dict[str, float]] = defaultdict(dict)
    for row in rows:
        tid = row["source_trade_id"]
        if tid is None or row["state"] != "final":
            continue
        value = realised_r(row)
        if value is not None:
            by_trade[int(tid)][row["variant"]] = value
    out: dict[str, dict] = {}
    control_fee = fee.get(control)
    for arm in {r["variant"] for r in rows} - {control}:
        arm_fee = fee.get(arm)
        deltas: list[float] = []
        wins = losses = 0
        for outcomes in by_trade.values():
            base, other = outcomes.get(control), outcomes.get(arm)
            if base is None or other is None:
                continue
            if arm_fee is not None and control_fee is not None:
                delta = (other - arm_fee) - (base - control_fee)
            else:
                delta = other - base
            deltas.append(delta)
            if delta > 0:
                wins += 1
            elif delta < 0:
                losses += 1
        mean = _mean(deltas)
        stderr = None
        if mean is not None and len(deltas) > 1:
            var = sum((d - mean) ** 2 for d in deltas) / (len(deltas) - 1)
            stderr = math.sqrt(var / len(deltas))
        out[arm] = {"pairs": len(deltas), "mean": mean, "stderr": stderr,
                    "wins": wins, "losses": losses}
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
    parser.add_argument("--strategy", default=None,
                        help="разбор по одной стратегии (агрегат тянет та, "
                             "у кого теней больше)")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = _load(conn, _timestamp(args.since), args.filled_only, args.strategy)
    print(f"source={args.db} since={args.since} fee={args.fee_pct}% "
          f"round-turn filled_only={args.filled_only} "
          f"strategy={args.strategy or 'все'} observational-only")
    if not rows:
        print("NO DATA")
        return 0

    arms = _summarise(rows, args.fee_pct)
    print()
    print(f"{'ветка':7} {'N':>5} {'досм':>5} {'цензур':>7} {'TP':>4} "
          f"{'SL':>4} {'нет_кас':>8} {'TP%':>6} {'95% CI':>13} "
          f"{'SL%цены':>8} {'ком.R':>7} {'grossR':>8} {'netR':>8}")
    for name in sorted(arms, key=lambda k: arms[k]["sl_pct"]):
        a = arms[name]
        lo, hi = a["ci"]
        ci = f"{100*lo:.1f}-{100*hi:.1f}" if a["final"] else "n/a"
        wr = f"{100*a['p_tp']:.1f}" if a["p_tp"] is not None else "n/a"
        print(f"{name:7} {a['n']:>5} {a['final']:>5} {a['censored']:>7} "
              f"{a['tp']:>4} {a['sl']:>4} {a['no_touch']:>8} {wr:>6} "
              f"{ci:>13} {a['sl_pct']:>8.3f} {_fmt(a['fee_r'])!s:>7} "
              f"{_fmt(a['gross_r'])!s:>8} {_fmt(a['net_r'])!s:>8}")
    print("TP% и grossR считаются от ВСЕХ досмотренных: «нет касания» — "
          "наблюдаемый исход (переоценка на конце горизонта), не пропуск.")

    print()
    print(f"=== парное сравнение против контроля {args.control} ===")
    if args.control not in arms:
        print(f"контрольной ветки {args.control} нет — сравнивать не с чем")
    else:
        fee = {k: v["fee_r"] for k, v in arms.items()}
        pairs = _paired(rows, args.control, fee)
        if not pairs:
            print("нет ветки, досмотренной одновременно с контролем")
        for arm in sorted(pairs):
            p = pairs[arm]
            ci = ""
            if p["mean"] is not None and p["stderr"]:
                lo = p["mean"] - 1.96 * p["stderr"]
                hi = p["mean"] + 1.96 * p["stderr"]
                ci = f" 95% CI [{lo:+.3f}; {hi:+.3f}]"
            print(f"{arm}: пар={p['pairs']} ΔnetR={_fmt(p['mean'])}{ci} "
                  f"ветка_лучше={p['wins']} контроль_лучше={p['losses']}")
        print("CI не учитывает кластеризацию по символо-дням — "
              "он оптимистичен; готовность проверяет scalp_forward_checkpoint.")

    observed_max = max((a["final"] for a in arms.values()), default=0)
    print()
    if observed_max < MIN_DECIDED:
        print(f"СТАТУС: COLLECTING — максимум {observed_max} досмотренных "
              f"исходов при пороге {MIN_DECIDED}. Выводы и изменения "
              f"запрещены (sample-size.mdc).")
    else:
        print(f"СТАТУС: выборка ≥{MIN_DECIDED}. Прежде чем что-то менять — "
              f"scalp_forward_checkpoint.py (кластеры и режимы) и OOS.")
    print("Напоминание: netR предполагает, что $-риск на сделку фиксирован, "
          "а qty пересчитывается под ширину стопа.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
