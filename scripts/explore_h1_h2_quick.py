#!/usr/bin/env python3
"""Быстрый exploratory-анализ enriched CSV: какой edge у H1/H2 buckets?

ДО полноценного `test_h1_h5_filters.py` — проверяем что фильтры в принципе
имеют сигнал, чтобы не тратить время на rigorous statistical testing
если едва ли есть direction.

НЕ ИСПОЛЬЗУЕТСЯ для финальных решений! Это только sanity probe.
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path


def summary(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"{label}: empty")
        return
    pnls = [float(r["net_pips"]) for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = 100 * len(wins) / n
    net = sum(pnls)
    avg = net / n
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    avg_w = statistics.mean(wins) if wins else 0
    avg_l = statistics.mean(losses) if losses else 0
    print(f"{label:48s} n={n:4d}  WR={wr:5.1f}%  net={net:+7.0f}p  avg={avg:+6.1f}p  "
          f"avg_w={avg_w:+6.1f}  avg_l={avg_l:+6.1f}  PF={pf:.2f}")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: explore_h1_h2_quick.py <enriched.csv>", file=sys.stderr)
        return 1
    path = Path(sys.argv[1])
    rows = list(csv.DictReader(path.open()))
    print(f"Loaded {len(rows)} rows from {path}\n")

    is_rows = [r for r in rows if r["is_oos"] == "False"]
    oos_rows = [r for r in rows if r["is_oos"] == "True"]

    print("─── BASELINE ───")
    summary(rows, "ALL")
    summary(is_rows, "  IS")
    summary(oos_rows, "  OOS")

    print("\n─── H1: ORB internal direction ───")
    for partition_label, partition in [("ALL", rows), ("IS", is_rows), ("OOS", oos_rows)]:
        aligned = [r for r in partition if r["h1_aligned"] == "True"]
        not_aligned = [r for r in partition if r["h1_aligned"] == "False"]
        print(f"  --- {partition_label} ---")
        summary(aligned,     f"    [aligned] trade-dir == orb-dir")
        summary(not_aligned, f"    [contra]  trade-dir != orb-dir")

    print("\n─── H2: ATR percentile regime ───")
    for partition_label, partition in [("ALL", rows), ("IS", is_rows), ("OOS", oos_rows)]:
        compression = [r for r in partition if r["h2_regime"] == "compression"]
        normal = [r for r in partition if r["h2_regime"] == "normal"]
        expansion = [r for r in partition if r["h2_regime"] == "expansion"]
        print(f"  --- {partition_label} ---")
        summary(compression, f"    [compression] ATR pct < 30")
        summary(normal,      f"    [normal]      ATR pct 30..70")
        summary(expansion,   f"    [expansion]   ATR pct > 70")

    print("\n─── H1 × H2 интеракция ───")
    for h1 in ("True", "False"):
        for regime in ("compression", "normal", "expansion"):
            bucket = [r for r in rows
                      if r["h1_aligned"] == h1 and r["h2_regime"] == regime]
            label = f"  H1={'aligned' if h1=='True' else 'contra ':9s}  H2={regime:12s}"
            summary(bucket, label)

    print("\n─── Session breakdown ───")
    for sess in ("london", "ny"):
        bucket = [r for r in rows if r["session"] == sess]
        summary(bucket, f"  session={sess}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
