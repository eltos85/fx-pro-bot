"""Локальный MFE-анализ momentum-сделок (read-only, артефакт для no-data-fitting).

Проверяет гипотезу пользователя: добегали ли УБЫТОЧНЫЕ сделки до заметной
плавающей прибыли (+$5-6 / +0.5R / +1R) перед разворотом в минус.

$-множитель ($/ценовой пункт) калибруется из самой сделки: mult=|gross|/|exit-entry|.
Экскурсия меряется ВНУТРИ ряда yfinance (от цены на момент входа) — это убирает
basis между yfinance (GC=F, FX mid) и брокером.

1R ≈ $15 (риск-сайзинг momentum). +$5-6 ≈ 0.4R.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

R_USD = 15.0  # риск на сделку (MOMENTUM_BOT_RISK_PER_TRADE_USD)
FXAI_GOLD_PID = 151663957  # золото fx-ai-trader, не momentum

YF = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "XAUUSD": "GC=F",
}


def load_bars() -> dict[str, pd.DataFrame]:
    out = {}
    for sym, yfsym in YF.items():
        df = yf.download(yfsym, period="7d", interval="5m", progress=False, auto_adjust=False)
        if df.empty:
            print(f"WARN no bars for {sym}", file=sys.stderr)
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index, utc=True)
        out[sym] = df[["High", "Low", "Close"]]
    return out


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "data/_momentum_trades_0611.json"
    trades = json.loads(Path(path).read_text())
    bars = load_bars()

    rows = []
    for t in trades:
        if t["pid"] == FXAI_GOLD_PID or not t["is_momentum"]:
            continue
        if t["open_ts_ms"] == t["close_ts_ms"]:
            continue  # открыта до окна / нет интрабар-данных
        sym = t["symbol"]
        if sym not in bars:
            continue
        df = bars[sym]
        o = pd.Timestamp(t["open_ts_ms"], unit="ms", tz="UTC")
        c = pd.Timestamp(t["close_ts_ms"], unit="ms", tz="UTC")
        win = df[(df.index >= o) & (df.index <= c)]
        if win.empty:
            continue
        entry_ref = float(win["Close"].iloc[0])  # цена yfinance на момент входа
        move = t["exit_price"] - t["entry_price"]
        if abs(move) < 1e-9:
            continue
        mult = abs(t["gross"]) / abs(move)  # $/ценовой пункт
        if t["side"] == "BUY":
            fav_price = float(win["High"].max()) - entry_ref
        else:
            fav_price = entry_ref - float(win["Low"].min())
        mfe_usd = max(mult * fav_price, 0.0)
        rows.append(
            {
                "pid": t["pid"],
                "sym": sym,
                "side": t["side"],
                "net": t["net"],
                "mfe_usd": round(mfe_usd, 2),
                "mfe_R": round(mfe_usd / R_USD, 2),
            }
        )

    rows.sort(key=lambda r: r["net"])
    print(f"{'pid':<11}{'sym':<8}{'side':<5}{'net$':>8}{'MFE$':>8}{'MFE_R':>7}  reached")
    print("-" * 60)
    for r in rows:
        flags = []
        if r["mfe_usd"] >= 5:
            flags.append("+$5")
        if r["mfe_usd"] >= 6:
            flags.append("+$6")
        if r["mfe_R"] >= 0.5:
            flags.append("0.5R")
        if r["mfe_R"] >= 1.0:
            flags.append("1R")
        print(f"{r['pid']:<11}{r['sym']:<8}{r['side']:<5}{r['net']:>8.2f}"
              f"{r['mfe_usd']:>8.2f}{r['mfe_R']:>7.2f}  {' '.join(flags)}")

    losers = [r for r in rows if r["net"] < 0]
    winners = [r for r in rows if r["net"] >= 0]
    print("\n=== ИТОГ ===")
    print(f"всего сделок с MFE-данными: {len(rows)} (винов {len(winners)}, лоссов {len(losers)})")
    for thr in (5, 6):
        n = sum(1 for r in losers if r["mfe_usd"] >= thr)
        print(f"лоссов, добежавших до +${thr}: {n}/{len(losers)}"
              f"  (сумма их net = ${sum(r['net'] for r in losers if r['mfe_usd']>=thr):+.2f})")
    n05 = sum(1 for r in losers if r["mfe_R"] >= 0.5)
    n1 = sum(1 for r in losers if r["mfe_R"] >= 1.0)
    print(f"лоссов с MFE>=0.5R: {n05}/{len(losers)};  с MFE>=1.0R (дошли бы до BE): {n1}/{len(losers)}")
    if losers:
        avg_mfe_losers = sum(r["mfe_usd"] for r in losers) / len(losers)
        print(f"средний MFE убыточной сделки: ${avg_mfe_losers:.2f} ({avg_mfe_losers/R_USD:.2f}R)")


if __name__ == "__main__":
    main()
