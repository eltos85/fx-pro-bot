"""OOS-бэктест вариантов выхода momentum-стратегии (FX-мажоры).

Цель (no-data-fitting): проверить гипотезу пользователя — даёт ли РАННИЙ
безубыток (BE@0.5R/0.4R) преимущество над текущим BE@1.0R, БЕЗ подгонки
под живые 25 сделок. Сравнение honest: одинаковые ВХОДЫ, разные пороги BE.

Реплика стратегии (src/fx_momentum_bot):
  entry  : edge-trigger по флипу 1h-momentum = close/close[-lookback]-1
           (lookback=24, threshold=0.0015); один трейд на символ.
  SL     : 2.5*ATR14(1h) от entry; без брокерского TP.
  mgmt   : BE@break_even_r (стоп->entry), partial@1.5R 50%, ATR-trailing@1.5R
           (1.5*ATR); sign-decay: флип 1h-momentum против позиции -> закрытие.
  учёт   : в R-единицах (1R = 2.5*ATR_entry). partial = 0.5 размера на 1.5R.

Данные: yfinance 5m (60d) -> resample 1h для сигнала, 5m для пути выхода.
Оговорки: mid-цены (без спреда/комиссии — одинаково для всех вариантов,
сравнение валидно), без event/spread-guard (входы консистентны между
вариантами), 5m-гранулярность intrabar. OOS = вторая половина окна.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

SYMBOLS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]
LOOKBACK = 24
ATR_PERIOD = 14
THRESHOLD = 0.0015
ATR_STOP_MULT = 2.5
PARTIAL_R = 1.5
PARTIAL_FRAC = 0.5
TRAIL_R = 1.5
TRAIL_ATR = 1.5


def atr(df, period):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def load(sym):
    df = yf.download(sym, period="60d", interval="5m", progress=False, auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    return df


def to_1h(df5):
    o = df5["Open"].resample("1h").first()
    h = df5["High"].resample("1h").max()
    l = df5["Low"].resample("1h").min()
    c = df5["Close"].resample("1h").last()
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c}).dropna()


def signal_dir(h1_close_slice):
    if len(h1_close_slice) < LOOKBACK + 1:
        return "flat"
    m = h1_close_slice.iloc[-1] / h1_close_slice.iloc[-1 - LOOKBACK] - 1.0
    if m > THRESHOLD:
        return "long"
    if m < -THRESHOLD:
        return "short"
    return "flat"


def backtest_symbol(df5, df1h, break_even_r):
    """Возврат списка (entry_time, R) закрытых трейдов."""
    h1_close = df1h["Close"]
    h1_atr = atr(df1h, ATR_PERIOD)
    # для каждого 1h таймстампа — direction и atr (на закрытии этого бара)
    dirs = {}
    atrs = {}
    closes = list(h1_close.index)
    for i, ts in enumerate(closes):
        dirs[ts] = signal_dir(h1_close.iloc[: i + 1])
        atrs[ts] = float(h1_atr.iloc[i]) if not np.isnan(h1_atr.iloc[i]) else 0.0

    h1_index = pd.DatetimeIndex(closes)
    trades = []
    last_direction = "flat"
    pos = None  # dict
    last_seen_h1 = None

    for ts, bar in df5.iterrows():
        # последний ЗАКРЫТЫЙ 1h бар (строго раньше текущего 5m)
        loc = h1_index.searchsorted(ts, side="right") - 1
        if loc < 0:
            continue
        h1_ts = h1_index[loc]
        cur_dir = dirs[h1_ts]
        cur_atr = atrs[h1_ts]

        hi, lo, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])

        if pos is not None:
            entry, side, risk = pos["entry"], pos["side"], pos["risk"]
            # 1) стоп/трейл-стоп — проверяем на adverse первым (консервативно)
            if side == "long":
                if lo <= pos["sl"]:
                    r_exit = (pos["sl"] - entry) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                    pos = None
                else:
                    r_now = (hi - entry) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        trades_partialR = PARTIAL_FRAC * PARTIAL_R
                        pos["realizedR"] += trades_partialR
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= break_even_r:
                        pos["sl"] = max(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R and cur_atr > 0:
                        pos["sl"] = max(pos["sl"], close - TRAIL_ATR * cur_atr)
            else:
                if hi >= pos["sl"]:
                    r_exit = (entry - pos["sl"]) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                    pos = None
                else:
                    r_now = (entry - lo) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= break_even_r:
                        pos["sl"] = min(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R and cur_atr > 0:
                        pos["sl"] = min(pos["sl"], close + TRAIL_ATR * cur_atr)

        # sign-decay: на НОВОМ 1h баре, если флип против позиции — закрыть
        if pos is not None and h1_ts != last_seen_h1:
            opp = "short" if pos["side"] == "long" else "long"
            if cur_dir == opp:
                if pos["side"] == "long":
                    r_exit = (close - pos["entry"]) / pos["risk"]
                else:
                    r_exit = (pos["entry"] - close) / pos["risk"]
                trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                pos = None

        # вход: edge-trigger по флипу direction (на новом 1h баре)
        if h1_ts != last_seen_h1:
            if pos is None and cur_dir in ("long", "short") and cur_dir != last_direction and cur_atr > 0:
                risk = cur_atr * ATR_STOP_MULT
                sl = close - risk if cur_dir == "long" else close + risk
                pos = {
                    "t": ts, "entry": close, "side": cur_dir, "risk": risk,
                    "sl": sl, "size": 1.0, "realizedR": 0.0, "be": False, "partial": False,
                }
            last_direction = cur_dir
            last_seen_h1 = h1_ts

    return trades


def stats(label, rs):
    if not rs:
        print(f"  {label:<10} n=0")
        return
    rs = np.array(rs)
    wins = rs[rs > 0]
    losses = rs[rs < 0]
    wr = len(wins) / len(rs) * 100
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    aw = wins.mean() if len(wins) else 0
    al = losses.mean() if len(losses) else 0
    print(f"  {label:<10} n={len(rs):>3} netR={rs.sum():+7.2f} WR={wr:>3.0f}% "
          f"avgR={rs.mean():+5.2f} PF={pf:>4.2f} avgW={aw:+4.2f} avgL={al:+5.2f}")


def main():
    variants = {"BE@1.0R(base)": 1.0, "BE@0.5R": 0.5, "BE@0.4R": 0.4}
    data = {}
    for sym in SYMBOLS:
        df5 = load(sym)
        if df5 is None or len(df5) < 3000:
            print(f"WARN {sym}: мало данных", file=sys.stderr)
            continue
        data[sym] = (df5, to_1h(df5))

    if not data:
        print("нет данных")
        return

    # OOS split по медиане времени всех баров
    all_idx = pd.DatetimeIndex(sorted(set().union(*[d[0].index for d in data.values()])))
    split = all_idx[len(all_idx) // 2]
    print(f"Окно: {all_idx[0].date()} → {all_idx[-1].date()}  | OOS-split: {split.date()}")
    print(f"Символы: {list(data.keys())}\n")

    for vname, be in variants.items():
        is_rs, oos_rs = [], []
        for sym, (df5, df1h) in data.items():
            for t, r in backtest_symbol(df5, df1h, be):
                (is_rs if t < split else oos_rs).append(r)
        print(f"=== {vname} ===")
        stats("IS", is_rs)
        stats("OOS", oos_rs)
        stats("ALL", is_rs + oos_rs)
        print()


if __name__ == "__main__":
    main()
