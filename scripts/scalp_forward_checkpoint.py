#!/usr/bin/env python3
"""Fail-closed readiness checkpoint для activation этапа 5.

Скрипт только читает SQLite и никогда не меняет конфиг/торговую логику.
READY означает лишь достаточную длительность и число релевантных исходов;
после READY всё равно обязательны effect size, p<0.05, BH-FDR/CI, PF и
expectancy из специализированных отчётов.

Usage:
  python scripts/scalp_forward_checkpoint.py \
    --db /data/scalp_bot.sqlite --cutoff 2026-07-22T14:08:00Z
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_episodes import DEDUPED_SETUP_TYPES, collapse_episodes  # noqa: E402


DEFAULT_CUTOFF = "2026-07-22T14:08:00Z"
MIN_OUTCOMES = 100
MIN_DAYS = 14.0


@dataclass(frozen=True)
class Readiness:
    hypothesis: str
    outcomes: int
    first_ts: float | None
    last_ts: float | None

    @property
    def span_days(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return max(0.0, (self.last_ts - self.first_ts) / 86_400.0)

    @property
    def ready(self) -> bool:
        return self.outcomes >= MIN_OUTCOMES and self.span_days >= MIN_DAYS


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def _aggregate(con: sqlite3.Connection, query: str,
               params: tuple) -> tuple[int, float | None, float | None]:
    row = con.execute(query, params).fetchone()
    return int(row[0] or 0), row[1], row[2]


def collect_readiness(con: sqlite3.Connection,
                      cutoff: float) -> list[Readiness]:
    result: list[Readiness] = []

    # Meta-gate sweep: closed actual fills + terminal maker non-fill.
    actual = _aggregate(
        con,
        """SELECT COUNT(*),MIN(t.ts_open),MAX(t.ts_open)
           FROM trades t JOIN meta_label_features m ON m.trade_id=t.id
           WHERE t.ts_open>=? AND m.label_type='fade_exhaustion'
             AND m.would_keep IS NOT NULL AND t.status='closed'
             AND t.pnl_usd IS NOT NULL
             AND (t.close_reason IS NULL OR t.close_reason NOT LIKE 'entry_%')""",
        (cutoff,),
    )
    maker = _aggregate(
        con,
        """SELECT COUNT(*),MIN(c.ts_candidate),MAX(c.ts_candidate)
           FROM counterfactual_setups c
           JOIN meta_label_features m ON m.trade_id=c.source_trade_id
           WHERE c.ts_candidate>=? AND c.setup_type='maker_nonfill'
             AND m.label_type='fade_exhaustion' AND m.would_keep IS NOT NULL
             AND c.outcome_target IN ('target','sl')""",
        (cutoff,),
    )
    times = [x for x in (actual[1], actual[2], maker[1], maker[2])
             if x is not None]
    result.append(Readiness(
        "sweep_fade_meta_gate", actual[0] + maker[0],
        min(times) if times else None, max(times) if times else None))

    # Breakout meta-label на реально завершённых V1.
    n, first, last = _aggregate(
        con,
        """SELECT COUNT(*),MIN(t.ts_open),MAX(t.ts_open)
           FROM trades t JOIN meta_label_features m ON m.trade_id=t.id
           WHERE t.ts_open>=? AND t.strategy='density_break'
             AND m.label_type='breakout_fuel' AND m.would_keep IS NOT NULL
             AND t.status='closed' AND t.pnl_usd IS NOT NULL
             AND (t.close_reason IS NULL OR t.close_reason NOT LIKE 'entry_%')""",
        (cutoff,),
    )
    result.append(Readiness("density_break_meta_gate", n, first, last))

    for setup_type, hypothesis in (
        ("density_break_v2_shadow", "density_break_v2_retest"),
        ("canon_rejection_shadow", "canon_rejection_redesign"),
    ):
        if setup_type in DEDUPED_SETUP_TYPES:
            # Считаем эпизоды, а не строки: до v0.18.47 один свип писался
            # десятками кандидатов, и порог MIN_OUTCOMES брался бы дублями.
            rows = con.execute(
                """SELECT symbol,side,level_type,level_price,ts_candidate
                   FROM counterfactual_setups
                   WHERE ts_candidate>=? AND setup_type=?
                     AND outcome_target IN ('target','sl')""",
                (cutoff, setup_type),
            ).fetchall()
            episodes = collapse_episodes(
                [dict(zip(("symbol", "side", "level_type", "level_price",
                           "ts_candidate"), r)) for r in rows])
            times = [float(e["ts_candidate"]) for e in episodes]
            result.append(Readiness(
                hypothesis, len(episodes),
                min(times) if times else None, max(times) if times else None))
            continue
        n, first, last = _aggregate(
            con,
            """SELECT COUNT(*),MIN(ts_candidate),MAX(ts_candidate)
               FROM counterfactual_setups
               WHERE ts_candidate>=? AND setup_type=?
                 AND outcome_target IN ('target','sl')""",
            (cutoff, setup_type),
        )
        result.append(Readiness(hypothesis, n, first, last))

    rows = con.execute(
        """SELECT variant,COUNT(*),MIN(ts_candidate),MAX(ts_candidate)
           FROM counterfactual_setups
           WHERE ts_candidate>=?
             AND setup_type='density_bounce_persist_shadow'
             AND outcome_target IN ('target','sl')
           GROUP BY variant ORDER BY variant""",
        (cutoff,),
    ).fetchall()
    by_variant = {row[0]: row[1:] for row in rows}
    for seconds in (60, 90, 120, 180):
        variant = f"persist_{seconds}s"
        row = by_variant.get(variant, (0, None, None))
        result.append(Readiness(
            f"density_bounce_{variant}", int(row[0]), row[1], row[2]))

    # v0.18.45: ширина стопа. Считаем по outcome_tp (TP vs SL), а не по
    # outcome_target: гипотеза именно про исход брекета целиком, ведь комиссия
    # в R зависит от ширины стопа, а не от промежуточного +1.5R.
    # Контрольная ×1.0 тоже проходит checkpoint — сравнивать не с чем, пока
    # у контроля нет собственной выборки.
    rows = con.execute(
        """SELECT variant,COUNT(*),MIN(ts_candidate),MAX(ts_candidate)
           FROM counterfactual_setups
           WHERE ts_candidate>=? AND setup_type='sl_widen'
             AND outcome_tp IN ('tp','sl')
           GROUP BY variant ORDER BY variant""",
        (cutoff,),
    ).fetchall()
    by_variant = {row[0]: row[1:] for row in rows}
    # Ветки перечисляем явно (как persist-grid): гипотеза должна быть видна в
    # отчёте с n=0, иначе про неё легко забыть до появления первых исходов.
    for variant in ("x1", "x1.5", "x2", "x3"):
        row = by_variant.get(variant, (0, None, None))
        result.append(Readiness(
            f"sl_widen_{variant}", int(row[0]), row[1], row[2]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/scalp_bot.sqlite")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    args = parser.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = collect_readiness(con, _ts(args.cutoff))
    con.close()

    print(f"cutoff={args.cutoff} min_n={MIN_OUTCOMES} min_days={MIN_DAYS:g}")
    any_ready = False
    for row in rows:
        status = "READY_FOR_STATS" if row.ready else "COLLECTING"
        any_ready = any_ready or row.ready
        print(f"{row.hypothesis}: {status} n={row.outcomes} "
              f"span={row.span_days:.2f}d")
    print("activation=FORBIDDEN "
          "(READY_FOR_STATS запускает статистическую проверку, не автогейт)")
    return 0 if any_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
