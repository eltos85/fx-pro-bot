"""Контрфакт двух рычагов выхода на реальных путях сделок (read-only).

Отвечает на вопрос: ранний BE режет ли КРУПНЫЕ лузы (или только give-back'и), и
что даёт более ТЕСНЫЙ стоп (он-то и режет straight-down лузы), и комбо.

Для каждой реальной сделки идём по 5m-пути во времени, первым сработавшим
правилом и закрываем:
  - BE@buf: как только плавающий +buf$ → стоп в entry; возврат к entry → ~$0.
  - tight stop -S$: как только плавающий -S$ → выход -S$ (тестируем тесный стоп).
  - иначе сделка доигрывается реально (actual net).
$/пункт калибруется из gross; уровни в координатах yfinance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

FXAI_GOLD_PID = 151663957
YF = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
      "AUDUSD": "AUDUSD=X", "XAUUSD": "GC=F"}


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


def sim(t, df, be_buf, tight_stop):
    """be_buf: $ для переноса в BE (0=выкл). tight_stop: $ тесного стопа (0=выкл)."""
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
    be_cost = abs(t["gross"] - t["net"])
    arm_dist = be_buf / mult if be_buf else None
    stop_dist = tight_stop / mult if tight_stop else None
    armed = False
    for _, bar in win.iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        if t["side"] == "BUY":
            fav, adv = hi - entry, entry - lo
            if stop_dist and adv >= stop_dist:
                return -tight_stop, "tight_stop"
            if arm_dist:
                if fav >= arm_dist:
                    armed = True
                if armed and lo <= entry:
                    return -be_cost, "BE"
        else:
            fav, adv = entry - lo, hi - entry
            if stop_dist and adv >= stop_dist:
                return -tight_stop, "tight_stop"
            if arm_dist:
                if fav >= arm_dist:
                    armed = True
                if armed and hi >= entry:
                    return -be_cost, "BE"
    return t["net"], "as_is"


def run(trades, bars, label, be_buf, tight_stop):
    actual = cf = 0.0
    rescued = win_cut = ts_n = 0
    big_loss_cut = 0  # лузы <= -$10, реально срезанные
    for t in trades:
        if t["symbol"] not in bars:
            continue
        v, outc = sim(t, bars[t["symbol"]], be_buf, tight_stop)
        if v is None:
            continue
        actual += t["net"]
        cf += v
        if outc in ("BE", "tight_stop") and v > t["net"]:
            if t["net"] < 0:
                rescued += 1
                if t["net"] <= -10:
                    big_loss_cut += 1
        if outc in ("BE", "tight_stop") and t["net"] > 0 and v < t["net"]:
            win_cut += 1
        if outc == "tight_stop":
            ts_n += 1
    print(f"{label:<34} actual ${actual:+7.2f} -> CF ${cf:+7.2f} (Δ ${cf-actual:+6.2f}) | "
          f"лоссов спасено {rescued} (из них крупных<=-$10: {big_loss_cut}), винов срезано {win_cut}, tight-stop сраб {ts_n}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/_momentum_trades_0611.json"
    trades = [t for t in json.loads(Path(path).read_text())
              if t["is_momentum"] and t["pid"] != FXAI_GOLD_PID
              and t["open_ts_ms"] != t["close_ts_ms"]]
    bars = load_bars()
    print("=== рычаги выхода (momentum, 23 сделки с путём, 06-11..16) ===")
    run(trades, bars, "BE@+$6 only", 6.0, 0.0)
    run(trades, bars, "tight stop -$10 only", 0.0, 10.0)
    run(trades, bars, "tight stop -$8 only", 0.0, 8.0)
    run(trades, bars, "BE@+$6 + tight stop -$10", 6.0, 10.0)
    run(trades, bars, "BE@+$6 + tight stop -$8", 6.0, 8.0)


if __name__ == "__main__":
    main()
