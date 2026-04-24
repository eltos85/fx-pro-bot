#!/usr/bin/env python3
"""EDA EXTENDED: полный data-driven поиск паттернов.

10 аналитических блоков:
1.  Hourly profile + DOW + gap (базовый EDA)
2.  Autocorrelation + Hurst
3.  Cross-asset lead-lag (DXY→FX, CL→CAD, ES→risk-FX, GC→JPY)
4.  Volatility regime split (high/low ATR)
5.  Session transitions (open/close 15-min windows)
6.  Volume-return bucket (momentum confirm vs fade)
7.  Monte Carlo / bootstrap p-correction (multiple comparisons)
8.  Range expansion после consolidation
9.  Gap-fill probability (weekend gaps)
10. Runs test (streaks of same-sign bars)
11. End-of-month seasonality
12. Body/wick ratio (trending vs choppy)

Запуск:
    PYTHONPATH=src python3 -m scripts.eda_fxpro_extended --timeframe M15 --full
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
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
            unique[i],
            g["open"][0], g["high"].max(),
            g["low"].min(), g["close"][-1],
            g["volume"].sum(),
        )
    return out


def log_rets(arr: np.ndarray) -> np.ndarray:
    c = arr["close"]
    return np.log(c[1:] / c[:-1])


def to_pips(r: np.ndarray, sym: str, ref: float) -> np.ndarray:
    return r * ref / pip_size(sym)


def atr(arr: np.ndarray, period: int = 14) -> np.ndarray:
    high = arr["high"]
    low = arr["low"]
    close = arr["close"]
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ),
    )
    out = np.zeros(len(arr))
    out[0] = 0
    out[1] = tr[0]
    for i in range(2, len(arr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out


# ─────────────────── базовые аналитики ───────────────────

def hourly_profile(arr: np.ndarray, sym: str):
    rets = log_rets(arr)
    hours = np.array([datetime.fromtimestamp(t, UTC).hour for t in arr["ts"][1:]])
    ref = float(np.mean(arr["close"]))
    rp = to_pips(rets, sym, ref)
    out = {}
    for h in range(24):
        m = hours == h
        n = int(m.sum())
        if n < 30:
            out[h] = (n, 0.0, 0.0, 1.0)
            continue
        d = rp[m]
        mean = float(np.mean(d))
        t, p = stats.ttest_1samp(d, 0.0)
        out[h] = (n, mean, float(t), float(p))
    return out


def autocorr(arr: np.ndarray, max_lag: int = 20):
    r = log_rets(arr)
    out = []
    for lag in range(1, max_lag + 1):
        if len(r) <= lag + 10:
            break
        x, y = r[:-lag], r[lag:]
        cor, p = stats.pearsonr(x, y)
        out.append((lag, float(cor), float(p)))
    return out


def hurst(arr: np.ndarray) -> float:
    c = arr["close"]
    if len(c) < 200:
        return 0.5
    r = np.diff(np.log(c))
    lags = np.unique(np.logspace(np.log10(20), np.log10(min(500, len(r) // 2)), 15).astype(int))
    rs = []
    for lag in lags:
        n = len(r) // lag
        if n < 2:
            continue
        vals = []
        for b in range(n):
            blk = r[b * lag:(b + 1) * lag]
            dev = blk - blk.mean()
            Z = np.cumsum(dev)
            R = Z.max() - Z.min()
            S = blk.std()
            if S > 0:
                vals.append(R / S)
        if vals:
            rs.append((lag, np.mean(vals)))
    if len(rs) < 4:
        return 0.5
    slope, *_ = stats.linregress(np.log([v[0] for v in rs]), np.log([v[1] for v in rs]))
    return float(slope)


# ─────────────────── 3. Cross-asset lead-lag ───────────────────

def align_series(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Выровнять по timestamp (inner join). Возвращает returns."""
    common = np.intersect1d(a["ts"], b["ts"])
    if len(common) < 100:
        return np.array([]), np.array([])
    a_map = {t: i for i, t in enumerate(a["ts"])}
    b_map = {t: i for i, t in enumerate(b["ts"])}
    ai = np.array([a_map[t] for t in common])
    bi = np.array([b_map[t] for t in common])
    ac = a["close"][ai]
    bc = b["close"][bi]
    ra = np.log(ac[1:] / ac[:-1])
    rb = np.log(bc[1:] / bc[:-1])
    return ra, rb


def lead_lag_scan(data: dict[str, np.ndarray], pairs: list[tuple[str, str]], max_lag: int = 5):
    """Для каждой пары (leader, follower) считает corr(lead[t], follower[t+k]) для k=-max..+max.

    Отрицательный k = follower лидирует, положительный = leader лидирует.
    """
    results = []
    for lead, follow in pairs:
        if lead not in data or follow not in data:
            continue
        ra, rb = align_series(data[lead], data[follow])
        if len(ra) < 500:
            continue
        best_k, best_r, best_p = 0, 0.0, 1.0
        row = []
        for k in range(-max_lag, max_lag + 1):
            if k > 0:
                x, y = ra[:-k], rb[k:]
            elif k < 0:
                x, y = ra[-k:], rb[:k]
            else:
                x, y = ra, rb
            if len(x) < 100:
                continue
            cor, p = stats.pearsonr(x, y)
            row.append((k, float(cor), float(p)))
            if abs(cor) > abs(best_r):
                best_k, best_r, best_p = k, float(cor), float(p)
        results.append((lead, follow, best_k, best_r, best_p, row))
    return results


# ─────────────────── 4. Volatility regime split ───────────────────

def vol_regime_split(arr: np.ndarray, sym: str):
    """Разделить бары на high-vol (top tercile ATR) и low-vol (bottom tercile)."""
    a = atr(arr, 14)
    # убираем первые 20 (warmup)
    r = log_rets(arr)[20:]
    a = a[21:21 + len(r)]
    if len(r) < 100:
        return None
    ref = float(np.mean(arr["close"]))
    rp = to_pips(r, sym, ref)
    hi_th = np.quantile(a, 0.67)
    lo_th = np.quantile(a, 0.33)
    hi_mask = a >= hi_th
    lo_mask = a <= lo_th
    hi_r = rp[hi_mask]
    lo_r = rp[lo_mask]

    def _stats(x):
        if len(x) < 30:
            return (0, 0.0, 0.0, 1.0, 0.0)
        mean = float(np.mean(x))
        std = float(np.std(x))
        t, p = stats.ttest_1samp(x, 0.0)
        # autocorr lag-1
        if len(x) > 50:
            ac, _ = stats.pearsonr(x[:-1], x[1:])
        else:
            ac = 0.0
        return (len(x), mean, std, float(p), float(ac))

    return {"high_vol": _stats(hi_r), "low_vol": _stats(lo_r)}


# ─────────────────── 5. Session transitions ───────────────────

TRANSITIONS = [
    ("asia_open",     (0, 0), (0, 30)),     # 00:00-00:30 UTC
    ("tokyo_close",   (7, 0), (7, 30)),     # 07:00-07:30 UTC
    ("london_open",   (8, 0), (8, 30)),     # 08:00-08:30 UTC
    ("ny_open",       (13, 0), (13, 30)),   # 13:00-13:30 UTC
    ("london_close",  (16, 0), (16, 30)),   # 16:00-16:30 UTC
    ("ny_close",      (20, 0), (20, 30)),   # 20:00-20:30 UTC
]


def session_transitions(arr: np.ndarray, sym: str):
    ts = arr["ts"][1:]
    r = log_rets(arr)
    ref = float(np.mean(arr["close"]))
    rp = to_pips(r, sym, ref)
    out = {}
    for name, (sh, sm), (eh, em) in TRANSITIONS:
        mask = np.array([
            (datetime.fromtimestamp(t, UTC).hour * 60 + datetime.fromtimestamp(t, UTC).minute) in
            range(sh * 60 + sm, eh * 60 + em + 1)
            for t in ts
        ])
        n = int(mask.sum())
        if n < 20:
            out[name] = (n, 0.0, 1.0)
            continue
        d = rp[mask]
        mean = float(np.mean(d))
        _, p = stats.ttest_1samp(d, 0.0)
        out[name] = (n, mean, float(p))
    return out


# ─────────────────── 6. Volume-return bucket ───────────────────

def volume_bucket(arr: np.ndarray, sym: str):
    vol = arr["volume"][1:]
    r = log_rets(arr)
    if len(vol) < 100 or float(vol.std()) == 0:
        return None
    ref = float(np.mean(arr["close"]))
    rp = to_pips(r, sym, ref)
    med = np.median(vol)
    hi = vol > med
    lo = vol <= med
    # |return| by bucket
    abs_hi = np.abs(rp[hi])
    abs_lo = np.abs(rp[lo])
    # Autocorr lag-1 within bucket (если vol→momentum, hi должен иметь +ACF)
    if len(rp[hi]) > 50:
        hi_ac, _ = stats.pearsonr(rp[hi][:-1], rp[hi][1:])
    else:
        hi_ac = 0.0
    if len(rp[lo]) > 50:
        lo_ac, _ = stats.pearsonr(rp[lo][:-1], rp[lo][1:])
    else:
        lo_ac = 0.0
    # t-test: отличается ли abs return в hi vs lo?
    _, p_vol = stats.mannwhitneyu(abs_hi, abs_lo, alternative="greater")
    return {
        "hi_n": int(hi.sum()),
        "lo_n": int(lo.sum()),
        "hi_abs_mean": float(np.mean(abs_hi)),
        "lo_abs_mean": float(np.mean(abs_lo)),
        "hi_ac": float(hi_ac),
        "lo_ac": float(lo_ac),
        "p_vol_effect": float(p_vol),
    }


# ─────────────────── 7. Monte Carlo / bootstrap p-correction ───────────────────

def mc_bootstrap_hourly(arr: np.ndarray, sym: str, n_boot: int = 1000):
    """Для каждого часа: реальный t-stat vs распределение при перемешанных часах.

    Возвращает dict[hour] → (real_t, mc_p) — mc_p это % перемешиваний с |t| >= real.
    """
    rets = log_rets(arr)
    hours = np.array([datetime.fromtimestamp(t, UTC).hour for t in arr["ts"][1:]])
    ref = float(np.mean(arr["close"]))
    rp = to_pips(rets, sym, ref)
    rng = np.random.default_rng(42)

    # Реальные t-stats
    real_ts = {}
    for h in range(24):
        m = hours == h
        if int(m.sum()) < 30:
            real_ts[h] = 0.0
            continue
        t, _ = stats.ttest_1samp(rp[m], 0.0)
        real_ts[h] = float(t)

    # MC: перемешиваем связь hour↔return
    mc_dist = {h: [] for h in range(24)}
    for _ in range(n_boot):
        shuffled_hours = rng.permutation(hours)
        for h in range(24):
            m = shuffled_hours == h
            if int(m.sum()) < 30:
                continue
            t, _ = stats.ttest_1samp(rp[m], 0.0)
            mc_dist[h].append(float(t))

    mc_p = {}
    for h in range(24):
        real = real_ts[h]
        dist = mc_dist[h]
        if not dist:
            mc_p[h] = 1.0
            continue
        # two-sided: доля |mc_t| >= |real_t|
        mc_p[h] = float(np.mean(np.abs(dist) >= abs(real)))

    return real_ts, mc_p


# ─────────────────── 8. Range expansion после consolidation ───────────────────

def range_expansion(arr: np.ndarray, sym: str, n_consol: int = 6, n_after: int = 4):
    """Если N подряд баров имеют range < ATR/2, какая вероятность breakout в следующие n_after?"""
    a = atr(arr, 14)
    ranges = arr["high"] - arr["low"]
    ref = float(np.mean(arr["close"]))
    hits = 0
    breakouts_up = 0
    breakouts_dn = 0
    total_move = []
    for i in range(20 + n_consol, len(arr) - n_after):
        consol = all(ranges[i - n_consol + 1 + k] < a[i] * 0.5 for k in range(n_consol))
        if not consol:
            continue
        hits += 1
        high_after = arr["high"][i + 1:i + n_after + 1].max()
        low_after = arr["low"][i + 1:i + n_after + 1].min()
        close_before = arr["close"][i]
        up_move = (high_after - close_before) / pip_size(sym)
        dn_move = (close_before - low_after) / pip_size(sym)
        total_move.append((up_move, dn_move))
        if up_move > a[i] / pip_size(sym):
            breakouts_up += 1
        if dn_move > a[i] / pip_size(sym):
            breakouts_dn += 1
    if hits < 10:
        return None
    return {
        "n_consol_events": hits,
        "breakout_up_rate": breakouts_up / hits,
        "breakout_dn_rate": breakouts_dn / hits,
        "avg_up_move_pips": float(np.mean([t[0] for t in total_move])),
        "avg_dn_move_pips": float(np.mean([t[1] for t in total_move])),
    }


# ─────────────────── 9. Gap-fill probability ───────────────────

def gap_fill(arr: np.ndarray, sym: str, n_bars_fill: int = 48):
    """На каждое пн-open: gap = open - prev_close. Вероятность заполнения за n_bars."""
    ps = pip_size(sym)
    events = []
    for i in range(1, len(arr)):
        dt = datetime.fromtimestamp(int(arr["ts"][i]), UTC)
        prev_dt = datetime.fromtimestamp(int(arr["ts"][i - 1]), UTC)
        # пн-open после выходных: бар в понедельник и предыдущий в пт (или скип > 1 часа)
        if dt.weekday() == 0 and (prev_dt.weekday() >= 4 or (dt - prev_dt).total_seconds() > 7200):
            gap = arr["open"][i] - arr["close"][i - 1]
            if abs(gap) / ps < 2:
                continue
            # ищем заполнение
            filled = False
            fill_bar = None
            for k in range(1, min(n_bars_fill + 1, len(arr) - i)):
                if gap > 0:
                    if arr["low"][i + k] <= arr["close"][i - 1]:
                        filled = True
                        fill_bar = k
                        break
                else:
                    if arr["high"][i + k] >= arr["close"][i - 1]:
                        filled = True
                        fill_bar = k
                        break
            events.append({
                "gap_pips": float(gap / ps),
                "filled": filled,
                "fill_bar": fill_bar,
            })
    if len(events) < 3:
        return None
    fill_rate = sum(1 for e in events if e["filled"]) / len(events)
    avg_fill_bars = np.mean([e["fill_bar"] for e in events if e["filled"]]) if any(e["filled"] for e in events) else None
    avg_abs_gap = np.mean([abs(e["gap_pips"]) for e in events])
    return {
        "n_gaps": len(events),
        "fill_rate": float(fill_rate),
        "avg_fill_bars": float(avg_fill_bars) if avg_fill_bars else None,
        "avg_abs_gap_pips": float(avg_abs_gap),
    }


# ─────────────────── 10. Runs test ───────────────────

def runs_test(arr: np.ndarray):
    r = log_rets(arr)
    signs = np.sign(r)
    signs = signs[signs != 0]
    n1 = int((signs > 0).sum())
    n2 = int((signs < 0).sum())
    n = n1 + n2
    if n < 50:
        return None
    # считаем runs
    runs = 1 + int((np.diff(signs) != 0).sum())
    expected = 2 * n1 * n2 / n + 1
    variance = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1))
    if variance <= 0:
        return None
    z = (runs - expected) / math.sqrt(variance)
    p = 2 * (1 - stats.norm.cdf(abs(z)))
    # интерпретация
    if z < -2:
        interp = "streak (persistence)"
    elif z > 2:
        interp = "anti-streak (alternation)"
    else:
        interp = "random"
    return {
        "runs": runs, "expected": float(expected),
        "z": float(z), "p": float(p), "interp": interp,
    }


# ─────────────────── 11. End-of-month seasonality ───────────────────

def end_of_month(arr: np.ndarray, sym: str):
    ts = arr["ts"][1:]
    r = log_rets(arr)
    ref = float(np.mean(arr["close"]))
    rp = to_pips(r, sym, ref)
    # для каждого бара: день месяца с конца (0 = последний день)
    dates = [datetime.fromtimestamp(t, UTC) for t in ts]
    # Найти последний день каждого (year, month)
    ym = [(d.year, d.month) for d in dates]
    last_day = {}
    for d in dates:
        k = (d.year, d.month)
        last_day[k] = max(last_day.get(k, 0), d.day)
    days_from_end = np.array([
        last_day[ym[i]] - dates[i].day
        for i in range(len(dates))
    ])
    # бакеты: 0-1 (ultra-end), 2-3, 4-7, 8+
    buckets = {
        "last_2d": days_from_end <= 1,
        "last_3-4d": (days_from_end >= 2) & (days_from_end <= 3),
        "mid_month": (days_from_end >= 4) & (days_from_end <= 15),
        "early_month": days_from_end > 15,
    }
    out = {}
    for name, m in buckets.items():
        n = int(m.sum())
        if n < 30:
            out[name] = (n, 0.0, 1.0)
            continue
        d = rp[m]
        mean = float(np.mean(d))
        _, p = stats.ttest_1samp(d, 0.0)
        out[name] = (n, mean, float(p))
    return out


# ─────────────────── 12. Body/wick ratio ───────────────────

def body_wick(arr: np.ndarray):
    body = np.abs(arr["close"] - arr["open"])
    upper_wick = arr["high"] - np.maximum(arr["close"], arr["open"])
    lower_wick = np.minimum(arr["close"], arr["open"]) - arr["low"]
    total = arr["high"] - arr["low"]
    valid = total > 0
    body_ratio = body[valid] / total[valid]
    upper_ratio = upper_wick[valid] / total[valid]
    lower_ratio = lower_wick[valid] / total[valid]
    return {
        "body_mean": float(body_ratio.mean()),
        "body_median": float(np.median(body_ratio)),
        "upper_wick_mean": float(upper_ratio.mean()),
        "lower_wick_mean": float(lower_ratio.mean()),
        "trending_frac": float((body_ratio > 0.7).mean()),
        "indecision_frac": float((body_ratio < 0.2).mean()),
    }


# ─────────────────── main ───────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["M5", "M15", "H1"], default="M15")
    ap.add_argument("--mc-boot", type=int, default=500)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    minutes = {"M5": 5, "M15": 15, "H1": 60}[args.timeframe]

    print("=" * 110)
    print(f"EDA EXTENDED | timeframe={args.timeframe} | 14 instruments | MC-boot={args.mc_boot}")
    print("=" * 110)

    # Load все
    data: dict[str, np.ndarray] = {}
    for sym in INSTRUMENTS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"[WARN] no data: {sym}")
            continue
        data[sym] = resample(raw, minutes)

    # ── 1-2. Hourly + autocorr + Hurst ──
    print("\n" + "=" * 110)
    print("[1-2] HURST + AUTOCORR lag-1 (data-driven regime)")
    print("-" * 110)
    for sym, arr in data.items():
        h = hurst(arr)
        ac = autocorr(arr, max_lag=5)
        l1 = next((c for lag, c, p in ac if lag == 1), 0.0)
        l1p = next((p for lag, c, p in ac if lag == 1), 1.0)
        regime = "TREND" if h > 0.55 else "MR" if h < 0.45 else "random"
        mark = "**" if l1p < 0.01 else "*" if l1p < 0.05 else ""
        print(f"  {sym:<12} Hurst={h:.3f} ({regime:>6})  ACF[1]={l1:+.4f} {mark:<2} p={l1p:.4f}")

    # ── 3. Cross-asset lead-lag ──
    print("\n" + "=" * 110)
    print("[3] CROSS-ASSET LEAD-LAG (best |corr| in [-5..+5] lag, + = leader→follower)")
    print("-" * 110)
    pairs = [
        # Commodity → FX
        ("CL=F", "USDCAD=X"),
        ("CL=F", "EURUSD=X"),
        ("GC=F", "USDJPY=X"),
        ("GC=F", "AUDUSD=X"),
        # Risk-on → FX
        ("ES=F", "AUDUSD=X"),
        ("ES=F", "EURUSD=X"),
        ("ES=F", "USDJPY=X"),
        # FX-to-FX
        ("EURUSD=X", "GBPUSD=X"),
        ("EURUSD=X", "AUDUSD=X"),
        ("EURJPY=X", "EURUSD=X"),
        ("USDJPY=X", "GBPJPY=X"),
        # Energies
        ("CL=F", "BZ=F"),
        ("CL=F", "NG=F"),
    ]
    ll = lead_lag_scan(data, pairs, max_lag=5)
    print(f"{'Leader':<12}{'Follower':<12}{'best_k':>8}{'corr':>10}{'p':>10}  Interpretation")
    for lead, follow, k, r, p, row in ll:
        if abs(r) < 0.02 or p > 0.05:
            interp = "no edge"
        elif k > 0:
            interp = f"{lead.replace('=X','').replace('=F','F')} LEADS {follow.replace('=X','').replace('=F','F')} by {k} bars"
        elif k < 0:
            interp = f"{follow.replace('=X','').replace('=F','F')} LEADS {lead.replace('=X','').replace('=F','F')} by {-k} bars"
        else:
            interp = "contemporaneous"
        mark = "**" if p < 0.01 else "*" if p < 0.05 else ""
        print(f"  {lead:<10}{follow:<12}{k:>8}{r:>+10.4f}{p:>10.4f}  {mark} {interp}")

    # ── 4. Volatility regime split ──
    print("\n" + "=" * 110)
    print("[4] VOLATILITY REGIME (high/low ATR tercile) — MR vs momentum by regime")
    print("-" * 110)
    print(f"{'Symbol':<12}{'hi_n':>6}{'hi_mean':>9}{'hi_std':>9}{'hi_AC[1]':>10}{'lo_n':>7}{'lo_mean':>9}{'lo_std':>9}{'lo_AC[1]':>10}")
    for sym, arr in data.items():
        v = vol_regime_split(arr, sym)
        if v is None:
            continue
        hi = v["high_vol"]
        lo = v["low_vol"]
        print(f"  {sym:<10}{hi[0]:>6}{hi[1]:>+9.3f}{hi[2]:>9.3f}{hi[4]:>+10.4f}{lo[0]:>7}{lo[1]:>+9.3f}{lo[2]:>9.3f}{lo[4]:>+10.4f}")

    # ── 5. Session transitions ──
    print("\n" + "=" * 110)
    print("[5] SESSION TRANSITIONS (first 30 min of session, pips/bar mean)")
    print("-" * 110)
    header = f"{'Symbol':<12}"
    for name, *_ in TRANSITIONS:
        header += f"{name:>15}"
    print(header)
    for sym, arr in data.items():
        st = session_transitions(arr, sym)
        row = f"  {sym:<10}"
        for name, *_ in TRANSITIONS:
            n, m, p = st.get(name, (0, 0, 1))
            mark = "**" if p < 0.01 else "*" if p < 0.05 else ""
            cell = f"{m:+.2f}{mark}"
            row += f"{cell:>15}"
        print(row)

    # ── 6. Volume-return ──
    print("\n" + "=" * 110)
    print("[6] VOLUME-RETURN (hi-vol vs lo-vol bucket)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'hi_|ret|':>10}{'lo_|ret|':>10}{'ratio':>8}{'hi_AC[1]':>10}{'lo_AC[1]':>10}  Interpretation")
    for sym, arr in data.items():
        vb = volume_bucket(arr, sym)
        if vb is None:
            continue
        ratio = vb["hi_abs_mean"] / vb["lo_abs_mean"] if vb["lo_abs_mean"] else 0
        # interpretation
        if vb["hi_ac"] > 0.02 and vb["lo_ac"] < -0.02:
            interp = "hi-vol=MOMENTUM, lo-vol=MR"
        elif vb["hi_ac"] < -0.02:
            interp = "hi-vol=MR (contrarian)"
        elif vb["hi_ac"] > 0.05:
            interp = "hi-vol=strong MOMENTUM"
        else:
            interp = "no clear split"
        print(f"  {sym:<10}{vb['hi_abs_mean']:>10.3f}{vb['lo_abs_mean']:>10.3f}{ratio:>8.2f}{vb['hi_ac']:>+10.4f}{vb['lo_ac']:>+10.4f}  {interp}")

    # ── 7. Monte Carlo p-correction ──
    print("\n" + "=" * 110)
    print(f"[7] MONTE CARLO bootstrap ({args.mc_boot} perms) — hourly biases surviving multiple-test correction")
    print("-" * 110)
    print(f"{'Symbol':<12}{'Hour':>6}{'real_t':>9}{'mc_p':>9}{'adj_p':>9}  Flag")
    mc_findings = []
    for sym, arr in data.items():
        real_ts, mc_p = mc_bootstrap_hourly(arr, sym, n_boot=args.mc_boot)
        # Bonferroni correction: умножаем на 24 часа × 14 симв = 336
        for h in range(24):
            adj_p = min(1.0, mc_p[h] * 336)
            if mc_p[h] < 0.01:
                mc_findings.append((sym, h, real_ts[h], mc_p[h], adj_p))
    mc_findings.sort(key=lambda x: x[3])
    for sym, h, t, p, adj in mc_findings[:30]:
        flag = "SURV-Bonf" if adj < 0.05 else "SURV-raw" if p < 0.01 else ""
        print(f"  {sym:<10}{h:>6}{t:>+9.2f}{p:>9.4f}{adj:>9.4f}  {flag}")

    # ── 8. Range expansion ──
    print("\n" + "=" * 110)
    print("[8] RANGE EXPANSION (after 6 bars of ATR-compression)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'n_events':>10}{'up_rate':>9}{'dn_rate':>9}{'avg_up':>10}{'avg_dn':>10}")
    for sym, arr in data.items():
        re_ = range_expansion(arr, sym, n_consol=6, n_after=4)
        if re_ is None:
            continue
        print(f"  {sym:<10}{re_['n_consol_events']:>10}{re_['breakout_up_rate']:>9.3f}{re_['breakout_dn_rate']:>9.3f}{re_['avg_up_move_pips']:>10.1f}{re_['avg_dn_move_pips']:>10.1f}")

    # ── 9. Gap-fill ──
    print("\n" + "=" * 110)
    print("[9] WEEKEND GAP-FILL (probability fill within 48 bars)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'n_gaps':>8}{'fill_rate':>11}{'avg_fill_bars':>15}{'avg_|gap|_pips':>16}")
    for sym, arr in data.items():
        gf = gap_fill(arr, sym, n_bars_fill=48)
        if gf is None:
            continue
        fb = f"{gf['avg_fill_bars']:.1f}" if gf['avg_fill_bars'] else "n/a"
        print(f"  {sym:<10}{gf['n_gaps']:>8}{gf['fill_rate']:>11.3f}{fb:>15}{gf['avg_abs_gap_pips']:>16.1f}")

    # ── 10. Runs test ──
    print("\n" + "=" * 110)
    print("[10] RUNS TEST (streaks of same-sign bars)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'runs':>7}{'expected':>11}{'z':>8}{'p':>9}  Interpretation")
    for sym, arr in data.items():
        rt = runs_test(arr)
        if rt is None:
            continue
        print(f"  {sym:<10}{rt['runs']:>7}{rt['expected']:>11.1f}{rt['z']:>+8.2f}{rt['p']:>9.4f}  {rt['interp']}")

    # ── 11. End-of-month ──
    print("\n" + "=" * 110)
    print("[11] END-OF-MONTH SEASONALITY")
    print("-" * 110)
    print(f"{'Symbol':<12}{'last_2d':>12}{'last_3-4d':>12}{'mid_month':>12}{'early_month':>13}")
    for sym, arr in data.items():
        em = end_of_month(arr, sym)
        def _fmt(b):
            n, m, p = b
            mark = "**" if p < 0.01 else "*" if p < 0.05 else ""
            return f"{m:+.2f}{mark}"
        print(f"  {sym:<10}"
              f"{_fmt(em['last_2d']):>12}"
              f"{_fmt(em['last_3-4d']):>12}"
              f"{_fmt(em['mid_month']):>12}"
              f"{_fmt(em['early_month']):>13}")

    # ── 12. Body/wick ──
    print("\n" + "=" * 110)
    print("[12] BODY/WICK RATIO (trending vs indecision)")
    print("-" * 110)
    print(f"{'Symbol':<12}{'body_mean':>12}{'upper_wick':>12}{'lower_wick':>12}{'trending%':>11}{'indecision%':>13}")
    for sym, arr in data.items():
        bw = body_wick(arr)
        print(f"  {sym:<10}{bw['body_mean']:>12.3f}{bw['upper_wick_mean']:>12.3f}{bw['lower_wick_mean']:>12.3f}{bw['trending_frac']*100:>10.1f}%{bw['indecision_frac']*100:>12.1f}%")

    # ── Summary ──
    print("\n" + "=" * 110)
    print("SUMMARY: паттерны, пережившие Bonferroni-коррекцию (adj_p < 0.05)")
    print("-" * 110)
    surv = [f for f in mc_findings if f[4] < 0.05]
    if not surv:
        print("  Ни один hourly bias не пережил multiple-comparison коррекцию.")
        print("  → почти все «значимые» паттерны из базового EDA — false positives.")
    else:
        for sym, h, t, p, adj in surv:
            print(f"  [SURVIVED] {sym:<12} h={h:02d} UTC | t={t:+.2f} | adj_p={adj:.4f}")


if __name__ == "__main__":
    main()
