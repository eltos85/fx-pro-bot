#!/usr/bin/env python3
"""Bottom-up Data Investigation (90 дней FxPro).

НЕ checklist методов — исследование 4 конкретных гипотез:

H1: КОРРЕЛЯЦИОННЫЕ РЕЖИМЫ
    Rolling 9×9 corr matrix по окнам 7d/30d/60d. Ищем structural breaks
    (корреляция переключается из high в low или наоборот). Гипотеза: можно
    ловить смены режимов и торговать pair divergence только в "correlated"
    режиме.

H2: CONDITIONAL RESPONSE
    Если инструмент A сделал экстремальное движение (>2σ за 4h), что делает
    инструмент B в следующие 1h / 4h / 24h? Гипотеза: лид-отстающая динамика
    даёт predictable reaction.

H3: SESSION-SPECIFIC BEHAVIOR
    Корреляции и волатильность pair spreads **по сессиям** (Asia/London/NY).
    Гипотеза: в Asian session JPY pairs mean-revert, в London-NY — trend.

H4: PCA FACTOR MODEL
    9 пар → 2-3 главных фактора (USD / EUR / Risk). Residuals после
    PCA-projection: stationary? Mean-reverting? Если да — торгуем отклонения
    от factor model как pairs trade (но с reduced dimensionality).

Все результаты в консоль — потом превращаем в hypotheses для backtest.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy import stats
from statsmodels.tsa.stattools import adfuller

FX_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "NZDUSD=X", "EURGBP=X",
    "EURJPY=X", "GBPJPY=X",
]


# ────────────────────── I/O ──────────────────────

def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    if not path.exists():
        return np.array([])
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append((int(r["timestamp"]) // 1000, float(r["close"])))
    dt = np.dtype([("ts", "i8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample(arr: np.ndarray, minutes: int) -> np.ndarray:
    if len(arr) == 0:
        return arr
    sec = minutes * 60
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["close"][e - 1])
    return out


def align_all(data: dict[str, np.ndarray]) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    common = None
    for sym, arr in data.items():
        if common is None:
            common = set(arr["ts"].tolist())
        else:
            common &= set(arr["ts"].tolist())
    ts = np.array(sorted(common))
    out = {}
    for sym, arr in data.items():
        sym_map = {t: i for i, t in enumerate(arr["ts"])}
        idx = np.array([sym_map[t] for t in ts])
        out[sym] = arr["close"][idx]
    return ts, out


# ────────────── H1: Rolling Correlation Regimes ──────────────

def rolling_corr(a: np.ndarray, b: np.ndarray, window: int) -> np.ndarray:
    """Rolling Pearson corr of log-returns."""
    ra = np.diff(np.log(a))
    rb = np.diff(np.log(b))
    n = len(ra)
    out = np.full(n, np.nan)
    for i in range(window, n):
        x = ra[i - window:i]
        y = rb[i - window:i]
        if np.std(x) > 0 and np.std(y) > 0:
            out[i] = np.corrcoef(x, y)[0, 1]
    return out


def h1_correlation_regimes(closes: dict[str, np.ndarray], ts: np.ndarray) -> None:
    """Анализ: какие пары имеют **стабильную** vs **нестабильную** корреляцию.

    Стабильная corr → может работать cointegration pair trading
    Нестабильная corr (switching regime) → торгуем переключения
    """
    print("\n" + "=" * 110)
    print("H1: CORRELATION REGIMES (H1 returns, rolling 30d window)")
    print("Cтабильные пары — кандидаты для cointegration. Нестабильные — regime switching.")
    print("=" * 110)

    h1_window = 30 * 24  # 30d H1 bars

    results = []
    for a, b in combinations(FX_PAIRS, 2):
        if a not in closes or b not in closes:
            continue
        corr = rolling_corr(closes[a], closes[b], h1_window)
        valid = corr[~np.isnan(corr)]
        if len(valid) < 100:
            continue
        mean_c = float(np.mean(valid))
        std_c = float(np.std(valid))
        min_c = float(np.min(valid))
        max_c = float(np.max(valid))
        range_c = max_c - min_c
        # % времени corr > 0.5
        pct_high = float(np.mean(np.abs(valid) > 0.5) * 100)
        results.append({
            "pair": f"{a.replace('=X','')}~{b.replace('=X','')}",
            "mean": mean_c, "std": std_c,
            "min": min_c, "max": max_c, "range": range_c,
            "pct_high": pct_high,
        })

    # Сортируем по abs(mean) убывая — самые коррелированные
    results.sort(key=lambda r: abs(r["mean"]), reverse=True)

    print(f"\n{'Pair':<18}{'mean_c':>9}{'std_c':>9}{'min':>8}{'max':>8}{'range':>8}"
          f"{'%|c|>0.5':>10}  Verdict")
    print("-" * 110)

    stable_high = []     # |corr| > 0.5 и std < 0.15 → candidates for pairs
    unstable = []        # range > 0.7 → switching regime
    stable_low = []      # |corr| < 0.3 и std < 0.15 → independent

    for r in results:
        verdict = ""
        if abs(r["mean"]) > 0.5 and r["std"] < 0.15:
            verdict = "✓ STABLE high (pairs candidate)"
            stable_high.append(r)
        elif r["range"] > 0.7:
            verdict = "⚠ SWITCHING regime"
            unstable.append(r)
        elif abs(r["mean"]) < 0.3 and r["std"] < 0.15:
            verdict = "· stable low (independent)"
            stable_low.append(r)
        else:
            verdict = "~ unstable/medium"

        print(f"  {r['pair']:<16}{r['mean']:>+9.3f}{r['std']:>9.3f}"
              f"{r['min']:>+8.3f}{r['max']:>+8.3f}{r['range']:>8.3f}"
              f"{r['pct_high']:>9.1f}%  {verdict}")

    print(f"\n  Summary: stable_high={len(stable_high)}  "
          f"switching={len(unstable)}  stable_low={len(stable_low)}")

    if stable_high:
        print("\n  → STABLE HIGH corr pairs (candidates for pairs trading):")
        for r in stable_high[:10]:
            print(f"      {r['pair']:<16} mean_c={r['mean']:+.3f}  std={r['std']:.3f}")

    if unstable:
        print("\n  → SWITCHING regime pairs (candidates для regime strategy):")
        for r in unstable[:10]:
            print(f"      {r['pair']:<16} range={r['range']:.3f}  "
                  f"min={r['min']:+.3f} max={r['max']:+.3f}")


# ────────────── H2: Conditional Response Analysis ──────────────

def h2_conditional_response(closes: dict[str, np.ndarray], ts: np.ndarray) -> None:
    """Если A сделал extreme move за 4h, что делает B следующие 1/4/24h?

    Метод:
    1. Для каждого A: std-нормализованное 4h log-return
    2. Отбираем бары где |4h_return_A| > 2σ
    3. Считаем mean и t-stat 1h/4h/24h-return B после
    4. Ищем где reaction статистически значима (p<0.05)
    """
    print("\n" + "=" * 110)
    print("H2: CONDITIONAL RESPONSE (extreme move in A → what does B do after?)")
    print("H1 timeframe. Trigger: |4h return A| > 2σ. Measure: 1h/4h/24h return B after.")
    print("Статистически значимые связи (p<0.05) с Bonferroni correction для 90 пар * 3 horizons = 270 тестов")
    print("=" * 110)

    horizons = [1, 4, 24]
    trigger_lookback = 4  # 4h return для триггера

    # Для каждой ПАРЫ (A trigger, B response) — не симметрично
    results = []
    for A in FX_PAIRS:
        if A not in closes:
            continue
        log_c_A = np.log(closes[A])
        n = len(log_c_A)
        # 4h returns A
        r4_A = np.full(n, np.nan)
        r4_A[trigger_lookback:] = log_c_A[trigger_lookback:] - log_c_A[:-trigger_lookback]
        # Std нормализация (rolling 30d-window среднеквадратичное)
        std_win = 30 * 24
        r4_A_norm = np.full(n, np.nan)
        for i in range(std_win, n):
            w = r4_A[i - std_win:i]
            w = w[~np.isnan(w)]
            if len(w) > 10:
                s = np.std(w)
                if s > 0:
                    r4_A_norm[i] = r4_A[i] / s
        # trigger points: |norm return| > 2
        trigger_idx = np.where(np.abs(r4_A_norm) > 2.0)[0]
        if len(trigger_idx) < 20:
            continue
        # Sign of A move
        sign_A = np.sign(r4_A[trigger_idx])

        for B in FX_PAIRS:
            if B == A or B not in closes:
                continue
            log_c_B = np.log(closes[B])
            for h in horizons:
                # response B at i+h
                valid = trigger_idx[trigger_idx + h < n]
                sign_valid = sign_A[trigger_idx + h < n]
                if len(valid) < 20:
                    continue
                resp_raw = log_c_B[valid + h] - log_c_B[valid]
                # Если B движется в том же направлении — sign_resp > 0
                resp_signed = resp_raw * sign_valid
                mean_resp = float(np.mean(resp_signed))
                t_stat, p = stats.ttest_1samp(resp_signed, 0)
                # Конвертируем в пипы (будем через pip_size потом, для отчёта — в bps)
                mean_bps = mean_resp * 10000
                results.append({
                    "A": A.replace("=X", ""),
                    "B": B.replace("=X", ""),
                    "h": h, "n": len(valid),
                    "mean_bps": mean_bps,
                    "t": float(t_stat), "p": float(p),
                })

    # Bonferroni correction: 90 pairs * 3 horizons = 270 tests
    n_tests = len(results)
    print(f"  Total tests: {n_tests}, Bonferroni α = 0.05/{n_tests} = {0.05/n_tests:.2e}")

    # Отфильтруем значимые
    results.sort(key=lambda r: r["p"])
    significant = [r for r in results if r["p"] * n_tests < 0.05]
    p05 = [r for r in results if r["p"] < 0.05 and r["p"] * n_tests >= 0.05]

    print(f"\n  Bonferroni-significant (p < {0.05/n_tests:.2e}): {len(significant)}")
    print(f"  Nominal significant (0.05 > p > Bonferroni):     {len(p05)}")
    print(f"  Total non-significant:                           {n_tests - len(significant) - len(p05)}")

    print("\n  TOP 20 most significant conditional responses:")
    print(f"  {'A trigger':<8}{'B resp':<8}{'h':>3}{'n':>5}{'mean_bps':>10}"
          f"{'t':>7}{'p':>10}  Verdict")
    print("  " + "-" * 100)
    for r in results[:20]:
        if r["p"] * n_tests < 0.05:
            v = "✓ BONFERRONI"
        elif r["p"] < 0.05:
            v = "~ nominal"
        else:
            v = ""
        print(f"  {r['A']:<8}{r['B']:<8}{r['h']:>3}{r['n']:>5}{r['mean_bps']:>+10.2f}"
              f"{r['t']:>+7.2f}{r['p']:>10.4f}  {v}")

    if significant:
        print(f"\n  ✓ SIGNIFICANT Bonferroni-corrected edges:")
        for r in significant:
            print(f"    {r['A']} → {r['B']} @ h={r['h']}h: "
                  f"mean={r['mean_bps']:+.2f} bps, t={r['t']:+.2f}, p={r['p']:.2e}, n={r['n']}")


# ────────────── H3: Session-Specific Behavior ──────────────

def h3_session_analysis(closes: dict[str, np.ndarray], ts: np.ndarray) -> None:
    """Session-specific: Asia (00-08 UTC), London (08-16), NY (13-21)."""
    print("\n" + "=" * 110)
    print("H3: SESSION-SPECIFIC CORRELATIONS & RETURNS")
    print("Hypothesis: в Asia JPY crosses mean-revert, в London-NY — trending")
    print("=" * 110)

    dts = [datetime.fromtimestamp(int(t), UTC) for t in ts]
    hours = np.array([d.hour for d in dts])

    # Define sessions
    asia = hours < 8
    london = (hours >= 8) & (hours < 13)
    london_ny = (hours >= 13) & (hours < 16)
    ny = (hours >= 16) & (hours < 21)
    late = hours >= 21

    session_masks = {
        "Asia (0-8)": asia,
        "London (8-13)": london,
        "LondonNY (13-16)": london_ny,
        "NY (16-21)": ny,
        "Late (21-0)": late,
    }

    # Для каждой пары: volatility по сессиям + autocorr 1-lag
    print("\n  VOLATILITY (std of H1 returns, bps) by session:")
    print(f"  {'Pair':<10}" + "".join(f"{s:>17}" for s in session_masks) + f"{'TOTAL':>10}")

    for sym in FX_PAIRS:
        if sym not in closes:
            continue
        log_ret = np.diff(np.log(closes[sym])) * 10000  # bps
        mask_ret = {k: m[1:] for k, m in session_masks.items()}
        row = f"  {sym.replace('=X',''):<10}"
        for s, m in session_masks.items():
            m_valid = m[1:]
            if np.sum(m_valid) > 10:
                row += f"{np.std(log_ret[m_valid]):>16.1f}"
            else:
                row += f"{'-':>16}"
        row += f"{np.std(log_ret):>10.1f}"
        print(row)

    print("\n  AUTOCORRELATION 1-lag (ρ) by session — отрицательная = mean-reversion:")
    print(f"  {'Pair':<10}" + "".join(f"{s:>17}" for s in session_masks) + f"{'TOTAL':>10}")

    for sym in FX_PAIRS:
        if sym not in closes:
            continue
        log_ret = np.diff(np.log(closes[sym]))
        row = f"  {sym.replace('=X',''):<10}"
        for s, m in session_masks.items():
            m_valid = m[1:]
            # Возьмём consecutive returns в сессии
            r = log_ret[m_valid]
            if len(r) > 100:
                acf = np.corrcoef(r[:-1], r[1:])[0, 1]
                row += f"{acf:>+16.3f}"
            else:
                row += f"{'-':>16}"
        # Total
        acf_total = np.corrcoef(log_ret[:-1], log_ret[1:])[0, 1] if len(log_ret) > 100 else 0
        row += f"{acf_total:>+10.3f}"
        print(row)


# ────────────── H4: PCA Factor Model ──────────────

def h4_pca_analysis(closes: dict[str, np.ndarray], ts: np.ndarray) -> None:
    """Принципиальный факторный анализ: 9-10 FX пар → как их разложить на factors?"""
    print("\n" + "=" * 110)
    print("H4: PCA FACTOR ANALYSIS")
    print("9-10 FX pairs → ищем главные факторы. Residuals от factor-model = mean-reverting?")
    print("=" * 110)

    # Матрица H1 returns
    log_rets = []
    pair_names = []
    for sym in FX_PAIRS:
        if sym not in closes:
            continue
        pair_names.append(sym.replace("=X", ""))
        log_rets.append(np.diff(np.log(closes[sym])))

    R = np.array(log_rets).T  # [n_bars, n_pairs]
    print(f"  Returns matrix: {R.shape} (bars × pairs)")

    # Centered + scaled
    R_c = (R - R.mean(axis=0)) / R.std(axis=0)

    # Covariance and eigendecomp
    cov = np.cov(R_c.T)
    eigvals, eigvecs = np.linalg.eigh(cov)
    # Сортируем по убыванию
    idx = np.argsort(-eigvals)
    eigvals = eigvals[idx]
    eigvecs = eigvecs[:, idx]

    total = eigvals.sum()
    print("\n  Principal Components (variance explained):")
    print(f"  {'PC':<5}{'eigval':>10}{'var %':>10}{'cumul %':>10}  Top loadings")
    cumul = 0
    for i in range(min(5, len(eigvals))):
        cumul += eigvals[i]
        loads = eigvecs[:, i]
        # TopN + bottomN
        order = np.argsort(-loads)
        top = [(pair_names[j], loads[j]) for j in order[:3]]
        bot = [(pair_names[j], loads[j]) for j in order[-3:]]
        load_str = " ".join(f"{n}={l:+.2f}" for n, l in top)
        load_str += " ... " + " ".join(f"{n}={l:+.2f}" for n, l in bot)
        print(f"  PC{i+1:<4}{eigvals[i]:>10.3f}{eigvals[i]/total*100:>9.1f}%"
              f"{cumul/total*100:>9.1f}%  {load_str}")

    # Residuals от первых 2 PC
    for n_factors in [1, 2, 3]:
        proj = eigvecs[:, :n_factors] @ eigvecs[:, :n_factors].T
        resid = R_c - R_c @ proj

        print(f"\n  Residuals после {n_factors} PC — stationarity test:")
        print(f"  {'Pair':<10}{'std':>8}{'ADF_p':>10}{'ACF(1)':>10}  Verdict")

        stat_pairs = []
        for i, name in enumerate(pair_names):
            s = resid[:, i]
            if len(s) < 100:
                continue
            # ADF test
            try:
                _, p_adf, *_ = adfuller(s, autolag="AIC")
            except Exception:
                p_adf = 1.0
            acf1 = np.corrcoef(s[:-1], s[1:])[0, 1]
            verdict = ""
            if p_adf < 0.05 and acf1 < -0.05:
                verdict = "✓ stationary & anti-correlated (mean-revert)"
                stat_pairs.append((name, p_adf, acf1))
            elif p_adf < 0.05:
                verdict = "✓ stationary (but no mean-rev)"
            elif acf1 < -0.05:
                verdict = "~ anti-corr but non-stationary"
            print(f"  {name:<10}{np.std(s):>8.3f}{p_adf:>10.4f}{acf1:>+10.3f}  {verdict}")

        if stat_pairs:
            print(f"\n  ⚡ Mean-reverting residuals после {n_factors}PC:")
            for n, p, a in stat_pairs:
                print(f"      {n}  p_adf={p:.4f}  ACF(1)={a:+.3f}")


# ────────────── Main ──────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["H1", "H4"], default="H1")
    args = ap.parse_args()

    print("=" * 110)
    print(f"BOTTOM-UP FX INVESTIGATION | timeframe={args.timeframe} | 10 FX pairs")
    print("Methodology: hypotheses-driven, not checklist. 4 blocks.")
    print("=" * 110)

    minutes = {"H1": 60, "H4": 240}[args.timeframe]

    # Load
    data = {}
    for sym in FX_PAIRS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"[WARN] no data: {sym}")
            continue
        data[sym] = resample(raw, minutes)

    ts, closes = align_all(data)
    print(f"\n  Aligned: {len(ts)} bars, {len([k for k in closes if k in data])} pairs")
    print(f"  From: {datetime.fromtimestamp(int(ts[0]), UTC)} "
          f"to: {datetime.fromtimestamp(int(ts[-1]), UTC)}")

    h1_correlation_regimes(closes, ts)
    h2_conditional_response(closes, ts)
    h3_session_analysis(closes, ts)
    h4_pca_analysis(closes, ts)

    print("\n" + "=" * 110)
    print("INVESTIGATION DONE. Next: отбираем гипотезы с edge → backtest.")
    print("=" * 110)


if __name__ == "__main__":
    main()
