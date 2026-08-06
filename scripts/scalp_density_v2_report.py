#!/usr/bin/env python3
"""Разбор гипотезы density_break V2 (вход лимиткой на ретесте стены).

V2 — теневая альтернатива боевому density_break: после close-confirm пробоя не
входить сразу по рынку, а ждать возврата к пробитой стене и войти лимиткой.
Скрипт только читает SQLite и ничего не меняет.

Ключевой вопрос не «лучше ли V2 боевого», а более базовый: есть ли у V2 edge
вообще. Брекет у обеих версий один (TP 3.5R / SL 1R), поэтому у правила есть
собственная точка безубыточности по доле TP, и её достаточно, чтобы вынести
вердикт, не опираясь на шумное сравнение с 33 боевыми сделками.

Считаем в R, а не в долларах: риск на сделку фиксирован, но 1R в долларах
плавает между символами, и суммирование долларов смешало бы разные ставки.

Незалившиеся сетапы (retest не пришёл / уровень инвалидирован) — это НЕ
пропущенные данные, а исход «сделки не было» с результатом 0R. Выбросить их
значило бы сравнивать V2 по подвыборке, где ему повезло с входом
(differential censoring, тот же дефект, что чинили в sl_widen v0.18.51).

Комиссия берётся из выученных ставок символа (`symbol_fees`, v0.18.53), с
откатом на стандартные Bybit. У V2 вход maker, выход taker; у боевого обе
стороны taker — эта разница в пользу V2 и учтена явно.

Usage:
  python scripts/scalp_density_v2_report.py /data/scalp_bot.sqlite \
    --since 2026-07-22T14:08:00Z
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

# Стандартные ставки Bybit (доля от notional за сторону). Используются, только
# если по символу ещё не выучена фактическая ставка.
STD_MAKER = 0.0002
STD_TAKER = 0.00055
SETUP_TYPE = "density_break_v2_shadow"
LIVE_STRATEGY = "density_break"
BOOTSTRAP = 10_000
SEED = 20260806


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / d, (centre + half) / d)


def cluster_bootstrap(clusters: list[list[float]],
                      reps: int = BOOTSTRAP) -> tuple[float, float]:
    """CI среднего при кластеризованных наблюдениях (bootstrap по кластерам).

    Наблюдения внутри символо-дня скоррелированы (та же стена, тот же режим),
    поэтому ресэмплим кластеры целиком: иначе CI будет ложно узким.
    """
    if len(clusters) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(SEED)
    k = len(clusters)
    means = []
    for _ in range(reps):
        pool: list[float] = []
        for _ in range(k):
            pool.extend(clusters[rng.randrange(k)])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    return (lo, hi)


def fee_rates(con: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    try:
        rows = con.execute(
            "SELECT symbol, maker_rate, taker_rate FROM symbol_fees").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r[0]): (r[1], r[2]) for r in rows}


def _fee_r(symbol: str, sl_frac: float, learned: dict, *, maker_entry: bool,
           ) -> float:
    """Комиссия round-trip в единицах R.

    R = sl_frac × notional, комиссия = (вход + выход) × notional, поэтому в R
    она равна отношению ставок к ширине стопа — чем у́же стоп, тем дороже
    сделка в R.
    """
    m, t = learned.get(symbol, (None, None))
    maker = m if m else STD_MAKER
    taker = t if t else STD_TAKER
    entry = maker if maker_entry else taker
    if sl_frac <= 0:
        return 0.0
    return (entry + taker) / sl_frac


def v2_outcomes(con: sqlite3.Connection, since: float, learned: dict,
                ) -> tuple[list[dict], dict]:
    rows = con.execute(
        f"""SELECT symbol, ts_candidate, state, entry, risk, tp,
                   outcome_tp, v1_signal_created
            FROM counterfactual_setups
            WHERE setup_type=? AND ts_candidate>=? AND state!='pending'""",
        (SETUP_TYPE, since),
    ).fetchall()
    out: list[dict] = []
    bracket: dict[float, int] = defaultdict(int)
    for symbol, ts, state, entry, risk, tp, outcome, v1 in rows:
        if not entry or not risk or risk <= 0:
            continue
        tp_r = abs(float(tp) - float(entry)) / float(risk)
        bracket[round(tp_r, 2)] += 1
        sl_frac = float(risk) / float(entry)
        fee = _fee_r(str(symbol), sl_frac, learned, maker_entry=True)
        if outcome == "tp":
            gross, filled = tp_r, True
        elif outcome == "sl":
            gross, filled = -1.0, True
        else:
            gross, filled = 0.0, False
        out.append({
            "symbol": str(symbol), "day": _day(ts), "filled": filled,
            "hit": outcome == "tp", "gross_r": gross,
            "net_r": gross - fee if filled else 0.0,
            "v1": bool(v1), "tp_r": tp_r,
        })
    return out, dict(bracket)


def live_outcomes(con: sqlite3.Connection, since: float) -> list[dict]:
    rows = con.execute(
        """SELECT symbol, ts_open, entry, sl, qty, pnl_usd, fees_usd,
                  close_reason
           FROM trades
           WHERE strategy=? AND ts_open>=? AND pnl_usd IS NOT NULL""",
        (LIVE_STRATEGY, since),
    ).fetchall()
    out = []
    for symbol, ts, entry, sl, qty, pnl, fees, reason in rows:
        risk_usd = abs(float(entry) - float(sl)) * float(qty)
        if risk_usd <= 0:
            continue
        net = float(pnl) / risk_usd
        out.append({
            "symbol": str(symbol), "day": _day(ts), "filled": True,
            "hit": reason == "tp_hit", "net_r": net,
            "gross_r": (float(pnl) + float(fees or 0.0)) / risk_usd,
        })
    return out


def _by_cluster(rows: list[dict], field: str) -> list[list[float]]:
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        groups[(r["symbol"], r["day"])].append(r[field])
    return list(groups.values())


def _report(title: str, rows: list[dict], *, tp_r: float) -> None:
    filled = [r for r in rows if r["filled"]]
    hits = sum(1 for r in filled if r["hit"])
    n_f = len(filled)
    print(f"\n{title}")
    print(f"  сетапов {len(rows)}, входов {n_f} "
          f"({100 * n_f / max(1, len(rows)):.0f}%), TP {hits}, SL {n_f - hits}")
    if not n_f:
        return
    rate = hits / n_f
    lo, hi = wilson(hits, n_f)
    breakeven = 1.0 / (1.0 + tp_r)
    verdict = ("НИЖЕ безубыточности" if hi < breakeven
               else "ВЫШЕ безубыточности" if lo > breakeven
               else "безубыточность внутри CI")
    print(f"  доля TP {rate:.1%} CI95 [{lo:.1%}; {hi:.1%}] | "
          f"безубыток по gross {breakeven:.1%} → {verdict}")
    for label, field, universe in (
            ("gross R/вход", "gross_r", filled),
            ("net R/вход", "net_r", filled),
            ("net R/сетап", "net_r", rows)):
        vals = [r[field] for r in universe]
        mean = sum(vals) / len(vals)
        lo_b, hi_b = cluster_bootstrap(_by_cluster(universe, field))
        print(f"  {label:14} {mean:+.3f} "
              f"CI95 [{lo_b:+.3f}; {hi_b:+.3f}] (bootstrap по символо-дням, "
              f"кластеров {len(_by_cluster(universe, field))})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--since", default="2026-07-22T14:08:00Z")
    args = parser.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    since = _ts(args.since)
    learned = fee_rates(con)
    v2, bracket = v2_outcomes(con, since, learned)
    live = live_outcomes(con, since)
    con.close()

    tp_r = max(bracket, key=bracket.get) if bracket else 3.5
    print(f"source={args.db} since={args.since} observational-only")
    print(f"брекет TP {tp_r}R / SL 1R (у {bracket.get(tp_r, 0)} из {len(v2)} "
          f"кандидатов), выученных ставок комиссии: {len(learned)}")

    _report("V2 — все кандидаты", v2, tp_r=tp_r)
    _report("V2 — только там, где боевой V1 тоже дал сигнал",
            [r for r in v2 if r["v1"]], tp_r=tp_r)
    _report("Боевой density_break за тот же период", live, tp_r=tp_r)

    print("\nЗамечание: незалившиеся сетапы входят в «net R/сетап» как 0R — "
          "это исход «сделки не было», а не пропуск данных.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
