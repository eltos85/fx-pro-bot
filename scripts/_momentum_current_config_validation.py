"""2-летняя проверка ТЕКУЩЕЙ конфигурации momentum (research-only).

Контекст: BUILDLOG 2026-07-10 — реплика базовой стратегии на 730d 1h дала
+0.02R gross / −135R net (edge ≈ 0). BUILDLOG 2026-07-24 добавил фильтры,
подобранные на 34 live-сделках (ADX≥20, NY-open block 14-16, exit-гистерезис
на −threshold) — out-of-sample на длинной истории они не проверялись.
Здесь: тот же движок, что scripts/_momentum_canon_validation.py, плюс
live-фильтры. Абляции — только уже задеплоенных компонентов (не поиск новых
порогов). Два parameter-free варианта: запрет входов в пятницу целиком и
фактическая доля partial 0.4 (floor по step объёма).

Семантика часов = live: signal_hour = час открытия закрытого 1h-бара
(индекс yfinance), вход по close бара. Session [7,21), NY-block {14,15,16}.
Friday: вход запрещён для баров, закрывающихся ≥20:00 UTC (индекс ≥19);
flat по close бара с индексом 19 (закрытие 20:00).
Не моделируется: news-close (нет истории календаря), лимит 3 позиции.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from _momentum_canon_validation import (  # noqa: E402
    ATR_PERIOD, ATR_STOP_MULT, BE_R, LOOKBACK, PARTIAL_R, SYMBOLS, THRESHOLD,
    TRAIL_ATR, TRAIL_R, atr_series, load_1h,
)

ADX_PERIOD = 14
COSTS = (0.0, 0.036, 0.06)  # gross; live 08-26→09-25 комиссия+своп; замер 07-10

FULL = dict(hyst=True, session=True, ny=True, adx=True, fri=True,
            fri_noentry=False, partial=0.5)
VARIANTS = {
    "base_july": dict(hyst=False, session=False, ny=False, adx=False, fri=False,
                      fri_noentry=False, partial=0.5),
    "current": FULL,
    "-hyst": {**FULL, "hyst": False},
    "-session": {**FULL, "session": False},
    "-ny_block": {**FULL, "ny": False},
    "-adx": {**FULL, "adx": False},
    "-friday": {**FULL, "fri": False},
    "fri_noentry": {**FULL, "fri_noentry": True},
    "partial0.4": {**FULL, "partial": 0.4},
}


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def adx_series(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = wilder(tr, ADX_PERIOD)
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    pdi = 100 * wilder(pdm, ADX_PERIOD) / atr
    mdi = 100 * wilder(mdm, ADX_PERIOD) / atr
    s = pdi + mdi
    dx = 100 * (pdi - mdi).abs() / s.where(s > 0)
    return wilder(dx, ADX_PERIOD)


def backtest(df: pd.DataFrame, v: dict) -> list[tuple[pd.Timestamp, float, str]]:
    close = df["Close"]
    atr = atr_series(df, ATR_PERIOD)
    adx = adx_series(df)
    mom = close / close.shift(LOOKBACK) - 1.0
    exit_thr = THRESHOLD if v["hyst"] else 0.0
    pf = v["partial"]

    out: list[tuple[pd.Timestamp, float, str]] = []
    last_dir = "flat"
    pos = None

    def close_at(price: float, kind: str) -> None:
        nonlocal pos
        r = ((price - pos["entry"]) if pos["side"] == "long"
             else (pos["entry"] - price)) / pos["risk"]
        out.append((pos["t"], pos["realizedR"] + pos["size"] * r, kind))
        pos = None

    for i in range(len(df)):
        m, a = mom.iloc[i], atr.iloc[i]
        if np.isnan(m) or np.isnan(a) or a <= 0:
            continue
        cur_dir = "long" if m > THRESHOLD else "short" if m < -THRESHOLD else "flat"
        hi, lo, c = float(df["High"].iloc[i]), float(df["Low"].iloc[i]), float(close.iloc[i])
        ts = df.index[i]
        is_fri, hour = ts.weekday() == 4, ts.hour

        if pos is not None:
            long = pos["side"] == "long"
            if (long and lo <= pos["sl"]) or (not long and hi >= pos["sl"]):
                close_at(pos["sl"], "sl" if not pos["be"] else "be_trail")
            else:
                r_now = ((hi - pos["entry"]) if long else (pos["entry"] - lo)) / pos["risk"]
                if not pos["partial"] and r_now >= PARTIAL_R:
                    pos["realizedR"] += pf * PARTIAL_R
                    pos["size"] -= pf
                    pos["partial"] = True
                if not pos["be"] and r_now >= BE_R:
                    pos["sl"] = max(pos["sl"], pos["entry"]) if long else min(pos["sl"], pos["entry"])
                    pos["be"] = True
                if r_now >= TRAIL_R:
                    pos["sl"] = (max(pos["sl"], c - TRAIL_ATR * a) if long
                                 else min(pos["sl"], c + TRAIL_ATR * a))

        if pos is not None:
            long = pos["side"] == "long"
            if (long and m < -exit_thr) or (not long and m > exit_thr):
                close_at(c, "decay")
            elif v["fri"] and is_fri and hour == 19:
                close_at(c, "friday")

        blocked = False
        if v["session"] and not (7 <= hour < 21):
            blocked = True
        if v["ny"] and hour in (14, 15, 16):
            blocked = True
        if v["fri"] and is_fri and hour >= 19:
            blocked = True
        if v["fri_noentry"] and is_fri:
            blocked = True
        if v["adx"] and (np.isnan(adx.iloc[i]) or adx.iloc[i] < 20.0):
            blocked = True

        if (pos is None and not blocked and cur_dir in ("long", "short")
                and cur_dir != last_dir):
            risk = a * ATR_STOP_MULT
            pos = {"t": ts, "entry": c, "side": cur_dir, "risk": risk,
                   "sl": c - risk if cur_dir == "long" else c + risk,
                   "size": 1.0, "realizedR": 0.0, "be": False, "partial": False}
        last_dir = cur_dir
    return out


def line(label: str, rs: np.ndarray, cost: float) -> str:
    if len(rs) == 0:
        return f"  {label:<12} n=0"
    a = rs - cost
    w, l = a[a > 0], a[a < 0]
    pf = w.sum() / -l.sum() if l.sum() < 0 else float("inf")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return (f"  {label:<12} n={len(a):>4} netR={a.sum():+8.1f} avgR={a.mean():+.3f}"
            f" ±{1.96 * se:.3f} WR={len(w) / len(a) * 100:>3.0f}% PF={pf:.2f}")


def main() -> int:
    data = {s: d for s in SYMBOLS if (d := load_1h(s)) is not None and len(d) > 5000}
    if not data:
        return 1
    any_df = next(iter(data.values()))
    t0, t1 = any_df.index[0], any_df.index[-1]
    mid = t0 + (t1 - t0) / 2
    print(f"Окно: {t0} → {t1} | IS/OOS split {mid.date()} | пары: {list(data)}\n")

    for name, v in VARIANTS.items():
        tr = []
        for sym, df in data.items():
            tr += [(t, r, k, sym) for t, r, k in backtest(df, v)]
        rs = np.array([r for _, r, _, _ in tr])
        is_rs = np.array([r for t, r, _, _ in tr if t < mid])
        oos_rs = np.array([r for t, r, _, _ in tr if t >= mid])
        print(f"=== {name}")
        for cost in COSTS:
            print(line(f"ALL c={cost}", rs, cost))
        print(line("IS c=0.036", is_rs, 0.036))
        print(line("OOS c=0.036", oos_rs, 0.036))
        if name == "current":
            by_q: dict[str, list[float]] = {}
            by_k: dict[str, list[float]] = {}
            by_s: dict[str, list[float]] = {}
            for t, r, k, s in tr:
                by_q.setdefault(f"{t.year}-Q{(t.month - 1) // 3 + 1}", []).append(r)
                by_k.setdefault(k, []).append(r)
                by_s.setdefault(s, []).append(r)
            for grp in (by_q, by_k, by_s):
                for key in sorted(grp):
                    print(line(key, np.array(grp[key]), 0.036))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
