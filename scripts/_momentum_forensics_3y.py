"""Forensics momentum-сигналов на 1h-истории 730d (research-only).

Скиллы trade-forensics + quant-math: что отличает выигрышные сделки от
убыточных среди признаков, известных в момент входа (leak-safe), с
разделением IS/OOS. Все признаки ищутся только на IS (первая половина окна);
границы терцилей фиксируются на IS и переносятся на OOS без изменений.

Сделки: входы базового сигнала без фильтров (edge-trigger флипа
24h-momentum через ±0.15%), выход — текущая live-механика
(BE@1R, partial 0.5@1.5R, trail 1.5ATR, гистерезис decay на −threshold,
friday-flat 20:00 UTC). Фильтры live (session, NY-block, ADX≥20)
проверяются здесь как признаки, а не применяются.

Признаки на close бара входа:
  side, symbol, hour, weekday, adx, z24 = (close−close[−24])/(ATR·√24),
  atr_rank = перцентиль ATR% в собственной истории символа за 500 баров
  (нормировка внутри символа — JPY не смешивается с остальными),
  ema_dist = (close−EMA200)/ATR, d1_agree = знак 120-барного momentum
  совпадает со стороной, range_pos = положение close в 24h high-low.
Дополнительно: MFE/MAE (R) по экстремумам 1h-баров, форвардный ход сигнала
в ATR на горизонтах 1..48 баров.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu, ttest_ind

sys.path.insert(0, "scripts")
from _momentum_canon_validation import (  # noqa: E402
    ATR_PERIOD, ATR_STOP_MULT, BE_R, LOOKBACK, PARTIAL_R, SYMBOLS, THRESHOLD,
    TRAIL_ATR, TRAIL_R, atr_series, load_1h,
)
from _momentum_current_config_validation import adx_series  # noqa: E402

COST_R = 0.036
PARTIAL_FRAC = 0.5
HORIZONS = (1, 3, 6, 12, 24, 48)


def features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["Close"]
    atr = atr_series(df, ATR_PERIOD)
    f = pd.DataFrame(index=df.index)
    f["atr"] = atr
    f["mom"] = c / c.shift(LOOKBACK) - 1.0
    f["adx"] = adx_series(df)
    f["z24"] = (c - c.shift(LOOKBACK)) / (atr * np.sqrt(LOOKBACK))
    atr_pct = atr / c
    f["atr_rank"] = atr_pct.rolling(500, min_periods=200).rank(pct=True)
    f["ema_dist"] = (c - c.ewm(span=200, adjust=False).mean()) / atr
    f["mom120"] = c / c.shift(120) - 1.0
    hi24 = df["High"].rolling(24).max()
    lo24 = df["Low"].rolling(24).min()
    f["range_pos"] = (c - lo24) / (hi24 - lo24)
    return f


def simulate(sym: str, df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    f = features(df)
    close = df["Close"].to_numpy()
    high = df["High"].to_numpy()
    low = df["Low"].to_numpy()
    idx = df.index
    atr = f["atr"].to_numpy()
    mom = f["mom"].to_numpy()

    trades: list[dict] = []
    signals: list[dict] = []
    last_dir, pos = "flat", None

    for i in range(len(df)):
        m, a = mom[i], atr[i]
        if np.isnan(m) or np.isnan(a) or a <= 0:
            continue
        cur = "long" if m > THRESHOLD else "short" if m < -THRESHOLD else "flat"
        hi, lo, c = high[i], low[i], close[i]
        ts = idx[i]

        if pos is not None:
            lg = pos["side"] == "long"
            fav = (hi - pos["entry"]) if lg else (pos["entry"] - lo)
            adv = (pos["entry"] - lo) if lg else (hi - pos["entry"])
            pos["mfe"] = max(pos["mfe"], fav / pos["risk"])
            pos["mae"] = max(pos["mae"], adv / pos["risk"])
            exit_px, kind = None, None
            if (lg and lo <= pos["sl"]) or (not lg and hi >= pos["sl"]):
                exit_px, kind = pos["sl"], ("sl" if not pos["be"] else "be_trail")
            else:
                r_now = fav / pos["risk"]
                if not pos["partial"] and r_now >= PARTIAL_R:
                    pos["realized"] += PARTIAL_FRAC * PARTIAL_R
                    pos["size"] -= PARTIAL_FRAC
                    pos["partial"] = True
                if not pos["be"] and r_now >= BE_R:
                    pos["sl"] = max(pos["sl"], pos["entry"]) if lg else min(pos["sl"], pos["entry"])
                    pos["be"] = True
                if r_now >= TRAIL_R:
                    pos["sl"] = max(pos["sl"], c - TRAIL_ATR * a) if lg else min(pos["sl"], c + TRAIL_ATR * a)
                if (lg and m < -THRESHOLD) or (not lg and m > THRESHOLD):
                    exit_px, kind = c, "decay"
                elif ts.weekday() == 4 and ts.hour == 19:
                    exit_px, kind = c, "friday"
            if exit_px is not None:
                r = ((exit_px - pos["entry"]) if lg else (pos["entry"] - exit_px)) / pos["risk"]
                pos["R"] = pos["realized"] + pos["size"] * r
                pos["exit"] = kind
                pos["hold"] = i - pos["i"]
                trades.append(pos)
                pos = None

        if cur in ("long", "short") and cur != last_dir:
            sgn = 1 if cur == "long" else -1
            row = {"t": ts, "sym": sym, "side": cur}
            for h in HORIZONS:
                row[f"fwd{h}"] = (sgn * (close[i + h] - c) / a) if i + h < len(df) else np.nan
            signals.append(row)
            if pos is None and not (ts.weekday() == 4 and ts.hour >= 19):
                risk = a * ATR_STOP_MULT
                fr = f.iloc[i]
                pos = {"t": ts, "i": i, "sym": sym, "side": cur, "entry": c, "risk": risk,
                       "sl": c - sgn * risk, "size": 1.0, "realized": 0.0,
                       "be": False, "partial": False, "mfe": 0.0, "mae": 0.0,
                       "hour": ts.hour, "weekday": ts.weekday(), "adx": fr["adx"],
                       "z24": abs(fr["z24"]), "atr_rank": fr["atr_rank"],
                       "ema_dist_signed": sgn * fr["ema_dist"],
                       "d1_agree": int(np.sign(fr["mom120"]) == sgn),
                       "range_pos_signed": fr["range_pos"] if sgn > 0 else 1 - fr["range_pos"]}
        last_dir = cur
    return trades, signals


def tstat(x: np.ndarray) -> float:
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 else np.nan


def show(label: str, x: pd.Series) -> str:
    x = x.dropna().to_numpy()
    if len(x) == 0:
        return f"  {label:<26} n=0"
    return (f"  {label:<26} n={len(x):>4} avgR={x.mean():+.3f} t={tstat(x):+5.2f} "
            f"WR={(x > 0).mean():.0%}")


def main() -> int:
    all_tr, all_sig = [], []
    for s in SYMBOLS:
        df = load_1h(s)
        if df is None:
            continue
        tr, sg = simulate(s, df)
        all_tr += tr
        all_sig += sg
    T = pd.DataFrame(all_tr)
    S = pd.DataFrame(all_sig)
    T["Rnet"] = T["R"] - COST_R
    t0, t1 = T["t"].min(), T["t"].max()
    mid = t0 + (t1 - t0) / 2
    T["oos"] = T["t"] >= mid
    S["oos"] = S["t"] >= mid
    IS, OOS = T[~T.oos], T[T.oos]
    print(f"Окно {t0} → {t1}, split {mid.date()}, сделок {len(T)} (IS {len(IS)} / OOS {len(OOS)})\n")

    print("=== 1. РАЗЛОЖЕНИЕ (R, 1R = 2.5 ATR)")
    print(show("gross ALL", T["R"]))
    print(show(f"net ALL (−{COST_R}R)", T["Rnet"]))
    w, l = T[T.R > 0].R, T[T.R <= 0].R
    be = -l.mean() / (w.mean() - l.mean())
    be_c = (COST_R - l.mean()) / (w.mean() - l.mean())
    print(f"  avg win {w.mean():+.2f}R  avg loss {l.mean():+.2f}R  WR {len(w) / len(T):.1%}  "
          f"BE-WR gross {be:.1%} / с издержками {be_c:.1%}")
    print(T.groupby("exit")["R"].agg(["count", "sum", "mean"]).round(3).to_string())

    print("\n=== 2. MFE/MAE: сигнал или управление")
    for thr in (0.5, 1.0, 1.5, 2.0):
        print(f"  доля сделок с MFE ≥ {thr}R: {(T.mfe >= thr).mean():.1%}")
    sl = T[T.exit == "sl"]
    print(f"  полных стопов {len(sl)}: MFE медиана {sl.mfe.median():.2f}R, "
          f"доля с MFE ≥ 0.5R {(sl.mfe >= 0.5).mean():.1%}, ≥ 1R {(sl.mfe >= 1).mean():.1%}")
    dec = T[T.exit == "decay"]
    print(f"  decay-выходов {len(dec)}: avgR {dec.R.mean():+.3f}, MFE медиана {dec.mfe.median():.2f}R, "
          f"MAE медиана {dec.mae.median():.2f}R")

    print("\n=== 3. ФОРВАРДНЫЙ ХОД СИГНАЛА (ATR, знак по стороне), IS | OOS")
    for h in HORIZONS:
        a, b = S.loc[~S.oos, f"fwd{h}"].dropna(), S.loc[S.oos, f"fwd{h}"].dropna()
        print(f"  +{h:>2} баров: IS {a.mean():+.3f} (t={tstat(a.to_numpy()):+.2f}, n={len(a)}) | "
              f"OOS {b.mean():+.3f} (t={tstat(b.to_numpy()):+.2f}, n={len(b)})")

    print("\n=== 4. ПРИЗНАКИ: терцили по IS → те же границы на OOS (gross R)")
    num = ["adx", "z24", "atr_rank", "ema_dist_signed", "range_pos_signed"]
    n_tests = 0
    for col in num:
        q = IS[col].quantile([1 / 3, 2 / 3]).to_numpy()
        bins = [-np.inf, q[0], q[1], np.inf]
        print(f"  --- {col}  границы IS: {q[0]:.2f} / {q[1]:.2f}")
        for part, d in (("IS", IS), ("OOS", OOS)):
            g = pd.cut(d[col], bins, labels=["low", "mid", "high"])
            cells = " | ".join(f"{k} {d.R[g == k].mean():+.3f} (n={int((g == k).sum())})"
                               for k in ("low", "mid", "high"))
            hi_r, lo_r = d.R[g == "high"], d.R[g == "low"]
            p = ttest_ind(hi_r, lo_r, equal_var=False).pvalue
            print(f"    {part:<3} {cells}   high−low p={p:.3f}")
        n_tests += 1
    cats = {"side": "side", "sym": "sym", "d1_agree": "d1_agree", "weekday": "weekday"}
    T["session"] = pd.cut(T.hour, [-1, 6, 9, 13, 16, 20, 23],
                          labels=["00-07", "07-10", "10-14", "14-17", "17-21", "21-24"])
    IS, OOS = T[~T.oos], T[T.oos]
    cats["session"] = "session"
    for col in cats:
        print(f"  --- {col}")
        for k in sorted(T[col].dropna().unique(), key=str):
            a, b = IS.R[IS[col] == k], OOS.R[OOS[col] == k]
            print(f"    {str(k):<8} IS {a.mean():+.3f} (n={len(a)}, t={tstat(a.to_numpy()):+.2f}) | "
                  f"OOS {b.mean():+.3f} (n={len(b)}, t={tstat(b.to_numpy()):+.2f})")
        n_tests += 1
    print(f"\n  Проверено признаков: {n_tests}. Поправка Бонферрони: значимо при p < {0.05 / n_tests:.4f}")

    print("\n=== 5. WIN vs LOSS (Mann-Whitney), IS | OOS")
    for col in num:
        res = []
        for d in (IS, OOS):
            x, y = d.loc[d.R > 0, col].dropna(), d.loc[d.R <= 0, col].dropna()
            res.append(f"win {x.median():.2f} loss {y.median():.2f} p={mannwhitneyu(x, y).pvalue:.3f}")
        print(f"  {col:<18} IS: {res[0]} | OOS: {res[1]}")

    T.drop(columns=["i"]).to_csv("/tmp/mom_3y_trades.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
