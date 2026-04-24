#!/usr/bin/env python3
"""EDA: data-driven поиск статистически значимых паттернов.

Задача: НЕ угадывать стратегию, а найти в данных сам edge.
Проверяем:
- Hourly mean returns по UTC-часу (bootstrap CI, t-stat)
- Autocorrelation lag 1..20 (trend vs mean-reversion характер)
- Hurst exponent (persistence)
- Day-of-week effects
- Gap-after-weekend returns
- Volatility clustering по часам
- Cross-asset lead-lag (Pearson corr между returns shifted)

Используем M15 как базу (меньше noise, хватает bars).

Запуск:
    PYTHONPATH=src python3 -m scripts.eda_fxpro
    PYTHONPATH=src python3 -m scripts.eda_fxpro --timeframe M5
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import DISPLAY_NAMES, pip_size

INSTRUMENTS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F", "NG=F", "ES=F",
]


def _filename_for(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F").replace("-", "_") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    """Возвращает structured array: ts, open, high, low, close, volume."""
    if not path.exists():
        return np.array([])
    data = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append((
                int(row["timestamp"]) // 1000,
                float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
                float(row["volume"]),
            ))
    dt = np.dtype([
        ("ts", "i8"), ("open", "f8"), ("high", "f8"),
        ("low", "f8"), ("close", "f8"), ("volume", "f8"),
    ])
    return np.array(data, dtype=dt)


def resample(arr: np.ndarray, minutes: int) -> np.ndarray:
    """Resample M5 → M15/H1 и т.д. (minutes должно быть кратно 5)."""
    if minutes == 5:
        return arr
    # группируем по block start (ts // minutes * minutes)
    sec = minutes * 60
    block = (arr["ts"] // sec) * sec
    # Найдём уникальные блоки
    unique_blocks, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique_blocks), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        group = arr[s:e]
        out[i] = (
            unique_blocks[i],
            group["open"][0],
            group["high"].max(),
            group["low"].min(),
            group["close"][-1],
            group["volume"].sum(),
        )
    return out


def log_returns(arr: np.ndarray) -> np.ndarray:
    closes = arr["close"]
    return np.log(closes[1:] / closes[:-1])


def to_pips(returns: np.ndarray, symbol: str, ref_price: float) -> np.ndarray:
    ps = pip_size(symbol)
    return returns * ref_price / ps


def bootstrap_mean_ci(data: np.ndarray, n_boot: int = 2000, alpha: float = 0.05) -> tuple[float, float, float]:
    """Bootstrap CI для среднего. Возвращает (mean, lower, upper)."""
    if len(data) < 10:
        return 0.0, 0.0, 0.0
    m = float(np.mean(data))
    idx = np.random.default_rng(42).integers(0, len(data), size=(n_boot, len(data)))
    boot_means = np.mean(data[idx], axis=1)
    lo = float(np.quantile(boot_means, alpha / 2))
    hi = float(np.quantile(boot_means, 1 - alpha / 2))
    return m, lo, hi


def t_stat(data: np.ndarray) -> tuple[float, float]:
    """One-sample t-test vs 0. Возвращает (t, p)."""
    if len(data) < 10:
        return 0.0, 1.0
    t, p = stats.ttest_1samp(data, 0.0)
    return float(t), float(p)


# ────────────────────── аналитические методы ──────────────────────

def hourly_profile(arr: np.ndarray, symbol: str) -> dict[int, tuple[int, float, float, float, float, float]]:
    """Per-hour (UTC) статистика returns.

    Возвращает dict[hour] → (n, mean_pips, lo_pips, hi_pips, t_stat, p_val).
    """
    ts = arr["ts"]
    rets = log_returns(arr)
    # час для каждого ret — час начала бара (closing hour-1)
    hours = np.array([datetime.fromtimestamp(t, UTC).hour for t in ts[1:]])
    ref_price = float(np.mean(arr["close"]))
    rets_pips = to_pips(rets, symbol, ref_price)

    out: dict[int, tuple[int, float, float, float, float, float]] = {}
    for h in range(24):
        mask = hours == h
        n = int(mask.sum())
        if n < 30:
            out[h] = (n, 0.0, 0.0, 0.0, 0.0, 1.0)
            continue
        data = rets_pips[mask]
        m, lo, hi = bootstrap_mean_ci(data)
        t, p = t_stat(data)
        out[h] = (n, m, lo, hi, t, p)
    return out


def autocorr(arr: np.ndarray, max_lag: int = 20) -> list[tuple[int, float, float]]:
    """Autocorrelation returns на лагах 1..max_lag. (lag, acf, p)."""
    rets = log_returns(arr)
    out: list[tuple[int, float, float]] = []
    for lag in range(1, max_lag + 1):
        if len(rets) <= lag + 10:
            break
        x = rets[:-lag]
        y = rets[lag:]
        if len(x) < 30:
            break
        r, p = stats.pearsonr(x, y)
        out.append((lag, float(r), float(p)))
    return out


def hurst_exponent(arr: np.ndarray, min_window: int = 20, max_window: int = 500) -> float:
    """Hurst через R/S. H>0.5 = persistence (тренд), <0.5 = anti-persistence (mean-rev)."""
    closes = arr["close"]
    if len(closes) < max_window * 2:
        max_window = len(closes) // 2
    log_closes = np.log(closes)
    rets = np.diff(log_closes)
    lags = np.unique(np.logspace(
        np.log10(min_window), np.log10(max_window), num=15
    ).astype(int))
    lags = [l for l in lags if l >= min_window]
    if len(lags) < 4:
        return 0.5
    rs_vals = []
    for lag in lags:
        # Разделим на блоки длины lag, посчитаем R/S для каждого, среднее
        n_blocks = len(rets) // lag
        if n_blocks < 2:
            continue
        rs_list = []
        for b in range(n_blocks):
            block = rets[b * lag: (b + 1) * lag]
            m = np.mean(block)
            dev = block - m
            Z = np.cumsum(dev)
            R = Z.max() - Z.min()
            S = np.std(block)
            if S > 0:
                rs_list.append(R / S)
        if rs_list:
            rs_vals.append((lag, np.mean(rs_list)))
    if len(rs_vals) < 4:
        return 0.5
    lag_arr = np.log([v[0] for v in rs_vals])
    rs_arr = np.log([v[1] for v in rs_vals])
    slope, _, _, _, _ = stats.linregress(lag_arr, rs_arr)
    return float(slope)


def dow_effect(arr: np.ndarray, symbol: str) -> dict[int, tuple[int, float, float]]:
    """Day-of-week: (n, mean_pips, p-value)."""
    ts = arr["ts"]
    rets = log_returns(arr)
    dow = np.array([datetime.fromtimestamp(t, UTC).weekday() for t in ts[1:]])
    ref_price = float(np.mean(arr["close"]))
    rets_pips = to_pips(rets, symbol, ref_price)
    out: dict[int, tuple[int, float, float]] = {}
    for d in range(7):
        mask = dow == d
        n = int(mask.sum())
        if n < 30:
            out[d] = (n, 0.0, 1.0)
            continue
        m = float(np.mean(rets_pips[mask]))
        _, p = t_stat(rets_pips[mask])
        out[d] = (n, m, p)
    return out


def gap_return(arr: np.ndarray, symbol: str, bars_after: int = 12) -> tuple[int, float, float]:
    """Return в первые N баров понедельника после weekend gap.

    Возвращает (n_mondays, mean_cumulative_pips_bars_after, p).
    """
    ts = arr["ts"]
    ref_price = float(np.mean(arr["close"]))
    ps = pip_size(symbol)
    rets_bars = []
    prev_dow = None
    for i, t in enumerate(ts):
        dt = datetime.fromtimestamp(t, UTC)
        if prev_dow is not None and prev_dow >= 4 and dt.weekday() == 0:
            # начало понедельника — возьмём следующие bars_after баров
            if i + bars_after < len(arr):
                r = math.log(arr["close"][i + bars_after] / arr["open"][i])
                rets_bars.append(r * ref_price / ps)
        prev_dow = dt.weekday()
    if len(rets_bars) < 5:
        return (len(rets_bars), 0.0, 1.0)
    m = float(np.mean(rets_bars))
    _, p = t_stat(np.array(rets_bars))
    return (len(rets_bars), m, p)


def volatility_by_hour(arr: np.ndarray) -> dict[int, float]:
    """Std returns по UTC-часу — оценка volatility pattern."""
    ts = arr["ts"]
    rets = log_returns(arr)
    hours = np.array([datetime.fromtimestamp(t, UTC).hour for t in ts[1:]])
    out: dict[int, float] = {}
    for h in range(24):
        mask = hours == h
        if int(mask.sum()) < 30:
            out[h] = 0.0
            continue
        out[h] = float(np.std(rets[mask]) * 1e4)  # bps
    return out


# ────────────────────── вывод ──────────────────────

def print_hourly_profile(symbol: str, prof: dict[int, tuple]) -> None:
    print(f"\n[{symbol}] Hourly return profile (UTC hour, pips per bar)")
    print(f"{'Hour':>5}{'n':>6}{'Mean':>8}{'Lo95':>8}{'Hi95':>8}{'t':>7}{'p':>8}{'Flag':>8}")
    for h in range(24):
        n, m, lo, hi, t, p = prof[h]
        flag = ""
        if p < 0.01 and abs(t) > 2.5:
            flag = "***" if p < 0.001 else "**"
        elif p < 0.05:
            flag = "*"
        print(f"{h:>5}{n:>6}{m:>8.3f}{lo:>8.3f}{hi:>8.3f}{t:>7.2f}{p:>8.4f}{flag:>8}")


def print_autocorr(symbol: str, acf: list) -> None:
    print(f"\n[{symbol}] Autocorrelation (lag 1..20)")
    sig_lags = [(lag, r, p) for lag, r, p in acf if p < 0.05]
    if not sig_lags:
        print("  Нет значимых лагов (p<0.05)")
    else:
        for lag, r, p in sig_lags:
            sign = "momentum" if r > 0 else "mean-rev"
            print(f"  lag={lag:>2}: ACF={r:+.4f} p={p:.4f} → {sign}")


def print_vol_hourly(symbol: str, vol: dict[int, float]) -> None:
    tops = sorted(vol.items(), key=lambda x: -x[1])[:5]
    lows = sorted(vol.items(), key=lambda x: x[1])[:3]
    print(f"\n[{symbol}] Volatility by hour (bps per bar). Top-5: ", end="")
    print(", ".join(f"{h}h={v:.1f}" for h, v in tops), end="  ")
    print(f"Low-3: ", ", ".join(f"{h}h={v:.1f}" for h, v in lows))


# ────────────────────── summary findings ──────────────────────

@dataclass
class Finding:
    symbol: str
    category: str
    description: str
    effect_pips: float
    p_value: float
    n: int


def collect_findings(symbol: str, arr: np.ndarray) -> list[Finding]:
    findings: list[Finding] = []

    # Hourly — значимые эффекты
    prof = hourly_profile(arr, symbol)
    for h, (n, m, lo, hi, t, p) in prof.items():
        if p < 0.01 and n >= 100 and abs(m) > 0.05:
            findings.append(Finding(
                symbol=symbol,
                category=f"hour_{h:02d}",
                description=f"h={h:02d} UTC: mean {m:+.3f} pips/bar (n={n}, t={t:+.2f})",
                effect_pips=m * n,  # накопленный pip-effect на N периодах
                p_value=p,
                n=n,
            ))

    # DOW
    dow = dow_effect(arr, symbol)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for d, (n, m, p) in dow.items():
        if p < 0.01 and abs(m) > 0.05 and n >= 100:
            findings.append(Finding(
                symbol=symbol,
                category=f"dow_{names[d]}",
                description=f"{names[d]}: {m:+.3f} pips/bar (n={n})",
                effect_pips=m * n,
                p_value=p,
                n=n,
            ))

    # Gap
    gn, gm, gp = gap_return(arr, symbol, bars_after=12)
    if gp < 0.05 and gn >= 5:
        findings.append(Finding(
            symbol=symbol,
            category="monday_gap_3h",
            description=f"Mon 09:00 UTC: +3h return {gm:+.2f} pips (n={gn}, p={gp:.3f})",
            effect_pips=gm * gn,
            p_value=gp,
            n=gn,
        ))

    # Autocorrelation lag-1 (ключевой)
    acf = autocorr(arr, max_lag=20)
    for lag, r, p in acf:
        if p < 0.01 and abs(r) > 0.02 and lag <= 5:
            kind = "MOMENTUM" if r > 0 else "MEAN-REV"
            findings.append(Finding(
                symbol=symbol,
                category=f"autocorr_lag_{lag}",
                description=f"{kind} lag={lag}: ACF={r:+.4f} (p={p:.4f})",
                effect_pips=abs(r) * 100,
                p_value=p,
                n=len(arr),
            ))

    return findings


# ────────────────────── main ──────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["M5", "M15", "H1"], default="M15")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    minutes = {"M5": 5, "M15": 15, "H1": 60}[args.timeframe]

    print("=" * 110)
    print(f"EDA: data-driven pattern discovery | timeframe={args.timeframe} | 14 instruments")
    print("=" * 110)

    all_findings: list[Finding] = []
    hurst_by_symbol: dict[str, float] = {}

    for sym in INSTRUMENTS:
        path = args.data_dir / _filename_for(sym)
        raw = load_csv(path)
        if len(raw) == 0:
            print(f"[WARN] no data: {sym}")
            continue
        arr = resample(raw, minutes)

        hurst = hurst_exponent(arr)
        hurst_by_symbol[sym] = hurst

        findings = collect_findings(sym, arr)
        all_findings.extend(findings)

        if args.verbose:
            print(f"\n{'=' * 90}\n{sym} — n_bars={len(arr)} | Hurst={hurst:.3f}")
            print_hourly_profile(sym, hourly_profile(arr, sym))
            print_autocorr(sym, autocorr(arr))
            print_vol_hourly(sym, volatility_by_hour(arr))

    # ── Summary ──
    print("\n" + "=" * 110)
    print("HURST EXPONENTS (>0.55 trending, <0.45 mean-reverting, 0.45-0.55 neutral)")
    print("-" * 60)
    for sym, h in sorted(hurst_by_symbol.items(), key=lambda x: -x[1]):
        regime = "TREND" if h > 0.55 else "MEAN-REV" if h < 0.45 else "random"
        print(f"  {sym:<12} H={h:.3f}   {regime}")

    print("\n" + "=" * 110)
    print(f"TOP SIGNIFICANT FINDINGS (p<0.01, sorted by |effect|)")
    print("-" * 110)
    all_findings.sort(key=lambda f: -abs(f.effect_pips))
    for f in all_findings[:40]:
        print(f"  [{f.symbol:<10}] {f.category:<20} {f.description}")

    # ── Hour heat-map агрегат (mean per hour across instruments) ──
    print("\n" + "=" * 110)
    print("HOUR × INSTRUMENT HEATMAP (pips/bar mean, * = p<0.05, ** = p<0.01)")
    print("-" * 110)
    header = "Hour " + "".join(f"{s.replace('=X','').replace('=F','F'):>9}" for s in INSTRUMENTS)
    print(header)
    for h in range(24):
        row = f"{h:>3} "
        for sym in INSTRUMENTS:
            path = args.data_dir / _filename_for(sym)
            raw = load_csv(path)
            if len(raw) == 0:
                row += f"{'---':>9}"
                continue
            arr = resample(raw, minutes)
            prof = hourly_profile(arr, sym)
            n, m, lo, hi, t, p = prof.get(h, (0, 0, 0, 0, 0, 1))
            mark = "**" if p < 0.01 else "*" if p < 0.05 else ""
            row += f"{m:+.2f}{mark:<3}"[:9].rjust(9)
        print(row)


if __name__ == "__main__":
    main()
