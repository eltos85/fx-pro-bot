#!/usr/bin/env python3
"""Read-only evidence report по counterfactual_setups.

Usage:
  python scripts/scalp_counterfactual_report.py data/scalp_bot.sqlite \
      --since 2026-07-22T13:30:00+00:00
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone


def _timestamp(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--since", default="1970-01-01T00:00:00+00:00")
    args = parser.parse_args()
    since = _timestamp(args.since)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT setup_type,variant,COUNT(*) n,"
        "SUM(state='final') final_n,"
        "SUM(outcome_target='target') target_first,"
        "SUM(outcome_target='sl') sl_first,"
        "AVG(mfe_r_60) mfe60,AVG(mae_r_60) mae60,"
        "AVG(mfe_r_180) mfe180,AVG(mae_r_180) mae180,"
        "AVG(retest_delay_sec) retest_delay,"
        "AVG(level_age_sec) level_age,AVG(level_touches) level_touches "
        "FROM counterfactual_setups WHERE ts_candidate>=? "
        "GROUP BY setup_type,variant ORDER BY setup_type,variant",
        (since,),
    ).fetchall()
    print(f"source={args.db} since={args.since} UTC observational-only")
    if not rows:
        print("NO DATA")
        return
    for row in rows:
        decided = int(row["target_first"] or 0) + int(row["sl_first"] or 0)
        wr = (100.0 * int(row["target_first"] or 0) / decided
              if decided else None)
        wr_text = f"{wr:.1f}%" if wr is not None else "n/a"
        print(
            f"{row['setup_type']}/{row['variant']}: n={row['n']} "
            f"final={row['final_n'] or 0} first-hit={decided} WR={wr_text} "
            f"MFE60={_fmt(row['mfe60'])} MAE60={_fmt(row['mae60'])} "
            f"MFE180={_fmt(row['mfe180'])} MAE180={_fmt(row['mae180'])} "
            f"retest_delay={_fmt(row['retest_delay'])}s "
            f"level_age={_fmt(row['level_age'])}s "
            f"touches={_fmt(row['level_touches'])}"
        )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    main()
