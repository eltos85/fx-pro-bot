"""Валидация канона momentum-стратегии на 2 годах 1h-данных (research-only).

Контекст (BUILDLOG 2026-07-10, запрос «верен ли канон вообще»): ядро бота —
24-барный 1h momentum с фикс-порогом 0.15% — цитирует Moskowitz/Ooi/Pedersen
2012, но канонический TSMOM это 12-МЕСЯЧНЫЙ lookback на фьючерсах с
месячным ребалансом и vol-scaling. Перенос на 24 часа литературой напрямую
не подтверждён (Neely/Weller 2003: intraday FX rules не переживают costs;
Menkhoff et al. 2012 JFE: FX momentum живёт на 1-12 мес formation и в
основном в high-spread минорах; arXiv 2501.16772: trending-режим начинается
с «нескольких часов» — 24h на границе). Стратегия НИ РАЗУ не проверялась
дальше 60 дней (лимит yfinance 5m). Здесь: 1h-бары, лимит 730 дней.

Варианты (все с одинаковым сопровождением BE@1R/partial@1.5R/trail 1.5ATR,
sign-decay zero-cross как в live, SL 2.5*ATR):
  base      — фикс THRESHOLD=0.0015 (текущий конфиг);
  volnorm   — порог в vol-единицах: |m| > K * ATR%/close-нормированной
              24h-волатильности (K подобран так, чтобы частота сигналов
              на IS-половине была сопоставима с base — не оптимизация
              прибыли, а выравнивание turnover для честного сравнения).

Издержки: −0.06R на сделку (замерено на live 06-05→07-10: fill в среднем
на 0.06R хуже сигнального close, см. BUILDLOG 2026-07-10). Гранулярность
1h intrabar: SL проверяется adverse-first (консервативно), partial/trail
по экстремумам бара — грубее 5m-реплики, но одинаково для всех вариантов.

Отчёт: per-quarter netR, IS (первый год) vs OOS (второй год).
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
BE_R = 1.0
COST_R = 0.06  # замерено live: спред+задержка входа, R на сделку


def load_1h(sym: str) -> pd.DataFrame | None:
    df = yf.download(sym, period="730d", interval="1h", progress=False,
                     auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["Open", "High", "Low", "Close"]].dropna()


def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def backtest(df: pd.DataFrame, mode: str, k_vol: float) -> list[tuple[pd.Timestamp, float]]:
    close = df["Close"]
    atr = atr_series(df, ATR_PERIOD)
    mom = close / close.shift(LOOKBACK) - 1.0
    # 24h-волатильность в тех же единицах, что momentum (относительный ход):
    # ATR — средний часовой ход; за 24 независимых часа масштаб ~ ATR*sqrt(24).
    vol24 = atr / close * np.sqrt(LOOKBACK)

    trades: list[tuple[pd.Timestamp, float]] = []
    last_dir = "flat"
    pos = None

    for i in range(len(df)):
        m = mom.iloc[i]
        a = atr.iloc[i]
        if np.isnan(m) or np.isnan(a) or a <= 0:
            continue
        if mode == "base":
            thr = THRESHOLD
        else:  # volnorm
            v = vol24.iloc[i]
            if np.isnan(v) or v <= 0:
                continue
            thr = k_vol * v
        cur_dir = "long" if m > thr else "short" if m < -thr else "flat"

        hi = float(df["High"].iloc[i])
        lo = float(df["Low"].iloc[i])
        c = float(close.iloc[i])
        ts = df.index[i]

        if pos is not None:
            entry, side, risk = pos["entry"], pos["side"], pos["risk"]
            if side == "long":
                if lo <= pos["sl"]:
                    r_exit = (pos["sl"] - entry) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit - COST_R))
                    pos = None
                else:
                    r_now = (hi - entry) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= BE_R:
                        pos["sl"] = max(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R:
                        pos["sl"] = max(pos["sl"], c - TRAIL_ATR * a)
            else:
                if hi >= pos["sl"]:
                    r_exit = (entry - pos["sl"]) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit - COST_R))
                    pos = None
                else:
                    r_now = (entry - lo) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= BE_R:
                        pos["sl"] = min(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R:
                        pos["sl"] = min(pos["sl"], c + TRAIL_ATR * a)

        # sign-decay: zero-cross против позиции (live-семантика)
        if pos is not None:
            fire = (pos["side"] == "long" and m < 0) or (pos["side"] == "short" and m > 0)
            if fire:
                if pos["side"] == "long":
                    r_exit = (c - pos["entry"]) / pos["risk"]
                else:
                    r_exit = (pos["entry"] - c) / pos["risk"]
                trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit - COST_R))
                pos = None

        # вход: edge-trigger по смене direction, один трейд на символ
        if pos is None and cur_dir in ("long", "short") and cur_dir != last_dir:
            risk = a * ATR_STOP_MULT
            sl = c - risk if cur_dir == "long" else c + risk
            pos = {"t": ts, "entry": c, "side": cur_dir, "risk": risk, "sl": sl,
                   "size": 1.0, "realizedR": 0.0, "be": False, "partial": False}
        last_dir = cur_dir

    return trades


def stats_line(label: str, rs: list[float]) -> str:
    if not rs:
        return f"  {label:<10} n=0"
    a = np.array(rs)
    w, l = a[a > 0], a[a < 0]
    pf = w.sum() / -l.sum() if l.sum() < 0 else float("inf")
    return (f"  {label:<10} n={len(a):>4} netR={a.sum():+8.2f} "
            f"WR={len(w)/len(a)*100:>3.0f}% avgR={a.mean():+5.2f} PF={pf:>5.2f}")


def main() -> int:
    data = {}
    for sym in SYMBOLS:
        df = load_1h(sym)
        if df is None or len(df) < 5000:
            print(f"WARN {sym}: мало 1h данных", file=sys.stderr)
            continue
        data[sym] = df
    if not data:
        return 1

    any_df = next(iter(data.values()))
    t0, t1 = any_df.index[0], any_df.index[-1]
    mid = t0 + (t1 - t0) / 2
    print(f"Окно: {t0.date()} → {t1.date()} | IS/OOS split: {mid.date()} | "
          f"cost/trade: −{COST_R}R\n")

    # k_vol: медиана base-порога в vol-единицах на IS-половине (совместимый
    # turnover, не оптимизация): thr_vol = THRESHOLD / vol24 медианно.
    ks = []
    for df in data.values():
        vol24 = (atr_series(df, ATR_PERIOD) / df["Close"] * np.sqrt(LOOKBACK))
        is_part = vol24[vol24.index < mid].dropna()
        ks.append(float(THRESHOLD / is_part.median()))
    k_vol = float(np.median(ks))
    print(f"volnorm: K = {k_vol:.3f} (медиана THRESHOLD/vol24 на IS — "
          f"turnover-эквивалент, не подгонка прибыли)\n")

    for mode in ("base", "volnorm"):
        all_tr: list[tuple[pd.Timestamp, float]] = []
        for sym, df in data.items():
            all_tr += backtest(df, mode, k_vol)
        print(f"=== {mode} ===")
        is_rs = [r for t, r in all_tr if t < mid]
        oos_rs = [r for t, r in all_tr if t >= mid]
        print(stats_line("IS", is_rs))
        print(stats_line("OOS", oos_rs))
        print(stats_line("ALL", [r for _, r in all_tr]))
        q: dict[str, list[float]] = {}
        for t, r in all_tr:
            q.setdefault(f"{t.year}-Q{(t.month - 1) // 3 + 1}", []).append(r)
        for k in sorted(q):
            print(stats_line(k, q[k]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
