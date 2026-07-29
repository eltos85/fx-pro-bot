"""Research: single-asset daily TSMOM на меди (HG=F → cTrader COPPER).

Контекст (BUILDLOG 2026-07-10): pivot стратегии momentum-бота. После замера
min-лотов на cTrader (см. диалог) выяснилось, что под daily-hold без близкого
SL на $1000–$1719 лезет ОДИН инструмент — COPPER (min_notional $110). Все
FX/золото/нефть/индексы требуют 30–275x leverage на мин-лоте. Крипта (ETH $3k,
BTC $60k) отпала по решению пользователя (крипта — на Bybit).

Цель: проверить, есть ли у меди edge на daily TSMOM (Moskowitz/Ooi/Pedersen
2012; Hurst/Ooi/Pedersen 2017 — 3/6/12 combo), и какой vol-target вписывается
в риск-профиль пользователя (maxDD ≤ −40%, цель — положительное матожидание
на горизонте месяца/квартала, минус-месяцы допустимы).

Метрики:
  * annRet, Sharpe, maxDD — стандарт.
  * pct_positive_months — доля прибыльных месяцев (релевантно цели «ежемесячный
    доход»; TSMOM-канон ~55–60%).
  * p-value — t-test месячных net-returns vs 0 (H0: edge = 0).

Параметры канонические, БЕЗ подбора: lookback 3/6/12 (Hurst 2017),
vol-window 60d (стандарт TSMOM-реплик), cost 5bp/turnover (conservative для
ликвидных commodities; медь LME/COMEX — узкий спред). Vol-target варьируем
{40,30,25,20}% — это НЕ подгонка под результат, а выбор risk-budget под
целевой maxDD пользователя (линейное масштабирование risk/return, Sharpe
и p-value не меняются от target).

Только research: код бота не меняется.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats

SYMBOL = "HG=F"          # COMEX copper front-month → cTrader COPPER
LOOKBACKS = {"3m": 63, "6m": 126, "12m": 252}   # Hurst/Ooi/Pedersen 2017
VOL_WIN = 60             # окно оценки vol (дни)
COST_BP = 5.0            # bp на единицу turnover
TARGETS = [0.40, 0.30, 0.25, 0.20]   # vol-target (risk-budget, не подгонка)


def load() -> pd.DataFrame | None:
    df = yf.download(SYMBOL, period="10y", interval="1d", progress=False,
                     auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["Close"]].dropna()


def tsmom(close: pd.Series, lookback: int, target_vol: float) -> pd.Series:
    """Single-asset daily TSMOM, monthly rebalance, vol-scaled, net of cost."""
    rets = close.pct_change()
    vol = rets.ewm(span=VOL_WIN, min_periods=VOL_WIN).std() * np.sqrt(252)
    mom = close / close.shift(lookback) - 1.0
    sign = np.sign(mom)
    w = (sign * (target_vol / vol)).clip(-10, 10)
    month = rets.index.to_period("M")
    is_month_end = month != np.roll(month, -1)
    w_reb = w.where(pd.Series(is_month_end, index=w.index), np.nan)
    w_held = w_reb.shift(1).ffill()        # без look-ahead
    port = w_held * rets
    turnover = w_held.diff().abs()
    net = port - turnover * COST_BP / 10000.0
    return net.dropna()


def report(name: str, daily: pd.Series) -> dict:
    if daily.empty:
        return {"name": name}
    ann = daily.mean() * 252
    sh = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0
    eq = (1 + daily).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    # месячная агрегация: compaund дневных rets внутри месяца
    monthly = daily.resample("ME").apply(lambda x: (1.0 + x).prod() - 1.0).dropna()
    pos_m = (monthly > 0).mean() * 100
    t, pval = stats.ttest_1samp(monthly, 0.0)
    return {
        "name": name, "ann": ann * 100, "sharpe": sh, "maxdd": dd * 100,
        "pos_months": pos_m, "pval": pval, "n_months": len(monthly),
        "worst_month": monthly.min() * 100, "best_month": monthly.max() * 100,
    }


def main() -> None:
    df = load()
    if df is None or len(df) < 1000:
        print("Нет данных HG=F"); sys.exit(1)
    close = df["Close"]
    t0, t1 = close.index[0], close.index[-1]
    print("=" * 78)
    print(f"Copper (HG=F) daily TSMOM | {t0.date()} → {t1.date()} "
          f"({len(close)} дней) | cost {COST_BP}bp/turnover")
    print("=" * 78)

    # combo по lookback'ам для каждого target
    for tv in TARGETS:
        print(f"\n--- vol-target {tv*100:.0f}% ---")
        per_lb = {}
        for lb_name, lb in LOOKBACKS.items():
            s = tsmom(close, lb, tv)
            per_lb[lb_name] = s
            r = report(lb_name, s)
            print(f"  {lb_name:<6} ann={r['ann']:+6.2f}%  Sharpe={r['sharpe']:+.2f}  "
                  f"maxDD={r['maxdd']:.1f}%  posMonths={r['pos_months']:.0f}%  "
                  f"p={r['pval']:.3f}  n={r['n_months']}m  "
                  f"worst={r['worst_month']:.1f}%")
        # combo 3/6/12 (Hurst 2017) — средний daily-return
        combo = pd.concat(list(per_lb.values()), axis=1).mean(axis=1).dropna()
        r = report("combo", combo)
        print(f"  {'combo':<6} ann={r['ann']:+6.2f}%  Sharpe={r['sharpe']:+.2f}  "
              f"maxDD={r['maxdd']:.1f}%  posMonths={r['pos_months']:.0f}%  "
              f"p={r['pval']:.3f}  n={r['n_months']}m  "
              f"worst={r['worst_month']:.1f}%")

    # блок 2: OOS-проверка combo — последние 5 лет vs первые 5 (stability)
    print("\n--- combo stability: first 5y vs last 5y (target 25%) ---")
    c25 = pd.concat(
        [tsmom(close, lb, 0.25) for lb in LOOKBACKS.values()], axis=1
    ).mean(axis=1).dropna()
    mid = c25.index[0] + (c25.index[-1] - c25.index[0]) / 2
    for label, sl in (("first5y", c25[c25.index < mid]),
                      ("last5y", c25[c25.index >= mid])):
        r = report(label, sl)
        print(f"  {label:<8} ann={r['ann']:+6.2f}%  Sharpe={r['sharpe']:+.2f}  "
              f"maxDD={r['maxdd']:.1f}%  posMonths={r['pos_months']:.0f}%  "
              f"p={r['pval']:.3f}")


if __name__ == "__main__":
    main()
