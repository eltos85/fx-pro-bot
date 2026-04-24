#!/usr/bin/env python3
"""Backtest: Late Session Mean-Reversion Scalper.

НАХОДКА ИЗ ДАННЫХ (bottom_up_analysis):
В UTC часах 21-23 (Sydney-Asian overlap, low liquidity) пары EURGBP, EURJPY,
GBPJPY показывают autocorrelation ≈ -0.25 на H1 returns. Это означает: если
бар закрылся с движением >|X|, следующий бар reverts с коэффициентом ≈0.25.

ГИПОТЕЗА:
1. В 21:00-23:00 UTC H1 барах если |return| > k·σ (σ rolling 30d)
2. Войти в обратную сторону
3. Hold 1-2 часа (exit H+1 или H+2)
4. SL = 2·entry_range (или time-stop)

АНТИ-OVERFIT:
- Параметры зафиксированы ДО: k=1.0, hold=1h, SL=3σ (tight)
- IS 70% / OOS 30%
- Тест на 3 инструментах (EURGBP, EURJPY, GBPJPY) — если работает на всех 3 = robust
- Стоимость: 2x pip commission + 0.3 pip slippage
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

# ─────────────────── Параметры (зафиксированы) ───────────────────

TARGET_HOURS_UTC = {21, 22, 23}  # Late Session UTC (21:00, 22:00, 23:00)
LOOKBACK_STD_BARS = 30 * 24      # rolling std — 30 days H1

ENTRY_K_SIGMA = 1.0              # enter если |last bar return| > 1σ (не слишком редко)
HOLD_BARS = 1                    # hold 1 H1 bar (ACF-based prediction)
SL_K_SIGMA = 3.0                 # SL 3σ (tight, для защиты от gap)
IS_FRACTION = 0.7

# Pairs с наибольшей Late-Session mean-reversion (из bottom_up_analysis H3)
TARGET_PAIRS = [
    ("EURGBP=X", -0.253),
    ("EURJPY=X", -0.240),
    ("GBPJPY=X", -0.253),
]

# Cost per trade (RT, pips) — комиссия FxPro $0.07/lot + spread + slippage
COST_RT = {
    "EURGBP=X": 1.3 + 1.0,  # spread 1-1.3 + slippage 0.5 + commission 0.7
    "EURJPY=X": 1.5 + 1.0,
    "GBPJPY=X": 2.0 + 1.0,
}


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append((int(r["timestamp"]) // 1000, float(r["open"]),
                         float(r["high"]), float(r["low"]), float(r["close"])))
    dt = np.dtype([("ts", "i8"), ("open", "f8"), ("high", "f8"),
                   ("low", "f8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample_h1(arr: np.ndarray) -> np.ndarray:
    if len(arr) == 0:
        return arr
    sec = 3600
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["open"][s], arr["high"][s:e].max(),
                  arr["low"][s:e].min(), arr["close"][e - 1])
    return out


@dataclass
class Trade:
    entry_ts: int
    exit_ts: int
    direction: int     # +1 long, -1 short
    entry_price: float
    exit_price: float
    prev_return_sigma: float
    exit_reason: str
    pnl_pips: float
    net: float


def backtest_pair(bars: np.ndarray, sym: str, cost: float) -> list[Trade]:
    """Для каждого H1 бара в Late Session:
    - Считаем return предыдущего бара в σ-нормализованных единицах
    - Если |σ_return| > ENTRY_K_SIGMA → open в обратную сторону на OPEN текущего бара
    - Exit через HOLD_BARS на CLOSE
    """
    ps = pip_size(sym)
    n = len(bars)
    close = bars["close"]
    open_ = bars["open"]
    high = bars["high"]
    low = bars["low"]
    ts = bars["ts"]

    # log-returns
    log_ret = np.zeros(n)
    log_ret[1:] = np.log(close[1:] / close[:-1])

    # Rolling std для sigma-normalization
    sigma = np.full(n, np.nan)
    for i in range(LOOKBACK_STD_BARS, n):
        w = log_ret[i - LOOKBACK_STD_BARS:i]
        s = np.std(w)
        if s > 0:
            sigma[i] = s

    # Hours UTC
    hours = np.array([datetime.fromtimestamp(int(t), UTC).hour for t in ts])

    trades: list[Trade] = []
    i = LOOKBACK_STD_BARS + 2

    while i < n - HOLD_BARS - 1:
        # Условие: текущий час в target_hours И сигма есть
        if hours[i] not in TARGET_HOURS_UTC:
            i += 1
            continue
        if np.isnan(sigma[i]) or sigma[i] == 0:
            i += 1
            continue

        # prev bar return in sigma
        prev_sigma_ret = log_ret[i - 1] / sigma[i]
        if abs(prev_sigma_ret) < ENTRY_K_SIGMA:
            i += 1
            continue

        # Enter opposite на OPEN текущего бара
        direction = -1 if prev_sigma_ret > 0 else 1
        entry_price = float(open_[i])
        entry_ts = int(ts[i])

        # SL = entry ± sl_sigma * sigma_price
        sigma_price = sigma[i] * entry_price
        sl_long = entry_price - SL_K_SIGMA * sigma_price
        sl_short = entry_price + SL_K_SIGMA * sigma_price

        # Идём вперёд HOLD_BARS. Если SL тронут — выход, иначе close на exit bar
        exit_reason = "TIME"
        exit_price = float(close[i + HOLD_BARS - 1])
        exit_ts = int(ts[i + HOLD_BARS - 1])

        for k in range(HOLD_BARS):
            bar_idx = i + k
            bar_high = float(high[bar_idx])
            bar_low = float(low[bar_idx])
            if direction == 1 and bar_low <= sl_long:
                exit_price = sl_long
                exit_reason = "SL"
                exit_ts = int(ts[bar_idx])
                break
            if direction == -1 and bar_high >= sl_short:
                exit_price = sl_short
                exit_reason = "SL"
                exit_ts = int(ts[bar_idx])
                break

        pnl_pips = (exit_price - entry_price) / ps * direction
        net = pnl_pips - cost
        trades.append(Trade(
            entry_ts=entry_ts, exit_ts=exit_ts, direction=direction,
            entry_price=entry_price, exit_price=exit_price,
            prev_return_sigma=float(prev_sigma_ret),
            exit_reason=exit_reason, pnl_pips=pnl_pips, net=net,
        ))

        i += HOLD_BARS  # non-overlapping

    return trades


def summarize(trades: list[Trade], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "total": 0.0, "mean": 0.0, "wr": 0.0,
                "p": 1.0, "t": 0.0, "pf": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "exits": {}}
    pnls = np.array([t.net for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    t_stat, p = stats.ttest_1samp(pnls, 0.0) if len(pnls) > 1 else (0, 1)
    exit_breakdown = {"TIME": 0, "SL": 0}
    for t in trades:
        exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1
    return {
        "label": label, "n": len(trades),
        "total": float(pnls.sum()), "mean": float(pnls.mean()),
        "wr": float(len(wins) / len(trades)),
        "p": float(p), "t": float(t_stat),
        "pf": float(pf) if pf != float("inf") else 999.0,
        "avg_win": float(wins.mean()) if len(wins) > 0 else 0,
        "avg_loss": float(losses.mean()) if len(losses) > 0 else 0,
        "exits": exit_breakdown,
    }


def print_stats(s: dict) -> None:
    if s["n"] == 0:
        print(f"    [{s['label']:<4}] no trades")
        return
    print(
        f"    [{s['label']:<4}] n={s['n']:>3}  total={s['total']:+8.1f}  "
        f"mean={s['mean']:+6.2f}  WR={s['wr']*100:5.1f}%  PF={s['pf']:5.2f}  "
        f"avgW={s['avg_win']:+5.1f}  avgL={s['avg_loss']:+5.1f}  "
        f"t={s['t']:+5.2f}  p={s['p']:.4f}  exits={s['exits']}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=" * 110)
    print(f"LATE SESSION MEAN-REVERSION SCALPER | H1 | UTC {sorted(TARGET_HOURS_UTC)}h")
    print(f"Entry: |prev H1 σ-return| > {ENTRY_K_SIGMA}. Fade. Hold {HOLD_BARS}h. SL {SL_K_SIGMA}σ")
    print("Based on H3 finding: ACF(1) in Late session ≈ -0.25 for EURGBP/EURJPY/GBPJPY")
    print("=" * 110)

    results = []

    for sym, acf in TARGET_PAIRS:
        path = args.data_dir / _fname(sym)
        raw = load_csv(path)
        if len(raw) == 0:
            continue
        bars = resample_h1(raw)
        cost = COST_RT[sym]

        print(f"\n{sym.replace('=X',''):<8}  bars={len(bars)}  cost_RT={cost:.1f}  "
              f"Late_ACF={acf:+.3f}")

        n_is = int(len(bars) * IS_FRACTION)
        is_trades = backtest_pair(bars[:n_is], sym, cost)
        oos_trades = backtest_pair(bars[n_is:], sym, cost)

        s_is = summarize(is_trades, "IS")
        s_oos = summarize(oos_trades, "OOS")
        print_stats(s_is)
        print_stats(s_oos)

        if args.verbose and oos_trades:
            print("    OOS trades:")
            for t in oos_trades[:15]:
                dt = datetime.fromtimestamp(t.entry_ts, UTC)
                print(f"      {dt:%Y-%m-%d %H:%M} dir={'L' if t.direction==1 else 'S'} "
                      f"prev_σ={t.prev_return_sigma:+.2f} → {t.exit_reason} "
                      f"pnl={t.pnl_pips:+6.1f}  net={t.net:+6.1f}")

        results.append({"sym": sym, "is": s_is, "oos": s_oos})

    # FINAL VERDICT
    print("\n" + "=" * 110)
    print("FINAL VERDICT")
    print("=" * 110)
    print(f"{'Pair':<10}{'n_IS':>6}{'IS_tot':>10}{'IS_WR':>7}{'IS_p':>8}"
          f"{'n_OOS':>7}{'OOS_tot':>10}{'OOS_WR':>7}{'OOS_p':>8}  Verdict")
    total_oos_n = 0
    total_oos_pips = 0
    passes = 0
    for r in results:
        is_ = r["is"]
        oos = r["oos"]
        if oos["n"] < 5:
            v = "~ too few"
        elif oos["total"] > 0 and oos["p"] < 0.15:
            v = "✓ PASS"
            passes += 1
        elif oos["total"] > 0:
            v = "~ +OOS low signif"
        else:
            v = "✗ FAIL"
        print(f"  {r['sym'].replace('=X',''):<8}{is_['n']:>6}{is_['total']:>+10.1f}"
              f"{is_['wr']*100:>6.1f}%{is_['p']:>8.4f}{oos['n']:>7}{oos['total']:>+10.1f}"
              f"{oos['wr']*100:>6.1f}%{oos['p']:>8.4f}  {v}")
        total_oos_n += oos["n"]
        total_oos_pips += oos["total"]

    print(f"\n  Total OOS: {total_oos_n} trades, {total_oos_pips:+.1f} pips")
    print(f"  Passes:    {passes}/{len(results)}")


if __name__ == "__main__":
    main()
