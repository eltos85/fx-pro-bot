"""Path-based контрфакт: что дал бы ранний безубыток (BE) на РЕАЛЬНЫХ путях сделок.

Read-only артефакт (no-data-fitting). Берёт реальные momentum-сделки + их 5m-путь
(yfinance) и применяет правило: "как только плавающая прибыль >= TRIG$, переносим
стоп в безубыток (entry); если цена вернулась к entry — выходим в ~$0 (минус costs);
иначе сделка доигрывается как реально (actual net)".

Так напрямую видно: сколько лоссов спаслось бы (give-back → scratch) и сколько
ВИНОВ срезалось бы (откатили к entry после буфера, но реально потом доросли).

$/пункт калибруется из gross сделки. Путь и уровни меряются в координатах yfinance
(basis уходит). Не предсказываем разворот — только реагируем стопом после буфера.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

FXAI_GOLD_PID = 151663957
YF = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "XAUUSD": "GC=F",
}


def load_bars() -> dict:
    out = {}
    for sym, yfsym in YF.items():
        df = yf.download(yfsym, period="7d", interval="5m", progress=False, auto_adjust=False)
        if df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index, utc=True)
        out[sym] = df[["High", "Low", "Close"]]
    return out


def simulate(t: dict, df: pd.DataFrame, trig_usd: float):
    """Возврат (cf_net, outcome) где outcome in {'BE_scratch','as_is'}."""
    o = pd.Timestamp(t["open_ts_ms"], unit="ms", tz="UTC")
    c = pd.Timestamp(t["close_ts_ms"], unit="ms", tz="UTC")
    win = df[(df.index >= o) & (df.index <= c)]
    if win.empty:
        return None, None
    entry = float(win["Close"].iloc[0])
    move = t["exit_price"] - t["entry_price"]
    if abs(move) < 1e-9:
        return None, None
    mult = abs(t["gross"]) / abs(move)
    arm_dist = trig_usd / mult
    be_cost = abs(t["gross"] - t["net"])  # swap+commission реальной сделки
    armed = False
    for _, bar in win.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        if t["side"] == "BUY":
            if hi - entry >= arm_dist:
                armed = True
            if armed and lo <= entry:
                return -be_cost, "BE_scratch"
        else:
            if entry - lo >= arm_dist:
                armed = True
            if armed and hi >= entry:
                return -be_cost, "BE_scratch"
    return t["net"], "as_is"


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/_momentum_trades_0611.json"
    trades = [t for t in json.loads(Path(path).read_text())
              if t["is_momentum"] and t["pid"] != FXAI_GOLD_PID
              and t["open_ts_ms"] != t["close_ts_ms"]]
    bars = load_bars()

    for trig in (5.0, 6.0):
        print(f"\n===== BE-trigger = +${trig:.0f} (стоп в безубыток) =====")
        actual_tot = cf_tot = 0.0
        rescued = damaged = scratched = 0
        print(f"{'pid':<11}{'sym':<8}{'side':<5}{'actual':>8}{'CF':>8}  outcome")
        rows = []
        for t in trades:
            if t["symbol"] not in bars:
                continue
            cf, outcome = simulate(t, bars[t["symbol"]], trig)
            if cf is None:
                continue
            rows.append((t, cf, outcome))
        rows.sort(key=lambda r: r[0]["net"])
        for t, cf, outcome in rows:
            actual_tot += t["net"]
            cf_tot += cf
            tag = ""
            if outcome == "BE_scratch":
                scratched += 1
                if t["net"] < 0:
                    rescued += 1
                    tag = "<- лосс спасён"
                else:
                    damaged += 1
                    tag = "<- ВИН срезан"
            print(f"{t['pid']:<11}{t['symbol']:<8}{t['side']:<5}{t['net']:>8.2f}{cf:>8.2f}  {outcome} {tag}")
        print("-" * 56)
        print(f"ИТОГ net: actual ${actual_tot:+.2f}  →  CF ${cf_tot:+.2f}  (Δ ${cf_tot-actual_tot:+.2f})")
        print(f"BE-scratch: {scratched} (лоссов спасено {rescued}, винов срезано {damaged}); "
              f"доиграно as-is: {len(rows)-scratched}")


if __name__ == "__main__":
    main()
