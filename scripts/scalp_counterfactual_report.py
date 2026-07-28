#!/usr/bin/env python3
"""Read-only evidence report по counterfactual_setups.

Считает НЕЗАВИСИМЫЕ эпизоды, а не строки: canon-тени до v0.18.47 писались на
каждом тике живого сетапа (см. scripts/scalp_episodes.py). Колонка `строк`
показывает исходный объём, `n` — эпизоды после схлопывания; их расхождение и
есть степень дублирования.

Usage:
  python scripts/scalp_counterfactual_report.py data/scalp_bot.sqlite \
      --since 2026-07-22T13:30:00+00:00
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_episodes import DEDUPED_SETUP_TYPES, collapse_episodes  # noqa: E402


def _timestamp(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _wilson(wins: int, n: int) -> tuple[float, float] | None:
    """95% CI по Уилсону — на малых n нормальное приближение врёт."""
    if not n:
        return None
    z = 1.96
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half) * 100.0, min(1.0, centre + half) * 100.0)


def _summarize(rows: list[sqlite3.Row], raw_n: int) -> dict:
    target = sum(1 for r in rows if r["outcome_target"] == "target")
    sl = sum(1 for r in rows if r["outcome_target"] == "sl")
    decided = target + sl
    return {
        "raw": raw_n,
        "n": len(rows),
        "final": sum(1 for r in rows if r["state"] == "final"),
        "decided": decided,
        "wr": 100.0 * target / decided if decided else None,
        "ci": _wilson(target, decided),
        "mfe60": _mean([r["mfe_r_60"] for r in rows if r["mfe_r_60"] is not None]),
        "mae60": _mean([r["mae_r_60"] for r in rows if r["mae_r_60"] is not None]),
        "mfe180": _mean(
            [r["mfe_r_180"] for r in rows if r["mfe_r_180"] is not None]),
        "mae180": _mean(
            [r["mae_r_180"] for r in rows if r["mae_r_180"] is not None]),
        "level_age": _mean(
            [r["level_age_sec"] for r in rows if r["level_age_sec"] is not None]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("--since", default="1970-01-01T00:00:00+00:00")
    args = parser.parse_args()
    since = _timestamp(args.since)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    raw = conn.execute(
        "SELECT setup_type,variant,symbol,side,level_type,level_price,"
        "ts_candidate,state,outcome_target,mfe_r_60,mae_r_60,mfe_r_180,"
        "mae_r_180,level_age_sec FROM counterfactual_setups "
        "WHERE ts_candidate>=? ORDER BY setup_type,variant,ts_candidate",
        (since,),
    ).fetchall()
    conn.close()

    print(f"source={args.db} since={args.since} UTC observational-only")
    if not raw:
        print("NO DATA")
        return

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in raw:
        grouped[(row["setup_type"], row["variant"])].append(row)

    print(f"{'сетап/ветка':46} {'строк':>6} {'эпиз':>5} {'фин':>5} "
          f"{'реш':>5} {'WR':>7} {'95% CI':>15} {'MFE60':>6} {'MAE60':>6} "
          f"{'MFE180':>7} {'MAE180':>7} {'возраст':>9}")
    for (setup_type, variant), rows in sorted(grouped.items()):
        raw_n = len(rows)
        if setup_type in DEDUPED_SETUP_TYPES:
            rows = collapse_episodes(rows)
        s = _summarize(rows, raw_n)
        ci = (f"[{s['ci'][0]:5.1f};{s['ci'][1]:5.1f}]" if s["ci"]
              else " " * 15)
        wr = f"{s['wr']:6.1f}%" if s["wr"] is not None else "    n/a"
        age = (f"{s['level_age'] / 3600.0:8.1f}ч"
               if s["level_age"] is not None else "      n/a")
        print(f"{setup_type + '/' + variant:46} {s['raw']:>6} {s['n']:>5} "
              f"{s['final']:>5} {s['decided']:>5} {wr} {ci:>15} "
              f"{_fmt(s['mfe60']):>6} {_fmt(s['mae60']):>6} "
              f"{_fmt(s['mfe180']):>7} {_fmt(s['mae180']):>7} {age}")

    inflated = [(k, len(v)) for k, v in grouped.items()
                if k[0] in DEDUPED_SETUP_TYPES
                and len(v) > len(collapse_episodes(v))]
    if inflated:
        print("\nстроки схлопнуты в эпизоды для: "
              + ", ".join(sorted({k[0] for k, _ in inflated}))
              + " (см. scripts/scalp_episodes.py)")


if __name__ == "__main__":
    main()
