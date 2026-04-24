#!/usr/bin/env python3
"""Cointegration Scan для всех 36 пар валютных комбинаций.

Мировая практика (Engle-Granger, Quant Decoded 2025, Lemishko 2024):
1. ADF test на residuals OLS-регрессии → cointegration p-value
2. Половина жизни (half-life) mean-reversion должна быть 5-60 дней
3. Отбираем пары с p < 0.05 И 5 ≤ half-life ≤ 60
4. Эти пары — кандидаты для pairs trading

Входные данные: daily closes всех 9 FX пар за 90 дней.

Запуск:
    PYTHONPATH=src python3 -m scripts.cointegration_scan
"""

from __future__ import annotations

import argparse
import csv
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy import stats
from statsmodels.tsa.stattools import adfuller, coint
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

FX_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
]


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
                float(r["close"]),
            ))
    dt = np.dtype([("ts", "i8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample_to_daily(arr: np.ndarray) -> np.ndarray:
    """Daily close: берём last close каждого UTC дня."""
    if len(arr) == 0:
        return arr
    day_sec = 86400
    block = (arr["ts"] // day_sec) * day_sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["close"][e - 1])
    return out


def resample_to_H1(arr: np.ndarray) -> np.ndarray:
    h_sec = 3600
    block = (arr["ts"] // h_sec) * h_sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["close"][e - 1])
    return out


def align(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    common = np.intersect1d(a["ts"], b["ts"])
    if len(common) < 30:
        return np.array([]), np.array([])
    a_map = {t: i for i, t in enumerate(a["ts"])}
    b_map = {t: i for i, t in enumerate(b["ts"])}
    ai = np.array([a_map[t] for t in common])
    bi = np.array([b_map[t] for t in common])
    return a["close"][ai], b["close"][bi]


def compute_hedge_ratio(y: np.ndarray, x: np.ndarray) -> tuple[float, float, np.ndarray]:
    """OLS: y = alpha + beta * x. Returns (alpha, beta, residuals)."""
    X = add_constant(x)
    model = OLS(y, X).fit()
    alpha = float(model.params[0])
    beta = float(model.params[1])
    resid = y - (alpha + beta * x)
    return alpha, beta, resid


def compute_half_life(spread: np.ndarray) -> float:
    """Half-life mean-reversion через AR(1) модель.

    ΔS_t = λ·S_{t-1} + ε → half-life = -ln(2) / ln(1 + λ)
    Если spread non-mean-reverting, возвращает np.inf.
    """
    s = spread
    if len(s) < 10:
        return float("inf")
    # регрессия ΔS на S_{t-1}
    ds = np.diff(s)
    s_lag = s[:-1]
    X = add_constant(s_lag)
    try:
        model = OLS(ds, X).fit()
        lam = float(model.params[1])
        if lam >= 0:
            return float("inf")
        hl = -np.log(2) / np.log(1 + lam)
        return float(hl)
    except Exception:
        return float("inf")


def adf_test(series: np.ndarray) -> tuple[float, float]:
    """ADF test. Returns (stat, p_value)."""
    if len(series) < 15:
        return 0.0, 1.0
    try:
        result = adfuller(series, autolag="AIC")
        return float(result[0]), float(result[1])
    except Exception:
        return 0.0, 1.0


def engle_granger(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float, np.ndarray]:
    """Engle-Granger 2-step: regress y on x, test residuals for stationarity.

    Returns (beta, adf_stat, adf_pvalue, spread).
    """
    alpha, beta, resid = compute_hedge_ratio(y, x)
    stat, p = adf_test(resid)
    return beta, stat, p, resid


def coint_test_statsmodels(y: np.ndarray, x: np.ndarray) -> float:
    """statsmodels.tsa.stattools.coint — прямой test. Returns p-value."""
    try:
        _, p, _ = coint(y, x)
        return float(p)
    except Exception:
        return 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["daily", "H1"], default="daily",
                    help="daily рекомендуется mirror мировой практике, H1 для large sample")
    args = ap.parse_args()

    print("=" * 110)
    print(f"COINTEGRATION SCAN | 9 FX pairs → 36 combinations | timeframe={args.timeframe}")
    print("Мировая практика (Engle-Granger + statsmodels.coint): p < 0.05 & half-life 5-60")
    print("=" * 110)

    # Load data
    data = {}
    for sym in FX_PAIRS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"[WARN] no data: {sym}")
            continue
        if args.timeframe == "daily":
            data[sym] = resample_to_daily(raw)
        else:
            data[sym] = resample_to_H1(raw)
        print(f"  {sym:<12} bars={len(data[sym])}")

    # Scan all combinations
    print("\n" + "=" * 110)
    print(f"ALL 36 PAIR COMBINATIONS (p-value: ADF on residuals & statsmodels.coint)")
    print("-" * 110)
    header = (f"{'Pair (Y ~ X)':<28}{'β':>8}{'ADF_p':>10}{'coint_p':>10}"
              f"{'half-life':>12}{'spread_std':>12}  Verdict")
    print(header)

    results = []
    for a, b in combinations(FX_PAIRS, 2):
        if a not in data or b not in data:
            continue
        y, x = align(data[a], data[b])
        if len(y) < 20:
            continue
        beta, adf_stat, adf_p, spread = engle_granger(y, x)
        coint_p = coint_test_statsmodels(y, x)
        hl = compute_half_life(spread)
        spread_std = float(np.std(spread))

        verdict = ""
        if adf_p < 0.05 and coint_p < 0.05:
            if 5 <= hl <= 60:
                verdict = "✓ COINTEGRATED (tradeable half-life)"
            else:
                verdict = f"✓ coint but HL={hl:.1f} outside 5-60"
        elif adf_p < 0.10 or coint_p < 0.10:
            verdict = "~ marginal"
        else:
            verdict = "✗ not cointegrated"

        label = f"{a.replace('=X',''):<6} ~ {b.replace('=X',''):<6} β={beta:+.3f}"
        hl_str = f"{hl:.1f}" if hl != float("inf") else "inf"
        print(
            f"  {label:<28}{beta:>+8.3f}{adf_p:>10.4f}{coint_p:>10.4f}"
            f"{hl_str:>12}{spread_std:>12.4f}  {verdict}"
        )

        results.append({
            "y": a, "x": b, "beta": beta,
            "adf_p": adf_p, "coint_p": coint_p,
            "half_life": hl, "spread_std": spread_std,
            "tradeable": adf_p < 0.05 and coint_p < 0.05 and 5 <= hl <= 60,
        })

    # Summary
    print("\n" + "=" * 110)
    tradeable = [r for r in results if r["tradeable"]]
    cointegrated = [r for r in results if r["adf_p"] < 0.05 and r["coint_p"] < 0.05]
    marginal = [r for r in results if (r["adf_p"] < 0.10 or r["coint_p"] < 0.10) and not (r["adf_p"] < 0.05 and r["coint_p"] < 0.05)]
    print(f"TRADEABLE pairs (cointegrated + tradeable half-life):      {len(tradeable)} / {len(results)}")
    print(f"COINTEGRATED (any half-life):                              {len(cointegrated)} / {len(results)}")
    print(f"MARGINAL (p<0.10):                                         {len(marginal)} / {len(results)}")
    print(f"NOT COINTEGRATED:                                          {len(results) - len(cointegrated) - len(marginal)} / {len(results)}")

    if tradeable:
        print("\n" + "=" * 110)
        print("TOP TRADEABLE PAIRS (sorted by half-life — короткий HL = faster reversion)")
        print("-" * 110)
        tradeable.sort(key=lambda r: r["half_life"])
        for r in tradeable:
            print(f"  {r['y'].replace('=X',''):<7} ~ {r['x'].replace('=X',''):<7} "
                  f"β={r['beta']:+7.3f}  ADF_p={r['adf_p']:.4f}  "
                  f"coint_p={r['coint_p']:.4f}  HL={r['half_life']:.1f}d  "
                  f"σ={r['spread_std']:.4f}")

    if cointegrated and not tradeable:
        print("\n" + "=" * 110)
        print("COINTEGRATED но half-life вне 5-60 (показать для оценки)")
        print("-" * 110)
        for r in cointegrated:
            print(f"  {r['y'].replace('=X',''):<7} ~ {r['x'].replace('=X',''):<7} "
                  f"β={r['beta']:+7.3f}  ADF_p={r['adf_p']:.4f}  "
                  f"coint_p={r['coint_p']:.4f}  HL={r['half_life']:.1f}d")


if __name__ == "__main__":
    main()
