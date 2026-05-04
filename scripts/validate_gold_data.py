#!/usr/bin/env python3
"""Validation скачанных M5 GC=F данных + IS/OOS split (70/30).

Проверяем перед запуском H1-H5 backtest:
1. Реальный диапазон дат (полный год?).
2. Количество баров vs теоретический максимум.
3. Гэпы > 1 часа (выходные XAUUSD = OK; всё что больше = проблема).
4. OHLC sanity: low ≤ open ≤ high, low ≤ close ≤ high, low ≤ high.
5. Volume > 0 для подавляющего большинства баров.
6. Datetime UTC consistency.
7. IS/OOS split: первые 70% по времени = IS, последние 30% = OOS hold-out.

Использование:
    python3 -m scripts.validate_gold_data --csv data/fxpro_klines/GC_F_M5.csv

Compliance: данные используются только для backtest H1-H5 в рамках
research-цикла (BUILDLOG 2026-05-04). OOS-партицию НЕ ИСПОЛЬЗУЕМ
для подбора параметров.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def load_bars(csv_path: Path) -> list[tuple[int, float, float, float, float, float]]:
    bars = []
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append((
                int(row["timestamp"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            ))
    return bars


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", required=True, help="Путь к CSV с M5-барами")
    p.add_argument(
        "--is-fraction", type=float, default=0.70,
        help="Доля для IS (default: 0.70 → 70% IS, 30% OOS)",
    )
    args = p.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"FAIL: {csv_path} не найден", file=sys.stderr)
        return 1

    bars = load_bars(csv_path)
    n = len(bars)
    if n == 0:
        print("FAIL: 0 баров", file=sys.stderr)
        return 1

    bars.sort(key=lambda b: b[0])

    ts_first_ms = bars[0][0]
    ts_last_ms = bars[-1][0]
    dt_first = datetime.fromtimestamp(ts_first_ms / 1000, UTC)
    dt_last = datetime.fromtimestamp(ts_last_ms / 1000, UTC)
    span_days = (ts_last_ms - ts_first_ms) / (1000 * 86400)

    print("─── BASIC ───")
    print(f"file:         {csv_path}")
    print(f"bars:         {n:,}")
    print(f"first ts:     {dt_first.isoformat()}")
    print(f"last ts:      {dt_last.isoformat()}")
    print(f"span:         {span_days:.1f} days")
    theoretical_5d = span_days * 5 / 7 * 24 * 12  # XAUUSD ≈ 5 days/week
    coverage = n / theoretical_5d * 100 if theoretical_5d else 0
    print(f"coverage:     {coverage:.1f}% of theoretical 5d/week")

    # OHLC sanity
    bad_ohlc = 0
    bad_ohlc_examples = []
    for b in bars:
        ts, o, h, l, c, v = b
        if not (l <= h and l <= o <= h and l <= c <= h):
            bad_ohlc += 1
            if len(bad_ohlc_examples) < 3:
                bad_ohlc_examples.append((ts, o, h, l, c))
    print("─── OHLC sanity ───")
    print(f"bad bars:     {bad_ohlc} ({bad_ohlc / n * 100:.4f}%)")
    if bad_ohlc_examples:
        for ts, o, h, l, c in bad_ohlc_examples:
            print(f"  example: ts={ts} O={o} H={h} L={l} C={c}")

    # Volume
    zero_vol = sum(1 for b in bars if b[5] <= 0)
    print(f"zero volume:  {zero_vol} ({zero_vol / n * 100:.4f}%)")
    vols = [b[5] for b in bars]
    vols_sorted = sorted(vols)
    median_vol = vols_sorted[n // 2]
    print(f"median vol:   {median_vol:.0f}")

    # Gap analysis
    print("─── GAPS ───")
    gap_buckets: Counter[str] = Counter()
    big_gaps: list[tuple[int, int, int]] = []
    EXPECTED_DELTA_MS = 5 * 60 * 1000
    for i in range(1, n):
        delta_ms = bars[i][0] - bars[i - 1][0]
        if delta_ms == EXPECTED_DELTA_MS:
            gap_buckets["normal_5m"] += 1
        elif delta_ms <= 30 * 60 * 1000:
            gap_buckets["≤30min"] += 1
        elif delta_ms <= 4 * 3600 * 1000:
            gap_buckets["30min-4h"] += 1
        elif delta_ms <= 50 * 3600 * 1000:
            gap_buckets["4h-50h_weekend"] += 1
        else:
            gap_buckets[">50h_unusual"] += 1
            big_gaps.append((bars[i - 1][0], bars[i][0], delta_ms))
    for k, v in sorted(gap_buckets.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} {v:6d}")
    if big_gaps:
        print(f"  unusual gaps (>50h, n={len(big_gaps)}, top 3):")
        for ts1, ts2, dms in big_gaps[:3]:
            d1 = datetime.fromtimestamp(ts1 / 1000, UTC).isoformat()
            d2 = datetime.fromtimestamp(ts2 / 1000, UTC).isoformat()
            print(f"    {d1} → {d2} = {dms / 3600000:.1f}h")

    # Price range sanity
    closes = [b[4] for b in bars]
    print("─── PRICE ───")
    print(f"min close:    ${min(closes):.2f}")
    print(f"max close:    ${max(closes):.2f}")
    print(f"first close:  ${closes[0]:.2f}")
    print(f"last close:   ${closes[-1]:.2f}")
    print(f"return:       {(closes[-1] / closes[0] - 1) * 100:+.1f}%")

    # IS/OOS split
    print("─── IS / OOS SPLIT ───")
    split_idx = int(n * args.is_fraction)
    is_bars = bars[:split_idx]
    oos_bars = bars[split_idx:]
    is_first = datetime.fromtimestamp(is_bars[0][0] / 1000, UTC)
    is_last = datetime.fromtimestamp(is_bars[-1][0] / 1000, UTC)
    oos_first = datetime.fromtimestamp(oos_bars[0][0] / 1000, UTC)
    oos_last = datetime.fromtimestamp(oos_bars[-1][0] / 1000, UTC)
    is_span_d = (is_bars[-1][0] - is_bars[0][0]) / 86400000
    oos_span_d = (oos_bars[-1][0] - oos_bars[0][0]) / 86400000
    print(f"IS:           bars {len(is_bars):,} | {is_first.date()} → {is_last.date()} | {is_span_d:.0f} days")
    print(f"OOS:          bars {len(oos_bars):,} | {oos_first.date()} → {oos_last.date()} | {oos_span_d:.0f} days")
    print(f"split ts:     {is_bars[-1][0]}  ({is_last.isoformat()})")

    # Verdict
    print("─── VERDICT ───")
    issues = []
    if span_days < 350:
        issues.append(f"span only {span_days:.0f} days (<350)")
    if bad_ohlc / n > 0.001:
        issues.append(f"too many bad OHLC ({bad_ohlc / n * 100:.2f}%)")
    if len(big_gaps) > 5:
        issues.append(f"too many >50h gaps ({len(big_gaps)})")
    if coverage < 70:
        issues.append(f"low coverage ({coverage:.0f}%)")
    if not issues:
        print("OK ✓ — данные пригодны для H1-H5 backtest")
        return 0
    print("WARN — есть предупреждения:")
    for it in issues:
        print(f"  - {it}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
