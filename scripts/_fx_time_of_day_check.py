"""Проверка Breedon & Ranaldo (2013, JMCB 45(5)) на свежих данных (research-only).

Статья: EBS 1997–2007, валюта дешевеет в свои торговые часы. Для EUR/USD
стратегия после bid/ask-издержек: short EUR от открытия Европы до
открытия США (Sharpe 1.3), long EUR от открытия США до закрытия США
(Sharpe 0.9). Часы — по FX-фьючерсам (Table 1): Европа 07:00 CET,
США 08:00–16:00 New York (с учётом перехода на летнее время).

Параметры взяты из статьи без подбора. Окно yfinance 1h (730d) —
полностью вне выборки статьи (post-publication OOS). Сделки закрываются
до 17:00 NY (rollover) — своп не платится.
Издержки: спред 0.5 pip + комиссия FxPro $3.5/лот на сторону (0.35 pip
для USD-quote) → 1.2 pip round-trip, консервативно.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

PAIRS = {"EURUSD=X": 1e-4, "GBPUSD=X": 1e-4, "AUDUSD=X": 1e-4, "USDJPY=X": 1e-2}
COST_PIPS = 1.2


def load(sym: str) -> pd.DataFrame:
    df = yf.download(sym, period="730d", interval="1h", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["Open", "Close"]].dropna()


def price_at(df: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """Open часового бара, начинающегося в ts (UTC)."""
    return float(df["Open"].loc[ts]) if ts in df.index else None


def legs(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    days = pd.Index(df.index.tz_convert("America/New_York").normalize().unique())
    for d in days:
        if d.weekday() >= 5:
            continue
        date = d.date()
        eu_open = pd.Timestamp(f"{date} 07:00", tz="Europe/Berlin").tz_convert("UTC")
        us_open = pd.Timestamp(f"{date} 08:00", tz="America/New_York").tz_convert("UTC")
        us_close = pd.Timestamp(f"{date} 16:00", tz="America/New_York").tz_convert("UTC")
        p0, p1, p2 = price_at(df, eu_open), price_at(df, us_open), price_at(df, us_close)
        if None in (p0, p1, p2):
            continue
        rows.append({"date": pd.Timestamp(date), "eu": p1 - p0, "us": p2 - p1, "p": p1})
    return pd.DataFrame(rows).set_index("date")


def line(label: str, pips: pd.Series) -> str:
    n = len(pips)
    m, sd = pips.mean(), pips.std(ddof=1)
    t = m / (sd / np.sqrt(n)) if sd > 0 else 0.0
    sh = m / sd * np.sqrt(252) if sd > 0 else 0.0
    return (f"  {label:<28} n={n:>4} mean={m:+6.2f} pip  t={t:+5.2f}  "
            f"Sharpe={sh:+5.2f}  sum={pips.sum():+8.1f} pip  WR={(pips > 0).mean():.0%}")


def main() -> int:
    for sym, pip in PAIRS.items():
        lg = legs(load(sym))
        short_eu = -lg["eu"] / pip
        long_us = lg["us"] / pip
        both = short_eu + long_us
        print(f"=== {sym}  {lg.index[0].date()} → {lg.index[-1].date()}")
        print(line("EU short gross", short_eu))
        print(line("US long gross", long_us))
        print(line(f"EU short net −{COST_PIPS}", short_eu - COST_PIPS))
        print(line(f"US long net −{COST_PIPS}", long_us - COST_PIPS))
        print(line(f"обе ноги net −{2 * COST_PIPS}", both - 2 * COST_PIPS))
        half = lg.index[len(lg) // 2]
        print(line("обе net, 1-я половина", (both - 2 * COST_PIPS)[lg.index < half]))
        print(line("обе net, 2-я половина", (both - 2 * COST_PIPS)[lg.index >= half]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
