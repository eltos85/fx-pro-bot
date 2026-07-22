#!/usr/bin/env python3
"""Coverage regime_features по стратегиям после telemetry cutoff.

Пример на VPS/в контейнере:
  python scripts/scalp_regime_coverage.py \
    --db /data/scalp_bot.sqlite --since 2026-07-22T13:00:00

Показывает долю non-NULL для полей, которые до v0.18.39 систематически
отсутствовали у auto-universe и density pins: KeyLevels прогревался только
для canon whitelist. Скрипт ничего не меняет в БД.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime


FIELDS = ("regime_ratio", "day_range_pct", "dist_high_pct", "dist_low_pct")


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/scalp_bot.sqlite")
    parser.add_argument("--since", required=True, help="ISO timestamp, UTC")
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    selects = [
        f"SUM(CASE WHEN r.{field} IS NOT NULL THEN 1 ELSE 0 END) AS {field}"
        for field in FIELDS
    ]
    rows = con.execute(
        f"""SELECT t.strategy, COUNT(*) AS n, {", ".join(selects)}
            FROM trades t
            JOIN regime_features r ON r.trade_id=t.id
            WHERE t.ts_open>=?
            GROUP BY t.strategy ORDER BY t.strategy""",
        (_ts(args.since),),
    ).fetchall()
    print(f"Regime coverage с {args.since} UTC")
    for row in rows:
        n = int(row["n"] or 0)
        print(f"\n{row['strategy']}: n={n}")
        for field in FIELDS:
            present = int(row[field] or 0)
            pct = 100.0 * present / n if n else 0.0
            status = "OK" if pct >= 95.0 else "GAP"
            print(f"  {field:<18} {present:>4}/{n:<4} {pct:>6.1f}% {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
