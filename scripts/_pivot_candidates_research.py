"""Research-этап смены стратегии momentum-бота (BUILDLOG 2026-07-10).

Канон 24h-TSMOM на FX-мажорах опровергнут (см. _momentum_canon_validation.py:
gross +0.02R/сделку за 2 года — шум; с live-издержками −0.06R/сделку глубокий
минус). По решению пользователя проверяем два направления ДО написания кода
бота (no-data-fitting: сначала артефакт анализа, потом код).

── Кандидат B: intraday mean-reversion на FX-мажорах (пункт 2) ─────────────
Research-основа:
  * Neely/Weller 2003: intraday FX — стабильная НЕГАТИВНАЯ автокорреляция
    (low-order), но у них же: costs съедают простые правила → нужен фильтр.
  * Quantile-regression evidence (Intra-day dynamics of exchange rates, QREF
    2019): умеренные intraday-ходы ревертят, ЭКСТРЕМАЛЬНЫЕ продолжаются →
    нельзя fade-ить capitulation (совпадает с удалением atr_spike из
    OutsidersStrategy, Chande/Kroll 1994).
  * Параметры канонические, БЕЗ подбора: Bollinger 2001 — BB(20, 2σ);
    Wilder 1978 — RSI(14) 30/70; session-фильтр [7,21) UTC (Lyons 2001,
    STRATEGIES.md liquid sessions); SL 2.5×ATR и издержки −0.06R/сделку —
    конвенции этого бота (сопоставимость с momentum-цифрами).
Правила:
  B1: close пробил нижнюю BB(20,2) → long (short зеркально); выход — touch
      SMA20 (mean), time-stop 24 бара, SL 2.5×ATR intrabar (adverse-first).
  B2: как B1 + подтверждение RSI(14) < 30 / > 70.
  Guard обоих: |close − SMA20| ≤ 4×ATR (не fade-им capitulation).
Данные: yfinance 1h, 730d, 4 мажора. IS = первый год, OOS = второй.

── Кандидат C: канонический multi-asset TSMOM на дневках (пункт 3) ─────────
Research-основа: Moskowitz/Ooi/Pedersen 2012 (JFE) — sign(mom за 12 мес),
vol-scaling к 40% годовой, месячный ребаланс, ДИВЕРСИФИЦИРОВАННАЯ вселенная.
Hurst/Ooi/Pedersen 2017 («A Century of Evidence…») — 3/6/12-мес комбинация.
Вселенная — только инструменты, доступные на cTrader-счёте бота (маппинг
executor.symbols): FX-мажоры, XAUUSD (GC=F), нефть (CL=F), газ (NG=F),
US500 (ES=F), BTC, ETH. Данные: yfinance 1d, 10y.
Издержки: 5 bp на единицу turnover (консервативно для FX/металлов/индексов,
крипта на cTrader дороже — отдельно смотрим срез без крипты).

Только research: код бота не меняется до одобрения результатов.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

FX = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]

# ── общие утилиты ──────────────────────────────────────────────────────────

def load(sym: str, period: str, interval: str) -> pd.DataFrame | None:
    df = yf.download(sym, period=period, interval=interval, progress=False,
                     auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["Open", "High", "Low", "Close"]].dropna()


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def stats_line(label: str, rs: list[float]) -> str:
    if not rs:
        return f"  {label:<22} n=0"
    a = np.array(rs)
    w, l = a[a > 0], a[a < 0]
    pf = w.sum() / -l.sum() if l.sum() < 0 else float("inf")
    return (f"  {label:<22} n={len(a):>4} netR={a.sum():+8.2f} "
            f"WR={len(w)/len(a)*100:>3.0f}% avgR={a.mean():+5.2f} PF={pf:>5.2f}")


# ── Кандидат B: BB mean-reversion 1h ───────────────────────────────────────

BB_PERIOD = 20          # Bollinger 2001 canonical
BB_SIGMA = 2.0
RSI_PERIOD = 14         # Wilder 1978
RSI_LO, RSI_HI = 30.0, 70.0
SL_ATR = 2.5            # конвенция бота (сопоставимость R)
TIME_STOP_BARS = 24
CAPITULATION_ATR = 4.0  # Chande/Kroll 1994 — не fade-им
SESSION = (7, 21)       # Lyons 2001 / STRATEGIES.md
COST_R = 0.06           # замер live (BUILDLOG 2026-07-10)


def rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def backtest_mr(df: pd.DataFrame, use_rsi: bool) -> list[tuple[pd.Timestamp, float]]:
    close = df["Close"]
    sma = close.rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
    sd = close.rolling(BB_PERIOD, min_periods=BB_PERIOD).std()
    upper, lower = sma + BB_SIGMA * sd, sma - BB_SIGMA * sd
    atr = atr_series(df)
    rsi = rsi_series(close, RSI_PERIOD)

    trades: list[tuple[pd.Timestamp, float]] = []
    pos = None
    prev_out = 0  # был ли прошлый бар за лентой (edge-trigger, не пирамидим)

    for i in range(len(df)):
        c = float(close.iloc[i])
        hi, lo = float(df["High"].iloc[i]), float(df["Low"].iloc[i])
        m, u, d = sma.iloc[i], upper.iloc[i], lower.iloc[i]
        a = atr.iloc[i]
        ts = df.index[i]
        if np.isnan(m) or np.isnan(a) or a <= 0:
            continue

        if pos is not None:
            entry, side, risk = pos["entry"], pos["side"], pos["risk"]
            # SL adverse-first
            if side == "long" and lo <= pos["sl"]:
                trades.append((pos["t"], (pos["sl"] - entry) / risk - COST_R))
                pos = None
            elif side == "short" and hi >= pos["sl"]:
                trades.append((pos["t"], (entry - pos["sl"]) / risk - COST_R))
                pos = None
            else:
                # выход: касание mean (внутри бара) или time-stop на close
                hit_mean = (side == "long" and hi >= m) or (side == "short" and lo <= m)
                timed_out = i - pos["i"] >= TIME_STOP_BARS
                if hit_mean:
                    px = m
                elif timed_out:
                    px = c
                if hit_mean or timed_out:
                    r = (px - entry) / risk if side == "long" else (entry - px) / risk
                    trades.append((pos["t"], r - COST_R))
                    pos = None

        out = 1 if c > u else (-1 if c < d else 0)
        if pos is None and out != 0 and prev_out == 0:
            in_session = SESSION[0] <= ts.hour < SESSION[1]
            not_capit = abs(c - m) <= CAPITULATION_ATR * a
            rsi_ok = True
            if use_rsi:
                r_val = rsi.iloc[i]
                rsi_ok = (out < 0 and r_val < RSI_LO) or (out > 0 and r_val > RSI_HI)
            if in_session and not_capit and rsi_ok:
                side = "long" if out < 0 else "short"  # fade пробоя ленты
                risk = SL_ATR * a
                sl = c - risk if side == "long" else c + risk
                pos = {"t": ts, "i": i, "entry": c, "side": side,
                       "risk": risk, "sl": sl}
        prev_out = out

    return trades


def run_candidate_b() -> None:
    print("=" * 72)
    print("Кандидат B: intraday mean-reversion FX-мажоры (1h, 2 года, "
          f"cost −{COST_R}R/сделку)")
    print("=" * 72)
    data = {s: load(s, "730d", "1h") for s in FX}
    data = {s: d for s, d in data.items() if d is not None and len(d) > 5000}
    any_df = next(iter(data.values()))
    t0, t1 = any_df.index[0], any_df.index[-1]
    mid = t0 + (t1 - t0) / 2
    print(f"Окно: {t0.date()} → {t1.date()} | IS/OOS split: {mid.date()}\n")

    for name, use_rsi in (("B1 BB(20,2σ) fade", False),
                          ("B2 BB + RSI(14) 30/70", True)):
        all_tr: list[tuple[pd.Timestamp, float]] = []
        per_sym: dict[str, list[float]] = {}
        for sym, df in data.items():
            tr = backtest_mr(df, use_rsi)
            all_tr += tr
            per_sym[sym] = [r for _, r in tr]
        print(f"--- {name} ---")
        print(stats_line("IS", [r for t, r in all_tr if t < mid]))
        print(stats_line("OOS", [r for t, r in all_tr if t >= mid]))
        print(stats_line("ALL", [r for _, r in all_tr]))
        for sym in sorted(per_sym):
            print(stats_line(f"  {sym}", per_sym[sym]))
        halves: dict[str, list[float]] = {}
        for t, r in all_tr:
            halves.setdefault(f"{t.year}-H{1 if t.month <= 6 else 2}", []).append(r)
        for k in sorted(halves):
            print(stats_line(f"  {k}", halves[k]))
        print()


# ── Кандидат C: multi-asset daily TSMOM ────────────────────────────────────

UNIVERSE = {
    # yf-символ: (доступен на cTrader, класс)
    "EURUSD=X": "fx", "GBPUSD=X": "fx", "USDJPY=X": "fx", "AUDUSD=X": "fx",
    "GC=F": "cmd", "CL=F": "cmd", "NG=F": "cmd",
    "ES=F": "idx",
    "BTC-USD": "crypto", "ETH-USD": "crypto",
}
TARGET_VOL = 0.40       # Moskowitz 2012: 40% ex-ante vol на инструмент
VOL_WIN = 60            # окно оценки vol (дни) — стандарт TSMOM-реплик
COST_BP = 5.0           # bp на единицу turnover
LOOKBACKS = {"3m": 63, "6m": 126, "12m": 252}


def run_candidate_c() -> None:
    print("=" * 72)
    print("Кандидат C: multi-asset daily TSMOM (10y, vol-scaling 40%, "
          f"месячный ребаланс, cost {COST_BP}bp/turnover)")
    print("=" * 72)
    closes = {}
    for sym in UNIVERSE:
        df = load(sym, "10y", "1d")
        if df is not None and len(df) > 1000:
            closes[sym] = df["Close"]
    print(f"Инструменты ({len(closes)}): {sorted(closes)}\n")

    rets = pd.DataFrame({s: c.pct_change() for s, c in closes.items()})
    vol = rets.ewm(span=VOL_WIN, min_periods=VOL_WIN).std() * np.sqrt(252)

    month = rets.index.to_period("M")
    results = {}
    for lb_name, lb in LOOKBACKS.items():
        mom = pd.DataFrame({s: closes[s] / closes[s].shift(lb) - 1.0 for s in closes})
        mom = mom.reindex(rets.index)
        # позиция пересматривается на последнем дне месяца, держится месяц
        sign = np.sign(mom)
        w = (sign * (TARGET_VOL / vol)).clip(-10, 10)
        is_month_end = month != np.roll(month, -1)
        w_reb = w.where(pd.Series(is_month_end, index=w.index), np.nan)
        w_held = w_reb.shift(1).ffill()  # без look-ahead: вес со след. дня
        n_active = w_held.notna().sum(axis=1).replace(0, np.nan)
        port_gross = (w_held * rets).sum(axis=1) / n_active
        turnover = w_held.diff().abs().sum(axis=1) / n_active
        port_net = port_gross - turnover * COST_BP / 10000.0
        results[lb_name] = (port_gross.dropna(), port_net.dropna())

    # комбинация 3/6/12 (Hurst/Ooi/Pedersen 2017)
    combo_g = pd.concat([g for g, _ in results.values()], axis=1).mean(axis=1)
    combo_n = pd.concat([n for _, n in results.values()], axis=1).mean(axis=1)
    results["combo"] = (combo_g.dropna(), combo_n.dropna())

    def report(name: str, x: pd.Series) -> str:
        ann = x.mean() * 252
        sh = x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else 0.0
        # maxDD
        eq = (1 + x).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        return f"  {name:<14} annRet={ann*100:+6.2f}%  Sharpe={sh:+.2f}  maxDD={dd*100:.1f}%"

    for lb_name, (g, n) in results.items():
        print(report(f"{lb_name} gross", g))
        print(report(f"{lb_name} net", n))
    # срез без крипты (cTrader-спреды на крипту хуже 5bp)
    print("\nБез крипты (BTC/ETH исключены):")
    no_c = [s for s in closes if UNIVERSE[s] != "crypto"]
    rets_nc = rets[no_c]
    vol_nc = vol[no_c]
    for lb_name, lb in LOOKBACKS.items():
        mom = pd.DataFrame({s: closes[s] / closes[s].shift(lb) - 1.0 for s in no_c})
        mom = mom.reindex(rets_nc.index)
        sign = np.sign(mom)
        w = (sign * (TARGET_VOL / vol_nc)).clip(-10, 10)
        is_month_end = month != np.roll(month, -1)
        w_reb = w.where(pd.Series(is_month_end, index=w.index), np.nan)
        w_held = w_reb.shift(1).ffill()
        n_active = w_held.notna().sum(axis=1).replace(0, np.nan)
        port = ((w_held * rets_nc).sum(axis=1) / n_active
                - (w_held.diff().abs().sum(axis=1) / n_active) * COST_BP / 10000.0)
        print(report(f"{lb_name} net", port.dropna()))


if __name__ == "__main__":
    run_candidate_b()
    print()
    run_candidate_c()
    sys.exit(0)
