#!/usr/bin/env python3
"""Currency Strength Index (CSI) Backtest.

Мировая практика (Sharapov 2013, TradingView CSI):
Для каждой из 8 валют (USD, EUR, GBP, JPY, AUD, CAD, CHF) считаем силу как
взвешенная сумма returns против других валют. Long strongest, short weakest.

Concept:
- USD strength = mean(USDJPY ret, -EURUSD ret, -GBPUSD ret, ...)
- Обновляем каждые N часов, ранжируем 7 валют (NZD у нас нет)
- Long базу из топ-1 валюты, short котировку из bottom-1 валюты
- Идеальная пара: strongest/weakest

Anti-overfit:
- Фиксированные параметры: lookback 24h, hold 4h
- IS/OOS 70/30
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF"]

# mapping pair → (base_ccy, quote_ccy, direction_in_our_symbol)
# если у нас EURUSD=X, base=EUR quote=USD, buying EURUSD = long EUR short USD
PAIRS = {
    "EURUSD=X": ("EUR", "USD"),
    "GBPUSD=X": ("GBP", "USD"),
    "USDJPY=X": ("USD", "JPY"),
    "AUDUSD=X": ("AUD", "USD"),
    "USDCAD=X": ("USD", "CAD"),
    "USDCHF=X": ("USD", "CHF"),
    "EURGBP=X": ("EUR", "GBP"),
    "EURJPY=X": ("EUR", "JPY"),
    "GBPJPY=X": ("GBP", "JPY"),
}

# Fee / spread в пипах для каждой пары (RT, round-trip)
COST_RT = {
    "EURUSD=X": 1.8, "GBPUSD=X": 2.2, "USDJPY=X": 1.8, "AUDUSD=X": 2.2,
    "USDCAD=X": 2.0, "USDCHF=X": 2.2, "EURGBP=X": 2.3, "EURJPY=X": 2.5,
    "GBPJPY=X": 3.0,
}

LOOKBACK_BARS = 24  # H1 → 24 bars = 24h
HOLD_BARS = 4       # 4h hold
IS_FRACTION = 0.7


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    rows = []
    with path.open() as f:
        r = csv.DictReader(f)
        for rec in r:
            rows.append((int(rec["timestamp"]) // 1000, float(rec["close"])))
    dt = np.dtype([("ts", "i8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample_h1(arr: np.ndarray) -> np.ndarray:
    sec = 3600
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


def compute_currency_strength(
    closes: dict[str, np.ndarray], lookback: int,
) -> dict[str, np.ndarray]:
    """Для каждой валюты считаем rolling strength = mean log-return против других валют."""
    n = len(next(iter(closes.values())))

    # Log-returns по каждой паре за lookback
    returns = {}
    for sym, c in closes.items():
        r = np.zeros(n)
        r[lookback:] = np.log(c[lookback:] / c[:-lookback])
        returns[sym] = r

    strengths = {ccy: np.zeros(n) for ccy in CURRENCIES}
    counts = {ccy: 0 for ccy in CURRENCIES}

    for sym, (base, quote) in PAIRS.items():
        if sym not in returns:
            continue
        r = returns[sym]
        strengths[base] = strengths[base] + r
        strengths[quote] = strengths[quote] - r
        counts[base] += 1
        counts[quote] += 1

    for ccy in CURRENCIES:
        if counts[ccy] > 0:
            strengths[ccy] = strengths[ccy] / counts[ccy]
    return strengths


def backtest_csi(
    ts: np.ndarray, closes: dict[str, np.ndarray],
    lookback: int = LOOKBACK_BARS, hold: int = HOLD_BARS,
) -> list[dict]:
    """CSI: на каждом баре ранжируем 7 валют, находим лучшую пару strongest/weakest,
    открываем и держим hold_bars часов."""
    strengths = compute_currency_strength(closes, lookback)

    trades = []
    i = lookback + 10

    while i < len(ts) - hold:
        # Текущая сила всех валют
        curr = {ccy: strengths[ccy][i] for ccy in CURRENCIES}
        sorted_ccy = sorted(curr.items(), key=lambda x: x[1], reverse=True)
        strongest = sorted_ccy[0][0]
        weakest = sorted_ccy[-1][0]

        # Искать пару strongest/weakest (прямую или обратную)
        pair_sym = None
        direction = 0  # +1 = buy base (strongest=base), -1 = sell base (strongest=quote)
        for sym, (base, quote) in PAIRS.items():
            if sym not in closes:
                continue
            if base == strongest and quote == weakest:
                pair_sym = sym
                direction = 1
                break
            if base == weakest and quote == strongest:
                pair_sym = sym
                direction = -1
                break

        if pair_sym is None:
            i += 1
            continue

        # Enter and hold
        entry_price = float(closes[pair_sym][i])
        exit_price = float(closes[pair_sym][i + hold])
        ps = pip_size(pair_sym)
        pnl_pips = (exit_price - entry_price) / ps * direction
        cost = COST_RT[pair_sym]
        net = pnl_pips - cost

        trades.append({
            "entry_ts": int(ts[i]),
            "exit_ts": int(ts[i + hold]),
            "pair": pair_sym,
            "direction": direction,
            "strongest": strongest,
            "weakest": weakest,
            "strength_spread": curr[strongest] - curr[weakest],
            "pnl_pips": pnl_pips,
            "net": net,
        })

        i += hold  # Non-overlapping trades

    return trades


def summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    pnls = np.array([t["net"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    t_stat, p = stats.ttest_1samp(pnls, 0.0) if len(pnls) > 1 else (0, 1)
    return {
        "label": label, "n": len(trades),
        "total": float(pnls.sum()), "mean": float(pnls.mean()),
        "wr": float(len(wins) / len(trades)), "p": float(p),
        "t": float(t_stat), "pf": float(pf) if pf != float("inf") else 999.0,
        "avg_win": float(wins.mean()) if len(wins) > 0 else 0,
        "avg_loss": float(losses.mean()) if len(losses) > 0 else 0,
    }


def print_stats(s: dict) -> None:
    if s["n"] == 0:
        print(f"    [{s['label']:<4}] no trades")
        return
    print(
        f"    [{s['label']:<4}] n={s['n']:>4}  total={s['total']:+8.1f}  "
        f"mean={s['mean']:+6.2f}  WR={s['wr']*100:5.1f}%  PF={s['pf']:5.2f}  "
        f"avgW={s['avg_win']:+5.1f}  avgL={s['avg_loss']:+5.1f}  "
        f"t={s['t']:+5.2f}  p={s['p']:.4f}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--lookback", type=int, default=LOOKBACK_BARS,
                    help="Lookback H1 bars для strength (default 24)")
    ap.add_argument("--hold", type=int, default=HOLD_BARS,
                    help="Hold H1 bars (default 4)")
    args = ap.parse_args()

    print("=" * 110)
    print(f"CURRENCY STRENGTH INDEX (CSI) BACKTEST | H1 | "
          f"lookback={args.lookback}h, hold={args.hold}h")
    print("Long strongest / short weakest currency pair")
    print("=" * 110)

    # Load all pairs
    data = {}
    for sym in PAIRS.keys():
        path = args.data_dir / _fname(sym)
        if not path.exists():
            continue
        raw = load_csv(path)
        if len(raw) == 0:
            continue
        data[sym] = resample_h1(raw)
        print(f"  {sym:<12} bars={len(data[sym])}")

    ts, closes = align_all(data)
    print(f"\n  Aligned bars: {len(ts)}")

    # Split IS/OOS
    n_is = int(len(ts) * IS_FRACTION)

    is_closes = {sym: c[:n_is] for sym, c in closes.items()}
    oos_closes = {sym: c[n_is:] for sym, c in closes.items()}

    is_trades = backtest_csi(ts[:n_is], is_closes, args.lookback, args.hold)
    oos_trades = backtest_csi(ts[n_is:], oos_closes, args.lookback, args.hold)

    print("\n" + "=" * 110)
    print("RESULTS")
    print("=" * 110)
    s_is = summarize(is_trades, "IS")
    s_oos = summarize(oos_trades, "OOS")
    print_stats(s_is)
    print_stats(s_oos)

    # Breakdown по парам на OOS
    pair_stats = {}
    for t in oos_trades:
        pair_stats.setdefault(t["pair"], []).append(t["net"])

    print("\n  OOS breakdown by most-used pair:")
    for sym, pnls in sorted(pair_stats.items(), key=lambda x: -len(x[1]))[:10]:
        arr = np.array(pnls)
        wr = (arr > 0).mean() * 100
        print(f"    {sym:<12} n={len(arr):3}  total={arr.sum():+7.1f}  WR={wr:5.1f}%")

    # Verdict
    print("\n" + "=" * 110)
    if s_oos["n"] < 10:
        verdict = "~ недостаточно OOS trades"
    elif s_oos["total"] > 0 and s_oos["p"] < 0.15:
        verdict = "✓ PASS — CSI edge подтверждён"
    elif s_oos["total"] > 0:
        verdict = "~ +OOS но low significance"
    else:
        verdict = "✗ FAIL — CSI не работает"
    print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    main()
