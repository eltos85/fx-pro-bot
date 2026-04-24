#!/usr/bin/env python3
"""M5 scalp setups backtest — 7 классических интрадей-сетапов.

Цель: найти 1-3 сетапа, которые дают суммарно 20-40 сигналов/день на
портфеле (10 FX + commodities) после учёта реальных costs FxPro.

Подход (anti-overfit):
1. Параметры setup'ов взяты из литературы / дефолтов (не оптимизированы).
2. IS = первые 60% / OOS = последние 40%. Отбираем только те, где OOS P&L > 0.
3. Единые стандарты: SL = 1.5·ATR(14 M5), TP = 2.0·ATR, time-stop = 12 M5 bars (60 мин).
4. Costs per trade = spread + commission (0.07·2 = 0.14 USD для 0.01) + slippage.
   Переведены в пипсы через PIP_VALUES_USD.
5. Permutation test (1000 shuffles) для каждого выжившего сетапа.

Setups:
  1. London ORB 15min (06:00-06:15 UTC breakout → trade in break direction)
  2. NY ORB 15min (12:30-12:45 UTC breakout)
  3. Asian Range Fade (пробой азиатского range fade-back к середине)
  4. VWAP Reversion 2σ (deviation 2σ от session VWAP → возврат к VWAP)
  5. Volatility Contraction Breakout (Bollinger bandwidth shrinks → breakout)
  6. EMA20 Pullback in trend (M15 trend + M5 pullback к EMA20)
  7. Session High/Low Retest (retest Asian/London high/low)

NB: цены — bid; spread/commission добавлены к P&L. Тест без комбинирования сетапов.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import numpy as np

# ─────────────────── Конфиг ───────────────────

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "fxpro_klines"

# Портфель — все FX + золото + нефть
PORTFOLIO = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F",
]

# Costs (pips round-trip) — spread + commission + slippage
# Commission FxPro cTrader Raw+: $3.50/lot/side → для 0.01 = $0.07/side = $0.14 RT
# В пипсах: commission_USD / pip_value_USD
PIP_VAL = {
    "EURUSD=X": 0.10, "GBPUSD=X": 0.10, "USDJPY=X": 0.07,
    "AUDUSD=X": 0.10, "USDCAD=X": 0.07, "USDCHF=X": 0.10,
    "EURGBP=X": 0.13, "EURJPY=X": 0.07, "GBPJPY=X": 0.07,
    "GC=F": 0.10, "CL=F": 0.10, "BZ=F": 0.10,
}
SPREAD_PIPS = {
    "EURUSD=X": 1.5, "GBPUSD=X": 1.8, "USDJPY=X": 1.5,
    "AUDUSD=X": 1.8, "USDCAD=X": 2.2, "USDCHF=X": 1.8,
    "EURGBP=X": 1.8, "EURJPY=X": 2.0, "GBPJPY=X": 2.5,
    "GC=F": 3.5, "CL=F": 4.0, "BZ=F": 4.0,
}
SLIPPAGE_PIPS = 0.5
PIP_SIZE = {
    "EURUSD=X": 0.0001, "GBPUSD=X": 0.0001, "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001, "USDCAD=X": 0.0001, "USDCHF=X": 0.0001,
    "EURGBP=X": 0.0001, "EURJPY=X": 0.01, "GBPJPY=X": 0.01,
    "GC=F": 0.10, "CL=F": 0.01, "BZ=F": 0.01,
}


def cost_pips(sym: str) -> float:
    """Round-trip costs в пипсах для 0.01 лота."""
    commission_usd = 3.50 * 0.01 * 2  # $0.14 RT
    commission_pips = commission_usd / PIP_VAL[sym]
    return SPREAD_PIPS[sym] + commission_pips + SLIPPAGE_PIPS


# ─────────────────── Loader ───────────────────

@dataclass
class Bars:
    sym: str
    ts: np.ndarray          # ms
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    hour: np.ndarray        # UTC hour 0-23
    minute: np.ndarray      # UTC min 0-55
    dow: np.ndarray         # 0=mon … 6=sun
    date: np.ndarray        # YYYYMMDD int


def _sym_to_fname(sym: str) -> str:
    # "EURUSD=X" → "EURUSD", "GC=F" → "GC_F"
    return sym.replace("=X", "").replace("=F", "_F")


def load(sym: str) -> Bars:
    fp = DATA_DIR / f"{_sym_to_fname(sym)}_M5.csv"
    ts: list[int] = []
    o: list[float] = []
    h: list[float] = []
    low: list[float] = []
    c: list[float] = []
    v: list[float] = []
    with fp.open() as f:
        next(f)  # header
        for line in f:
            parts = line.strip().split(",")
            ts.append(int(parts[0]))
            o.append(float(parts[1]))
            h.append(float(parts[2]))
            low.append(float(parts[3]))
            c.append(float(parts[4]))
            v.append(float(parts[5]))
    ts_arr = np.asarray(ts, dtype=np.int64)
    dts = [datetime.fromtimestamp(t / 1000, tz=UTC) for t in ts_arr]
    hour = np.asarray([d.hour for d in dts], dtype=np.int16)
    minute = np.asarray([d.minute for d in dts], dtype=np.int16)
    dow = np.asarray([d.weekday() for d in dts], dtype=np.int16)
    date = np.asarray([d.year * 10000 + d.month * 100 + d.day for d in dts], dtype=np.int32)
    return Bars(
        sym=sym, ts=ts_arr,
        o=np.asarray(o), h=np.asarray(h), l=np.asarray(low), c=np.asarray(c), v=np.asarray(v),
        hour=hour, minute=minute, dow=dow, date=date,
    )


# ─────────────────── Indicators ───────────────────

def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    a = np.full_like(tr, np.nan, dtype=np.float64)
    if len(tr) < n:
        return a
    a[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        a[i] = (a[i - 1] * (n - 1) + tr[i]) / n
    return a


def ema(x: np.ndarray, n: int) -> np.ndarray:
    k = 2.0 / (n + 1)
    e = np.full_like(x, np.nan, dtype=np.float64)
    if len(x) < n:
        return e
    e[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        e[i] = e[i - 1] + k * (x[i] - e[i - 1])
    return e


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    s = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        s[i] = x[i - n + 1:i + 1].std(ddof=0)
    return s


# ─────────────────── Trade simulator ───────────────────

@dataclass
class Trade:
    sym: str
    entry_idx: int
    entry_ts: int
    direction: int          # +1 long / -1 short
    entry_price: float
    sl: float
    tp: float
    exit_idx: int = 0
    exit_ts: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pips_gross: float = 0.0
    pnl_pips_net: float = 0.0
    cost_pips: float = 0.0
    setup: str = ""


def simulate_trade(
    bars: Bars,
    entry_idx: int,
    direction: int,
    sl: float,
    tp: float,
    max_bars: int,
    setup: str,
) -> Trade | None:
    """Симулировать сделку начиная со СЛЕДУЮЩЕГО бара. Entry по open[entry_idx+1]."""
    if entry_idx + 1 >= len(bars.c):
        return None
    entry_i = entry_idx + 1
    entry_price = bars.o[entry_i]
    trade = Trade(
        sym=bars.sym, entry_idx=entry_i, entry_ts=int(bars.ts[entry_i]),
        direction=direction, entry_price=entry_price, sl=sl, tp=tp, setup=setup,
    )
    end = min(entry_i + max_bars, len(bars.c) - 1)
    for i in range(entry_i, end + 1):
        hi = bars.h[i]
        lo = bars.l[i]
        if direction == 1:
            if lo <= sl:
                trade.exit_idx = i
                trade.exit_ts = int(bars.ts[i])
                trade.exit_price = sl
                trade.exit_reason = "SL"
                break
            if hi >= tp:
                trade.exit_idx = i
                trade.exit_ts = int(bars.ts[i])
                trade.exit_price = tp
                trade.exit_reason = "TP"
                break
        else:
            if hi >= sl:
                trade.exit_idx = i
                trade.exit_ts = int(bars.ts[i])
                trade.exit_price = sl
                trade.exit_reason = "SL"
                break
            if lo <= tp:
                trade.exit_idx = i
                trade.exit_ts = int(bars.ts[i])
                trade.exit_price = tp
                trade.exit_reason = "TP"
                break
    else:
        trade.exit_idx = end
        trade.exit_ts = int(bars.ts[end])
        trade.exit_price = bars.c[end]
        trade.exit_reason = "TIME"
    pip = PIP_SIZE[bars.sym]
    trade.pnl_pips_gross = direction * (trade.exit_price - entry_price) / pip
    trade.cost_pips = cost_pips(bars.sym)
    trade.pnl_pips_net = trade.pnl_pips_gross - trade.cost_pips
    return trade


# ─────────────────── Setups ───────────────────

def setup_london_orb(bars: Bars, orb_minutes: int = 15) -> list[Trade]:
    """London ORB: breakout OR_High/OR_Low формированного в 06:00-06:15 UTC."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    unique_dates = np.unique(bars.date)
    for d in unique_dates:
        mask = bars.date == d
        idxs = np.flatnonzero(mask)
        if len(idxs) < 50:
            continue
        or_start_hour, or_start_min = 6, 0
        or_end_min = or_start_min + orb_minutes
        or_mask = (bars.hour[idxs] == or_start_hour) & (bars.minute[idxs] < or_end_min)
        or_idxs = idxs[or_mask]
        if len(or_idxs) < 2:
            continue
        or_high = bars.h[or_idxs].max()
        or_low = bars.l[or_idxs].min()
        trade_window_start = or_idxs[-1] + 1
        trade_window_end_mask = (bars.date == d) & (bars.hour >= 7) & (bars.hour < 11)
        trade_window = np.flatnonzero(trade_window_end_mask)
        if len(trade_window) == 0:
            continue
        trade_window = trade_window[trade_window >= trade_window_start]
        if len(trade_window) == 0:
            continue
        broken = False
        for i in trade_window:
            if np.isnan(a[i]) or a[i] == 0:
                continue
            if bars.h[i] > or_high and not broken:
                sl = or_low
                tp = bars.c[i] + (bars.c[i] - sl) * 1.5
                t = simulate_trade(bars, i, +1, sl, tp, 24, "london_orb")
                if t:
                    trades.append(t)
                broken = True
                break
            if bars.l[i] < or_low and not broken:
                sl = or_high
                tp = bars.c[i] - (sl - bars.c[i]) * 1.5
                t = simulate_trade(bars, i, -1, sl, tp, 24, "london_orb")
                if t:
                    trades.append(t)
                broken = True
                break
    return trades


def setup_ny_orb(bars: Bars, orb_minutes: int = 15) -> list[Trade]:
    """NY ORB: breakout формированного в 12:30-12:45 UTC."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    unique_dates = np.unique(bars.date)
    for d in unique_dates:
        mask = bars.date == d
        idxs = np.flatnonzero(mask)
        if len(idxs) < 50:
            continue
        or_mask = (bars.hour[idxs] == 12) & (bars.minute[idxs] >= 30) & (bars.minute[idxs] < 30 + orb_minutes)
        or_idxs = idxs[or_mask]
        if len(or_idxs) < 2:
            continue
        or_high = bars.h[or_idxs].max()
        or_low = bars.l[or_idxs].min()
        trade_window_start = or_idxs[-1] + 1
        trade_window_mask = (bars.date == d) & (bars.hour >= 13) & (bars.hour < 17)
        trade_window = np.flatnonzero(trade_window_mask)
        trade_window = trade_window[trade_window >= trade_window_start]
        if len(trade_window) == 0:
            continue
        broken = False
        for i in trade_window:
            if np.isnan(a[i]):
                continue
            if bars.h[i] > or_high and not broken:
                sl = or_low
                tp = bars.c[i] + (bars.c[i] - sl) * 1.5
                t = simulate_trade(bars, i, +1, sl, tp, 24, "ny_orb")
                if t:
                    trades.append(t)
                broken = True
                break
            if bars.l[i] < or_low and not broken:
                sl = or_high
                tp = bars.c[i] - (sl - bars.c[i]) * 1.5
                t = simulate_trade(bars, i, -1, sl, tp, 24, "ny_orb")
                if t:
                    trades.append(t)
                broken = True
                break
    return trades


def setup_asian_range_fade(bars: Bars) -> list[Trade]:
    """Asian range fade: azi range 22-06 UTC. London session fade extremes."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    unique_dates = np.unique(bars.date)
    for d in unique_dates:
        mask = (bars.date == d) & ((bars.hour >= 22) | (bars.hour < 6))
        idxs = np.flatnonzero(mask)
        if len(idxs) < 30:
            continue
        as_high = bars.h[idxs].max()
        as_low = bars.l[idxs].min()
        as_mid = (as_high + as_low) / 2
        trade_mask = (bars.date == d) & (bars.hour >= 7) & (bars.hour < 12)
        trade_window = np.flatnonzero(trade_mask)
        if len(trade_window) == 0:
            continue
        for i in trade_window:
            if np.isnan(a[i]):
                continue
            if bars.h[i] > as_high and bars.c[i] < as_high:
                sl = bars.h[i] + 0.3 * a[i]
                tp = as_mid
                if tp < bars.c[i]:
                    t = simulate_trade(bars, i, -1, sl, tp, 24, "asian_fade")
                    if t:
                        trades.append(t)
                    break
            if bars.l[i] < as_low and bars.c[i] > as_low:
                sl = bars.l[i] - 0.3 * a[i]
                tp = as_mid
                if tp > bars.c[i]:
                    t = simulate_trade(bars, i, +1, sl, tp, 24, "asian_fade")
                    if t:
                        trades.append(t)
                    break
    return trades


def setup_vwap_reversion(bars: Bars, sigma: float = 2.0) -> list[Trade]:
    """Session VWAP reversion: отклонение 2σ от VWAP → возврат."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    unique_dates = np.unique(bars.date)
    tp_price = (bars.h + bars.l + bars.c) / 3
    for d in unique_dates:
        mask = bars.date == d
        idxs = np.flatnonzero(mask)
        if len(idxs) < 100:
            continue
        # VWAP только в торговую сессию London+NY
        tm = (bars.hour[idxs] >= 7) & (bars.hour[idxs] < 17)
        session_idxs = idxs[tm]
        if len(session_idxs) < 40:
            continue
        pv = tp_price[session_idxs] * bars.v[session_idxs]
        vv = bars.v[session_idxs]
        cum_pv = np.cumsum(pv)
        cum_v = np.cumsum(vv)
        cum_v = np.where(cum_v == 0, 1e-9, cum_v)
        vwap = cum_pv / cum_v
        # running std of (price - vwap)
        dev = tp_price[session_idxs] - vwap
        # rolling 20-bar std
        n = 20
        std = np.full_like(dev, np.nan)
        for i in range(n - 1, len(dev)):
            std[i] = dev[i - n + 1:i + 1].std(ddof=0)
        last_trade_idx = -1
        for j in range(n, len(session_idxs)):
            i = session_idxs[j]
            if np.isnan(std[j]) or std[j] == 0 or np.isnan(a[i]):
                continue
            if j - last_trade_idx < 12:  # не чаще 1/час
                continue
            z = dev[j] / std[j]
            if z > sigma:
                sl = bars.c[i] + 0.8 * a[i]
                tp = vwap[j]
                if tp < bars.c[i]:
                    t = simulate_trade(bars, i, -1, sl, tp, 18, "vwap_rev")
                    if t:
                        trades.append(t)
                        last_trade_idx = j
            elif z < -sigma:
                sl = bars.c[i] - 0.8 * a[i]
                tp = vwap[j]
                if tp > bars.c[i]:
                    t = simulate_trade(bars, i, +1, sl, tp, 18, "vwap_rev")
                    if t:
                        trades.append(t)
                        last_trade_idx = j
    return trades


def setup_vol_contraction(bars: Bars) -> list[Trade]:
    """Bollinger squeeze: BB width в 30% процентиле → breakout."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    n = 20
    std = rolling_std(bars.c, n)
    ma = np.full_like(bars.c, np.nan)
    for i in range(n - 1, len(bars.c)):
        ma[i] = bars.c[i - n + 1:i + 1].mean()
    bb_width = 4 * std  # 2σ на обе стороны
    # rolling percentile BB width
    lookback = 96  # 8h
    last_trade = -10000
    for i in range(lookback, len(bars.c) - 1):
        if np.isnan(bb_width[i]) or np.isnan(a[i]):
            continue
        if i - last_trade < 24:
            continue
        recent_bw = bb_width[i - lookback:i]
        if np.isnan(recent_bw).all():
            continue
        pct = np.nanpercentile(recent_bw, 30)
        if bb_width[i] > pct:
            continue
        # squeeze — wait for breakout in next bar
        upper = ma[i] + 2 * std[i]
        lower = ma[i] - 2 * std[i]
        if bars.h[i] > upper:
            sl = bars.c[i] - 1.0 * a[i]
            tp = bars.c[i] + 1.5 * a[i]
            t = simulate_trade(bars, i, +1, sl, tp, 24, "vol_contract")
            if t:
                trades.append(t)
                last_trade = i
        elif bars.l[i] < lower:
            sl = bars.c[i] + 1.0 * a[i]
            tp = bars.c[i] - 1.5 * a[i]
            t = simulate_trade(bars, i, -1, sl, tp, 24, "vol_contract")
            if t:
                trades.append(t)
                last_trade = i
    return trades


def setup_ema_pullback(bars: Bars) -> list[Trade]:
    """EMA20 pullback: на M5 trend (EMA50 > EMA200) pullback к EMA20."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    e20 = ema(bars.c, 20)
    e50 = ema(bars.c, 50)
    e200 = ema(bars.c, 200)
    last_trade = -10000
    for i in range(210, len(bars.c) - 1):
        if np.isnan(a[i]) or np.isnan(e20[i]) or np.isnan(e50[i]) or np.isnan(e200[i]):
            continue
        if i - last_trade < 12:
            continue
        # Только London+NY session
        if bars.hour[i] < 7 or bars.hour[i] >= 17:
            continue
        trend_up = e50[i] > e200[i] and bars.c[i] > e50[i]
        trend_dn = e50[i] < e200[i] and bars.c[i] < e50[i]
        if trend_up:
            # pullback коснулся EMA20 последними 3 барами, текущий отскакивает
            touched = bars.l[i - 2:i + 1].min() <= e20[i] <= bars.h[i - 2:i + 1].max()
            bullish = bars.c[i] > bars.o[i]
            if touched and bullish and bars.c[i] > e20[i]:
                sl = bars.l[i - 2:i + 1].min() - 0.2 * a[i]
                tp = bars.c[i] + (bars.c[i] - sl) * 1.5
                t = simulate_trade(bars, i, +1, sl, tp, 24, "ema_pullback")
                if t:
                    trades.append(t)
                    last_trade = i
        elif trend_dn:
            touched = bars.l[i - 2:i + 1].min() <= e20[i] <= bars.h[i - 2:i + 1].max()
            bearish = bars.c[i] < bars.o[i]
            if touched and bearish and bars.c[i] < e20[i]:
                sl = bars.h[i - 2:i + 1].max() + 0.2 * a[i]
                tp = bars.c[i] - (sl - bars.c[i]) * 1.5
                t = simulate_trade(bars, i, -1, sl, tp, 24, "ema_pullback")
                if t:
                    trades.append(t)
                    last_trade = i
    return trades


def setup_session_retest(bars: Bars) -> list[Trade]:
    """Asian session high/low retest в London session."""
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    unique_dates = np.unique(bars.date)
    for d in unique_dates:
        as_mask = (bars.date == d) & ((bars.hour >= 22) | (bars.hour < 6))
        as_idxs = np.flatnonzero(as_mask)
        if len(as_idxs) < 30:
            continue
        as_high = bars.h[as_idxs].max()
        as_low = bars.l[as_idxs].min()
        ln_mask = (bars.date == d) & (bars.hour >= 7) & (bars.hour < 14)
        ln_idxs = np.flatnonzero(ln_mask)
        if len(ln_idxs) < 10:
            continue
        # ждём пробой, потом retest
        broke_high = False
        broke_low = False
        for i in ln_idxs:
            if np.isnan(a[i]):
                continue
            if not broke_high and bars.h[i] > as_high + 0.2 * a[i]:
                broke_high = True
                continue
            if not broke_low and bars.l[i] < as_low - 0.2 * a[i]:
                broke_low = True
                continue
            if broke_high and bars.l[i] <= as_high <= bars.h[i] and bars.c[i] > as_high:
                sl = as_high - 1.0 * a[i]
                tp = bars.c[i] + (bars.c[i] - sl) * 1.5
                t = simulate_trade(bars, i, +1, sl, tp, 18, "session_retest")
                if t:
                    trades.append(t)
                    broke_high = False
            if broke_low and bars.l[i] <= as_low <= bars.h[i] and bars.c[i] < as_low:
                sl = as_low + 1.0 * a[i]
                tp = bars.c[i] - (sl - bars.c[i]) * 1.5
                t = simulate_trade(bars, i, -1, sl, tp, 18, "session_retest")
                if t:
                    trades.append(t)
                    broke_low = False
    return trades


SETUPS: dict[str, Callable[[Bars], list[Trade]]] = {
    "london_orb": setup_london_orb,
    "ny_orb": setup_ny_orb,
    "asian_fade": setup_asian_range_fade,
    "vwap_rev": setup_vwap_reversion,
    "vol_contract": setup_vol_contraction,
    "ema_pullback": setup_ema_pullback,
    "session_retest": setup_session_retest,
}


# ─────────────────── Статистика + валидация ───────────────────

@dataclass
class Stats:
    setup: str
    phase: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    net_pips: float = 0.0
    gross_pips: float = 0.0
    avg_net: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    pf: float = 0.0
    wr: float = 0.0
    exits: dict[str, int] = field(default_factory=dict)


def stats_from(setup: str, phase: str, trades: list[Trade]) -> Stats:
    s = Stats(setup=setup, phase=phase, n=len(trades))
    if not trades:
        return s
    wins = [t.pnl_pips_net for t in trades if t.pnl_pips_net > 0]
    losses = [t.pnl_pips_net for t in trades if t.pnl_pips_net < 0]
    be = [t for t in trades if t.pnl_pips_net == 0]
    s.wins = len(wins)
    s.losses = len(losses)
    s.breakeven = len(be)
    s.gross_pips = sum(t.pnl_pips_gross for t in trades)
    s.net_pips = sum(t.pnl_pips_net for t in trades)
    s.avg_net = s.net_pips / s.n
    s.avg_win = np.mean(wins) if wins else 0.0
    s.avg_loss = np.mean(losses) if losses else 0.0
    s.pf = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    s.wr = s.wins / s.n if s.n else 0.0
    exits: dict[str, int] = {}
    for t in trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    s.exits = exits
    return s


def permutation_test(trades: list[Trade], n_perm: int = 1000, seed: int = 42) -> float:
    """Перемешиваем знаки направлений и считаем p-value."""
    if not trades:
        return 1.0
    rng = np.random.default_rng(seed)
    obs = sum(t.pnl_pips_net for t in trades)
    # вместо знаков — перемешиваем sign(pnl_gross) и пересчитываем gross
    # проще: берём pnl_pips_gross, добавляем случайный знак, вычитаем cost
    gross = np.asarray([t.pnl_pips_gross for t in trades])
    costs = np.asarray([t.cost_pips for t in trades])
    ge = 0
    for _ in range(n_perm):
        signs = rng.choice([-1, 1], size=len(trades))
        perm_pnl = (gross * signs - costs).sum()
        if perm_pnl >= obs:
            ge += 1
    return (ge + 1) / (n_perm + 1)


# ─────────────────── Main ───────────────────

def split_trades_is_oos(trades: list[Trade], is_frac: float = 0.6) -> tuple[list[Trade], list[Trade]]:
    if not trades:
        return [], []
    ts_sorted = sorted(trades, key=lambda t: t.entry_ts)
    cutoff = ts_sorted[int(len(ts_sorted) * is_frac)].entry_ts if is_frac < 1 else ts_sorted[-1].entry_ts + 1
    is_ = [t for t in ts_sorted if t.entry_ts < cutoff]
    oos = [t for t in ts_sorted if t.entry_ts >= cutoff]
    return is_, oos


def format_stats(s: Stats) -> str:
    if s.n == 0:
        return f"  {s.phase:>4}: n=0"
    return (
        f"  {s.phase:>4}: n={s.n:4d}  "
        f"wr={s.wr*100:5.1f}%  "
        f"net={s.net_pips:+8.1f} pips  "
        f"avg={s.avg_net:+5.2f}  "
        f"pf={s.pf:5.2f}  "
        f"W={s.avg_win:+5.2f} L={s.avg_loss:+5.2f}  "
        f"exits={s.exits}"
    )


def main() -> None:
    print("=" * 100)
    print("M5 SCALP SETUPS BACKTEST — 7 сетапов × 12 инструментов")
    print("=" * 100)
    print(f"Costs per trade (round-trip, pips):")
    for sym in PORTFOLIO:
        print(f"  {sym:10s} = {cost_pips(sym):.2f} pips")
    print()

    all_bars = {}
    for sym in PORTFOLIO:
        try:
            all_bars[sym] = load(sym)
        except Exception as e:
            print(f"  !!! load {sym} failed: {e}")

    # Собираем trade'ы по setup × sym
    all_trades_by_setup: dict[str, list[Trade]] = {k: [] for k in SETUPS}
    trading_days_by_sym: dict[str, int] = {}
    for sym, bars in all_bars.items():
        trading_days_by_sym[sym] = len(np.unique(bars.date))
        for setup_name, fn in SETUPS.items():
            try:
                ts = fn(bars)
            except Exception as e:
                print(f"  !!! setup {setup_name} on {sym} failed: {e}")
                ts = []
            all_trades_by_setup[setup_name].extend(ts)

    # Итого по портфелю дней (возьмём median как estimate)
    total_days_union: set[int] = set()
    for bars in all_bars.values():
        total_days_union.update(map(int, np.unique(bars.date)))
    total_days = len(total_days_union)
    print(f"Всего календарных дней в данных: {total_days}")
    print()

    # Результаты
    summary_rows: list[dict] = []
    for setup_name in SETUPS:
        trades = all_trades_by_setup[setup_name]
        is_, oos = split_trades_is_oos(trades, is_frac=0.6)
        st_all = stats_from(setup_name, "ALL", trades)
        st_is = stats_from(setup_name, "IS", is_)
        st_oos = stats_from(setup_name, "OOS", oos)
        p_all = permutation_test(trades, n_perm=1000)
        p_oos = permutation_test(oos, n_perm=1000)

        signals_per_day = st_all.n / max(total_days, 1)

        print("─" * 100)
        print(
            f"SETUP: {setup_name}  total_trades={st_all.n}  "
            f"~{signals_per_day:.2f} signals/day  "
            f"p_all={p_all:.4f}  p_oos={p_oos:.4f}"
        )
        print(format_stats(st_all))
        print(format_stats(st_is))
        print(format_stats(st_oos))

        # Разбивка по инструментам
        per_sym: dict[str, list[Trade]] = {}
        for t in trades:
            per_sym.setdefault(t.sym, []).append(t)
        sym_lines: list[str] = []
        for sym, ts_ in sorted(per_sym.items(), key=lambda x: -sum(t.pnl_pips_net for t in x[1])):
            st = stats_from(setup_name, sym, ts_)
            sym_lines.append(f"    {sym:10s} n={st.n:3d}  net={st.net_pips:+7.1f}  wr={st.wr*100:4.1f}%  pf={st.pf:4.2f}")
        print("  Per-symbol:")
        for line in sym_lines:
            print(line)

        summary_rows.append({
            "setup": setup_name,
            "n": st_all.n,
            "signals_per_day": round(signals_per_day, 3),
            "net_pips_all": round(st_all.net_pips, 1),
            "net_pips_is": round(st_is.net_pips, 1),
            "net_pips_oos": round(st_oos.net_pips, 1),
            "wr_all": round(st_all.wr * 100, 1),
            "wr_oos": round(st_oos.wr * 100, 1),
            "pf_all": round(st_all.pf, 2) if st_all.pf != float("inf") else 999.99,
            "pf_oos": round(st_oos.pf, 2) if st_oos.pf != float("inf") else 999.99,
            "avg_net": round(st_all.avg_net, 2),
            "p_all": round(p_all, 4),
            "p_oos": round(p_oos, 4),
        })

    # Сводка
    print()
    print("=" * 100)
    print("СВОДКА")
    print("=" * 100)
    print(f"{'setup':<20}  {'n':>5}  {'/day':>6}  {'net':>8}  {'is':>8}  {'oos':>8}  {'wr%':>5}  {'pf':>5}  {'p_all':>6}  {'p_oos':>6}")
    for r in sorted(summary_rows, key=lambda x: -x["net_pips_oos"]):
        print(
            f"{r['setup']:<20}  "
            f"{r['n']:>5}  "
            f"{r['signals_per_day']:>6.2f}  "
            f"{r['net_pips_all']:>+8.1f}  "
            f"{r['net_pips_is']:>+8.1f}  "
            f"{r['net_pips_oos']:>+8.1f}  "
            f"{r['wr_all']:>4.1f}  "
            f"{r['pf_all']:>5.2f}  "
            f"{r['p_all']:>6.4f}  "
            f"{r['p_oos']:>6.4f}"
        )

    # Сохраним
    out = Path(__file__).resolve().parents[1] / "data" / "scalp_setups_m5_summary.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
