"""Собственное исследование: 3 гипотезы, pre-registered (BUILDLOG 2026-09-25).

Правила зафиксированы в BUILDLOG ДО первого прогона. Не менять пороги,
окна и время — иначе результат становится подгонкой.

Запуск: .venv/bin/python scripts/_own_research_3h.py [--validate]
Без --validate печатается только период поиска (2023-09-21→2025-04-30).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")
SPLIT = pd.Timestamp("2025-05-01", tz="UTC")

PAIRS = {  # файл, pip, cost_pips, usd_sign (+1: рост пары = сила USD)
    "EURUSD": ("EURUSD", 1e-4, 1.4, -1),
    "GBPUSD": ("GBPUSD", 1e-4, 1.6, -1),
    "AUDUSD": ("AUDUSD", 1e-4, 1.6, -1),
    "USDJPY": ("USDJPY", 1e-2, 1.8, +1),
}
GOLD = ("GC_F", 0.1, 7.0)
# своп: пипсы цены за ночь (снимок ProtoOASymbol 2026-09-25), long / short
SWAP = {"EURUSD": (-0.89, 0.19), "GBPUSD": (-0.415, -0.325), "AUDUSD": (-0.195, -0.29),
        "USDJPY": (0.69, -2.795), "GC_F": (-6.925, 1.715)}  # золото в пипсах 0.1


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(f"data/fxpro_klines/{name}_M5_3y.csv")
    df.index = pd.to_datetime(df.timestamp, unit="ms", utc=True)
    return df[["open", "high", "low", "close"]]


def daily_atr(m5: pd.DataFrame) -> pd.Series:
    d = m5.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().shift(1)  # ATR на начало дня, без сегодняшнего


def rollovers(t0: pd.Timestamp, t1: pd.Timestamp) -> int:
    n, d = 0, t0.tz_convert(NY).date()
    while True:
        roll = pd.Timestamp(datetime.combine(d, time(17, 0)), tz=NY).tz_convert(UTC)
        if roll > t1:
            return n
        if roll > t0 and d.weekday() < 5:
            n += 3 if d.weekday() == 2 else 1
        d += timedelta(days=1)


def trade(m5, name, pip, cost, side, t_entry, t_exit_max, tp=None, sl=None, exit_fn=None):
    """Вход open бара ≥ t_entry; выход по tp/sl (intrabar, sl первым) / exit_fn / t_exit_max."""
    seg = m5.loc[t_entry:t_exit_max]
    if len(seg) < 2:
        return None
    entry, t0 = seg.open.iloc[0], seg.index[0]
    lg = side > 0
    exit_px, t1 = seg.close.iloc[-1], seg.index[-1]
    for ts, row in seg.iloc[1:].iterrows():
        if sl is not None and ((lg and row.low <= sl) or (not lg and row.high >= sl)):
            exit_px, t1 = sl, ts
            break
        if tp is not None and ((lg and row.high >= tp) or (not lg and row.low <= tp)):
            exit_px, t1 = tp, ts
            break
        if exit_fn is not None and exit_fn(ts):
            exit_px, t1 = row.close, ts
            break
    gross = side * (exit_px - entry) / pip
    sw = SWAP[name][0 if lg else 1] * rollovers(t0, t1)
    return {"t": t0, "sym": name, "side": side, "gross": gross, "net": gross - cost + sw,
            "hold_h": (t1 - t0).total_seconds() / 3600}


# ── H1 USD-bloc dispersion ───────────────────────────────────────────────
def h1(data) -> list[dict]:
    h = {k: v.close.resample("1h").last().dropna() for k, v in data.items()}
    px = pd.DataFrame(h).dropna()
    r24 = np.log(px / px.shift(24))
    usd = pd.DataFrame({k: r24[k] * PAIRS[k][3] for k in PAIRS})
    factor = usd.mean(axis=1)
    resid = usd.sub(factor, axis=0)
    z = resid / resid.rolling(24 * 30, min_periods=24 * 20).std()
    out = []
    for k, (fname, pip, cost, sgn) in PAIRS.items():
        m5 = data[k]
        atr = daily_atr(m5)
        busy = pd.Timestamp("1970-01-01", tz="UTC")
        zk = z[k]
        for ts, zv in zk.items():
            if np.isnan(zv) or abs(zv) < 2 or ts < busy or not (7 <= ts.hour < 20) or ts.weekday() >= 5:
                continue
            # остаток > 0 → пара «слишком сильна в сторону USD» → сделка против:
            side = -int(np.sign(zv)) * sgn  # в знаке цены пары
            entry_t = ts + pd.Timedelta(hours=1)  # z по закрытию часового бара ts
            sign0 = np.sign(zv)

            def flip(t, zk=zk, sign0=sign0):
                hr = t.floor("1h") - pd.Timedelta(hours=1)
                return hr in zk.index and np.sign(zk.loc[hr]) != sign0 and t.minute == 0

            tr = trade(m5, k, pip, cost, side, entry_t, entry_t + pd.Timedelta(hours=24), exit_fn=flip)
            if tr:
                a = atr.asof(tr["t"].floor("1D"))
                tr["atr_units"] = tr["net"] * pip / a if a and a > 0 else np.nan
                tr["gross_atr"] = tr["gross"] * pip / a if a and a > 0 else np.nan
                out.append(tr)
                busy = tr["t"] + pd.Timedelta(hours=tr["hold_h"])
    return out


# ── H2 Month-end hedge rebalancing ───────────────────────────────────────
def h2(data) -> list[dict]:
    import yfinance as yf
    idx = {"EURUSD": "^STOXX50E", "GBPUSD": "^FTSE", "USDJPY": "^N225", "AUDUSD": "^AXJO"}
    tick = ["^GSPC"] + list(idx.values())
    raw = yf.download(tick, start="2023-08-01", progress=False, auto_adjust=True)["Close"]
    raw.index = pd.to_datetime(raw.index).date
    out = []
    days = pd.bdate_range("2023-10-01", "2026-09-25")
    month_last = pd.Series(days).groupby(pd.Series(days).dt.to_period("M")).max()
    for d in month_last:
        d = d.date()
        for k, ix in idx.items():
            m0 = date(d.year, d.month, 1)
            hist = raw[[ "^GSPC", ix]].dropna()
            prev = hist[hist.index < m0]
            cur = hist[(hist.index >= m0) & (hist.index < d)]
            if prev.empty or cur.empty:
                continue
            base = prev.iloc[-1]
            last = cur.iloc[-1]
            us, fo = last["^GSPC"] / base["^GSPC"] - 1, last[ix] / base[ix] - 1
            # США сильнее → иностр. фонды продают USD → иностранная валюта растёт
            foreign_up = 1 if us > fo else -1
            side = foreign_up * (-PAIRS[k][3])  # в знаке цены пары
            t_in = pd.Timestamp(datetime.combine(d, time(14, 0)), tz=LDN).tz_convert(UTC)
            t_out = pd.Timestamp(datetime.combine(d, time(16, 5)), tz=LDN).tz_convert(UTC)
            _, pip, cost, _ = PAIRS[k]
            tr = trade(data[k], k, pip, cost, side, t_in, t_out)
            if tr:
                a = daily_atr(data[k]).asof(tr["t"].floor("1D"))
                tr["atr_units"] = tr["net"] * pip / a
                tr["gross_atr"] = tr["gross"] * pip / a
                out.append(tr)
    return out


# ── H3 Weekend gap fade ──────────────────────────────────────────────────
def h3(data) -> list[dict]:
    out = []
    specs = {k: (v[1], v[2]) for k, v in PAIRS.items()} | {"GC_F": (GOLD[1], GOLD[2])}
    for k, (pip, cost) in specs.items():
        m5 = data[k]
        atr = daily_atr(m5)
        gaps = m5.index.to_series().diff() > pd.Timedelta(hours=24)
        for t_first in m5.index[gaps]:
            fri = m5.loc[:t_first - pd.Timedelta(minutes=1)]
            if fri.empty:
                continue
            fri_close = fri.close.iloc[-1]
            gap = m5.open.loc[t_first] - fri_close
            a = atr.asof(t_first.floor("1D"))
            if not a or np.isnan(a) or abs(gap) < 0.25 * a:
                continue
            mon = (t_first + pd.Timedelta(hours=6)).normalize()  # понедельник 00:00 UTC
            t_in, t_out = mon, mon + pd.Timedelta(hours=12)
            pre = m5.loc[t_first:t_in - pd.Timedelta(minutes=1)]
            filled = (pre.low.min() <= fri_close) if gap > 0 else (pre.high.max() >= fri_close)
            if filled:
                continue
            side = -1 if gap > 0 else 1
            sl = fri_close + 2 * gap
            tr = trade(m5, k, pip, cost, side, t_in, t_out, tp=fri_close, sl=sl)
            if tr:
                tr["atr_units"] = tr["net"] * pip / a
                tr["gross_atr"] = tr["gross"] * pip / a
                out.append(tr)
    return out


def report(name: str, rows: list[dict], validate: bool) -> None:
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"=== {name}: 0 сделок\n")
        return
    parts = [("поиск", df[df.t < SPLIT])]
    if validate:
        parts.append(("ПРОВЕРКА", df[df.t >= SPLIT]))
    print(f"=== {name}")
    for label, d in parts:
        x = d.atr_units.dropna()
        p = ttest_1samp(x, 0).pvalue if len(x) > 2 else float("nan")
        print(f"  {label:<9} n={len(x):>4}  gross {d.gross_atr.mean():+.4f} ATR  net {x.mean():+.4f} ATR  "
              f"p={p:.4f}  WR={(x > 0).mean():.0%}  net_pips={d.net.sum():+.0f}  "
              f"hold {d.hold_h.median():.1f}ч")
        for s, g in d.groupby("sym"):
            print(f"      {s:<7} n={len(g):>4} net {g.atr_units.mean():+.4f} ATR")
    print()


def main() -> int:
    validate = "--validate" in sys.argv
    data = {k: load(v[0]) for k, v in PAIRS.items()}
    data["GC_F"] = load(GOLD[0])
    print(f"Режим: {'поиск + ПРОВЕРКА' if validate else 'только поиск'}; порог поиска p<0.0167\n")
    report("H1 USD-bloc dispersion", h1(data), validate)
    report("H2 Month-end hedge rebalancing", h2(data), validate)
    report("H3 Weekend gap fade", h3(data), validate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
