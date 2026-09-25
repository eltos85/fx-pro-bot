"""Кандидат C (daily multi-asset TSMOM) с учётом CFD-свопа брокера (research-only).

Контекст: BUILDLOG 2026-07-10 — кандидат C (_pivot_candidates_research.py)
дал 12м Sharpe 0.88 при издержках 5bp/turnover, но без overnight-свопа.
Для CFD своп — основная статья издержек трендовых стратегий с удержанием
неделями (github.com/ilahuerta-IA/carver-systematic-trading: EWMAC 26 лет,
Sharpe +0.31 gross → −0.46 со свопом).

Свопы — снимок ProtoOASymbol с demo-счёта FxPro 2026-09-25
(swapCalculationType 0 = пипсы на единицу базового актива за ночь,
1 = % годовых). Годовая ставка = swap × 10^-pipPosition / price × 365.
Снимок применяется ко всей истории (ставки в прошлом были другими —
оговорка из mql5.com/en/articles/23469).
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from _pivot_candidates_research import (  # noqa: E402
    COST_BP, LOOKBACKS, TARGET_VOL, VOL_WIN, load,
)

# yf: (swapLong, swapShort, pipPosition, type)
SWAPS = {
    "EURUSD=X": (-0.89, 0.19, 4, 0),
    "GBPUSD=X": (-0.415, -0.325, 4, 0),
    "USDJPY=X": (0.69, -2.795, 2, 0),
    "AUDUSD=X": (-0.195, -0.29, 4, 0),
    "GC=F": (-69.25, 17.15, 2, 0),
    "BZ=F": (3.615, -16.565, 2, 0),
    "NG=F": (-12.125, 2.935, 3, 0),
    "ES=F": (0.0, 0.0, 2, 0),  # #US500_Z26 — датированный CFD, carry в цене
    "BTC-USD": (-20.0, -20.0, 2, 1),
    "ETH-USD": (-20.0, -20.0, 2, 1),
}
CRYPTO = {"BTC-USD", "ETH-USD"}


def annual_rates(last_px: dict[str, float]) -> dict[str, tuple[float, float]]:
    out = {}
    for s, (sl, ss, pp, typ) in SWAPS.items():
        if typ == 1:
            out[s] = (sl / 100, ss / 100)
        else:
            k = 10 ** -pp / last_px[s] * 365
            out[s] = (sl * k, ss * k)
    return out


def run(closes: dict[str, pd.Series], rates, lb: int, with_swap: bool) -> pd.Series:
    syms = list(closes)
    rets = pd.DataFrame({s: closes[s].pct_change() for s in syms})
    vol = rets.ewm(span=VOL_WIN, min_periods=VOL_WIN).std() * np.sqrt(252)
    mom = pd.DataFrame({s: closes[s] / closes[s].shift(lb) - 1.0 for s in syms}).reindex(rets.index)
    w = (np.sign(mom) * (TARGET_VOL / vol)).clip(-10, 10)
    month = rets.index.to_period("M")
    is_me = pd.Series(month != np.roll(month, -1), index=w.index)
    w_held = w.where(is_me, np.nan).shift(1).ffill()
    n = w_held.notna().sum(axis=1).replace(0, np.nan)
    gross = (w_held * rets).sum(axis=1) / n
    turn = w_held.diff().abs().sum(axis=1) / n
    net = gross - turn * COST_BP / 1e4
    if with_swap:
        per_day = {s: 365 if s in CRYPTO else 252 for s in syms}
        sw = pd.DataFrame(index=w_held.index)
        for s in syms:
            rl, rs = rates[s]
            ws = w_held[s]
            has_bar = rets[s].notna()
            sw[s] = np.where(ws > 0, ws * rl, -ws * rs) / per_day[s] * has_bar
        net = net + sw.sum(axis=1) / n
    return net.dropna()


def report(name: str, x: pd.Series) -> str:
    ann = x.mean() * 252
    sh = x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else 0.0
    eq = (1 + x).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    half = x.index[len(x) // 2]
    sh_is = x[x.index < half].mean() / x[x.index < half].std() * np.sqrt(252)
    sh_oos = x[x.index >= half].mean() / x[x.index >= half].std() * np.sqrt(252)
    return (f"  {name:<34} annRet={ann * 100:+6.2f}% Sharpe={sh:+.2f} "
            f"(IS {sh_is:+.2f} / OOS {sh_oos:+.2f}) maxDD={dd * 100:.1f}%")


def main() -> int:
    closes = {}
    for s in SWAPS:
        df = load(s, "10y", "1d")
        if df is not None and len(df) > 1000:
            closes[s] = df["Close"]
    last = {s: float(c.iloc[-1]) for s, c in closes.items()}
    rates = annual_rates(last)
    print("Своп, % годовых от номинала (long / short):")
    for s in closes:
        print(f"  {s:<9} px={last[s]:>10.3f}  {rates[s][0] * 100:+7.2f}% / {rates[s][1] * 100:+7.2f}%")
    print()

    universes = {
        "все 10": list(closes),
        "без крипты": [s for s in closes if s not in CRYPTO],
        "без крипты, Brent, газа": [s for s in closes if s not in CRYPTO | {"BZ=F", "NG=F"}],
    }
    for uname, syms in universes.items():
        sub = {s: closes[s] for s in syms}
        print(f"=== {uname}: {syms}")
        for lb_name, lb in LOOKBACKS.items():
            print(report(f"{lb_name} 5bp, без свопа", run(sub, rates, lb, False)))
            print(report(f"{lb_name} 5bp + своп", run(sub, rates, lb, True)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
