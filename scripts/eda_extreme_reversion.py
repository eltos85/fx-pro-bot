#!/usr/bin/env python3
"""Extreme-reversion analysis: что происходит после бара с |return| > k·σ?

Классический edge в "хвостах распределения": большое движение часто оверреакт →
следующие N баров показывают возврат.

Метрика:
- Для каждого inst+TF: находим все бары с |return| > k·σ_20
- Смотрим на средний return в следующих N барах (N=1,2,4,8)
- Отдельно для UP-шоков и DOWN-шоков (в идеале: mean next return = −k·spike)
- Сравниваем net (с учётом cost FxPro)

Запуск:
    PYTHONPATH=src python3 -m scripts.eda_extreme_reversion --timeframe M15 --k 2.5
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

INSTRUMENTS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F", "NG=F", "ES=F",
]

# Примерный cost R-T в pips для 0.01 lot (спред + commission)
COST_PIPS = {
    "EURUSD=X": 1.8, "GBPUSD=X": 2.2, "USDJPY=X": 1.8, "AUDUSD=X": 2.2,
    "USDCAD=X": 2.3, "USDCHF=X": 2.3, "EURGBP=X": 3.9, "EURJPY=X": 2.5, "GBPJPY=X": 3.0,
    "GC=F": 40.0, "CL=F": 3.5, "BZ=F": 3.5, "NG=F": 3.5, "ES=F": 2.0,
}


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    if not path.exists():
        return np.array([])
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((
                int(r["timestamp"]) // 1000,
                float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]),
                float(r["volume"]),
            ))
    dt = np.dtype([
        ("ts", "i8"), ("open", "f8"), ("high", "f8"),
        ("low", "f8"), ("close", "f8"), ("volume", "f8"),
    ])
    return np.array(rows, dtype=dt)


def resample(arr: np.ndarray, minutes: int) -> np.ndarray:
    if minutes == 5:
        return arr
    sec = minutes * 60
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        g = arr[s:e]
        out[i] = (
            unique[i], g["open"][0], g["high"].max(),
            g["low"].min(), g["close"][-1], g["volume"].sum(),
        )
    return out


def extreme_reversion(
    arr: np.ndarray, sym: str,
    k: float = 2.5,
    lookback: int = 20,
    horizons: tuple[int, ...] = (1, 2, 4, 8, 16),
) -> dict:
    c = arr["close"]
    rets = np.log(c[1:] / c[:-1])
    ps = pip_size(sym)
    ref = float(np.mean(c))
    rets_pips = rets * ref / ps

    if len(rets) < lookback + max(horizons) + 10:
        return {}

    # rolling std (z-score на последние lookback баров)
    # Для простоты используем предшествующие lookback значений до момента i
    roll_std = np.array([
        np.std(rets[max(0, i - lookback):i]) if i >= 5 else 0.0
        for i in range(len(rets))
    ])
    z = np.where(roll_std > 0, rets / roll_std, 0)

    up_mask = z > k
    dn_mask = z < -k

    result: dict = {
        "n_up": int(up_mask.sum()),
        "n_dn": int(dn_mask.sum()),
        "horizons": {},
        "cost_rt": COST_PIPS.get(sym, 5.0),
    }

    for N in horizons:
        # Для up-spike: средний cumulative return в следующих N барах, ожидаем negative (reversion)
        up_idx = np.where(up_mask[:-N])[0]
        dn_idx = np.where(dn_mask[:-N])[0]
        if len(up_idx) < 5 or len(dn_idx) < 5:
            result["horizons"][N] = None
            continue
        # Cumulative return в горизонте: log(close[i+N]/close[i+1])
        # Используем +1 потому что i — индекс в rets (т.е. бар i+1 в arr).
        up_next = np.array([
            np.sum(rets_pips[i + 1:i + 1 + N])
            for i in up_idx
        ])
        dn_next = np.array([
            np.sum(rets_pips[i + 1:i + 1 + N])
            for i in dn_idx
        ])
        # MR strategy: после up-spike SHORT → profit = -up_next; после dn-spike LONG → profit = dn_next
        mr_profit = np.concatenate([-up_next, dn_next])
        gross = float(np.mean(mr_profit))
        net = gross - result["cost_rt"]
        t, p = stats.ttest_1samp(mr_profit, 0.0)
        # WR
        wr = float(np.mean(mr_profit > 0))
        wr_net = float(np.mean(mr_profit > result["cost_rt"]))
        result["horizons"][N] = {
            "n_trades": len(mr_profit),
            "gross_mean_pips": gross,
            "net_mean_pips": net,
            "t": float(t),
            "p": float(p),
            "wr_gross": wr,
            "wr_after_cost": wr_net,
            "up_next_mean": float(np.mean(up_next)),
            "dn_next_mean": float(np.mean(dn_next)),
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["M5", "M15", "H1"], default="M15")
    ap.add_argument("--k", type=float, default=2.5, help="sigma threshold for extreme bar")
    ap.add_argument("--lookback", type=int, default=20)
    args = ap.parse_args()

    minutes = {"M5": 5, "M15": 15, "H1": 60}[args.timeframe]

    print("=" * 110)
    print(
        f"EXTREME REVERSION | TF={args.timeframe} | k={args.k}σ (rolling {args.lookback}-bar std)"
    )
    print("=" * 110)

    horizons = (1, 2, 4, 8, 16)

    # заголовок
    print(f"\n{'Symbol':<12}{'n_up':>6}{'n_dn':>6}{'cost':>7} | "
          + "".join(
              f"{'H='+str(h):^28}" for h in horizons
          ))
    print(f"{'':<31} | "
          + "".join(
              f"{'gross  net  WR%  p':^28}" for _ in horizons
          ))
    print("-" * (31 + 28 * len(horizons) + 3))

    summary_rows = []
    for sym in INSTRUMENTS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"[WARN] no data: {sym}")
            continue
        arr = resample(raw, minutes)
        res = extreme_reversion(arr, sym, k=args.k, lookback=args.lookback, horizons=horizons)
        if not res:
            continue

        row = f"{sym:<12}{res['n_up']:>6}{res['n_dn']:>6}{res['cost_rt']:>7.1f} | "
        for h in horizons:
            d = res["horizons"].get(h)
            if d is None:
                row += f"{'---':^28}"
                continue
            flag = "**" if d["p"] < 0.01 else "*" if d["p"] < 0.05 else " "
            net_color = "+" if d["net_mean_pips"] > 0 else ""
            cell = f"{d['gross_mean_pips']:+5.1f} {d['net_mean_pips']:+5.1f} {d['wr_after_cost']*100:4.1f} p={d['p']:.2f}{flag}"
            row += f"{cell:^28}"
        print(row)

        # собираем лучший горизонт по net
        best_h = None
        best_net = -1e9
        for h in horizons:
            d = res["horizons"].get(h)
            if d and d["net_mean_pips"] > best_net and d["p"] < 0.05:
                best_net = d["net_mean_pips"]
                best_h = h
        if best_h is not None:
            d = res["horizons"][best_h]
            summary_rows.append({
                "sym": sym, "h": best_h,
                "n_trades": d["n_trades"],
                "gross": d["gross_mean_pips"],
                "net": d["net_mean_pips"],
                "p": d["p"],
                "wr": d["wr_after_cost"],
                "cost": res["cost_rt"],
            })

    # ── Summary ──
    print("\n" + "=" * 110)
    print(f"BEST HORIZONS per instrument (p<0.05, net > 0)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'horizon':>10}{'n':>6}{'gross':>9}{'net':>9}{'cost':>8}{'WR%':>7}{'p':>9}")
    summary_rows.sort(key=lambda x: -x["net"])
    for r in summary_rows:
        flag = "✓ PROFITABLE" if r["net"] > 0 else "x"
        print(
            f"  {r['sym']:<10}H={r['h']:>5}{r['n_trades']:>6}"
            f"{r['gross']:>+9.2f}{r['net']:>+9.2f}"
            f"{r['cost']:>8.1f}{r['wr']*100:>7.1f}{r['p']:>9.4f}  {flag}"
        )
    if not summary_rows:
        print("  Нет значимых (p<0.05) net-profitable edges ни на одном инструменте.")


if __name__ == "__main__":
    main()
