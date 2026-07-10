"""Матчинг live-сделок к реплике: ГДЕ теряется разрыв −19.4R vs +35R.

Продолжение _momentum_live_vs_backtest.py (BUILDLOG 2026-07-10). Реплика на
live-окне зарабатывает, live теряет → implementation gap. Здесь: пер-сделочное
сравнение. Реплика расширена: возвращает (t, R, side, entry, exit_reason).

Матчинг: live-сделка ↔ реплика-сделка того же символа и стороны, |Δt входа|
≤ 2ч (live входит на 5-мин цикле после закрытия 1h бара; реплика — на 5m
баре флипа). Категории:
  matched   — вход есть у обоих: сравниваем R (исполнение/выходы);
  live_only — live вошёл, реплика нет (лишние входы: рестарты, дребезг,
              направление после MARKET_CLOSED и т.п.);
  repl_only — реплика вошла, live нет (пропущенные входы: downtime, guard,
              бот лежал — напр. 07-04→07-06 token-каскад).
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from momentum_exit_backtest import (  # noqa: E402
    ATR_PERIOD, ATR_STOP_MULT, LOOKBACK, PARTIAL_FRAC, PARTIAL_R, SYMBOLS,
    THRESHOLD, TRAIL_ATR, TRAIL_R, atr, load, signal_dir, to_1h,
)

WINDOW_START = pd.Timestamp("2026-06-05", tz="UTC")
WINDOW_END = pd.Timestamp("2026-07-10 23:59", tz="UTC")
MATCH_TOL = pd.Timedelta(hours=2)

YF2CT = {"EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD",
         "USDJPY=X": "USDJPY", "AUDUSD=X": "AUDUSD"}


def backtest_symbol_ext(df5, df1h, break_even_r=1.0):
    """Копия backtest_symbol с расширенным выводом (side, entry, why)."""
    h1_close = df1h["Close"]
    h1_atr = atr(df1h, ATR_PERIOD)
    dirs, atrs = {}, {}
    closes = list(h1_close.index)
    for i, ts in enumerate(closes):
        dirs[ts] = signal_dir(h1_close.iloc[: i + 1])
        atrs[ts] = float(h1_atr.iloc[i]) if not np.isnan(h1_atr.iloc[i]) else 0.0

    h1_index = pd.DatetimeIndex(closes)
    trades = []
    last_direction = "flat"
    pos = None
    last_seen_h1 = None

    for ts, bar in df5.iterrows():
        loc = h1_index.searchsorted(ts, side="right") - 1
        if loc < 0:
            continue
        h1_ts = h1_index[loc]
        cur_dir = dirs[h1_ts]
        cur_atr = atrs[h1_ts]
        hi, lo, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])

        if pos is not None:
            entry, side, risk = pos["entry"], pos["side"], pos["risk"]
            if side == "long":
                if lo <= pos["sl"]:
                    r_exit = (pos["sl"] - entry) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit,
                                   side, entry, "sl/trail"))
                    pos = None
                else:
                    r_now = (hi - entry) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
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
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit,
                                   side, entry, "sl/trail"))
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

        if pos is not None and h1_ts != last_seen_h1:
            opp = "short" if pos["side"] == "long" else "long"
            if dirs[h1_ts] == opp:
                if pos["side"] == "long":
                    r_exit = (close - pos["entry"]) / pos["risk"]
                else:
                    r_exit = (pos["entry"] - close) / pos["risk"]
                trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit,
                               pos["side"], pos["entry"], "sign_decay"))
                pos = None

        if h1_ts != last_seen_h1:
            if pos is None and cur_dir in ("long", "short") \
                    and cur_dir != last_direction and cur_atr > 0:
                risk = cur_atr * ATR_STOP_MULT
                sl = close - risk if cur_dir == "long" else close + risk
                pos = {"t": ts, "entry": close, "side": cur_dir, "risk": risk,
                       "sl": sl, "size": 1.0, "realizedR": 0.0,
                       "be": False, "partial": False}
            last_direction = cur_dir
            last_seen_h1 = h1_ts

    return trades


def main() -> int:
    live = json.load(open("data/loss_audit_trades_0710.json"))
    live = [t for t in live if t.get("r") is not None]

    repl = []
    for sym in SYMBOLS:
        df5 = load(sym)
        if df5 is None:
            continue
        for (t, r, side, entry, why) in backtest_symbol_ext(df5, to_1h(df5)):
            if WINDOW_START <= t <= WINDOW_END:
                repl.append({"sym": YF2CT[sym], "t": t, "r": r,
                             "side": side, "why": why, "used": False})

    matched, live_only = [], []
    for lt in sorted(live, key=lambda x: x["ts_open"]):
        lt_ts = pd.Timestamp(lt["ts_open"], unit="s", tz="UTC")
        best, best_dt = None, MATCH_TOL
        for rt in repl:
            if rt["used"] or rt["sym"] != lt["symbol"] or rt["side"] != lt["side"]:
                continue
            dt = abs(rt["t"] - lt_ts)
            if dt <= best_dt:
                best, best_dt = rt, dt
        if best is not None:
            best["used"] = True
            matched.append((lt, best))
        else:
            live_only.append(lt)
    repl_only = [rt for rt in repl if not rt["used"]]

    lr = [lt["r"] for lt, _ in matched]
    rr = [rt["r"] for _, rt in matched]
    print(f"=== МАТЧИНГ live({len(live)}) ↔ реплика({len(repl)}), tol=2h ===\n")
    print(f"matched   : {len(matched):>3}  liveR={sum(lr):+7.2f}  replR={sum(rr):+7.2f}  "
          f"gap={sum(lr)-sum(rr):+7.2f}")
    lo_r = [t["r"] for t in live_only]
    print(f"live_only : {len(live_only):>3}  liveR={sum(lo_r):+7.2f}   (лишние входы live)")
    ro_r = [t["r"] for t in repl_only]
    print(f"repl_only : {len(repl_only):>3}  replR={sum(ro_r):+7.2f}   (пропущенные live входы)")

    print("\n── matched: топ-12 по |gap| (live хуже реплики) ──")
    rows = sorted(matched, key=lambda p: p[0]["r"] - p[1]["r"])[:12]
    print(f"{'sym':<8}{'side':<6}{'live_open(UTC)':<15}{'liveR':>7}{'replR':>7}"
          f"{'gap':>7}  {'live_exit':<12}{'repl_exit'}")
    for lt, rt in rows:
        ts = datetime.fromtimestamp(lt["ts_open"], tz=UTC).strftime("%m-%d %H:%M")
        print(f"{lt['symbol']:<8}{lt['side']:<6}{ts:<15}{lt['r']:>+7.2f}{rt['r']:>+7.2f}"
              f"{lt['r']-rt['r']:>+7.2f}  {lt['exit_kind']:<12}{rt['why']}")

    print("\n── live_only: все (входы, которых нет у реплики) ──")
    print(f"{'sym':<8}{'side':<6}{'open(UTC)':<15}{'R':>7}  {'exit':<12}")
    for t in sorted(live_only, key=lambda x: x["ts_open"]):
        ts = datetime.fromtimestamp(t["ts_open"], tz=UTC).strftime("%m-%d %H:%M")
        print(f"{t['symbol']:<8}{t['side']:<6}{ts:<15}{t['r']:>+7.2f}  {t['exit_kind']:<12}")

    print("\n── repl_only: все (реплика вошла, live нет) ──")
    for t in sorted(repl_only, key=lambda x: x["t"]):
        print(f"{t['sym']:<8}{t['side']:<6}{t['t'].strftime('%m-%d %H:%M'):<15}"
              f"{t['r']:>+7.2f}  {t['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
