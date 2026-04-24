#!/usr/bin/env python3
"""Backtest Oil ORB v2 — ТОЧНЫЙ клон gold_orb методологии, примененный к CL/BZ.

Изменения от v1 (ошибочной):
- M5 базовый (не M15)
- Box = 3 M5 bars = 15 мин
- London 08:00-08:15 formation → 08:15-12:00 trading
- NY 14:30-14:45 formation → 14:45-17:00 trading
- SL = 1.5 × ATR, TP = 3.0 × ATR (R:R = 2, как в gold_orb)
- **Touch-break**: bar.high > box_high (long), bar.low < box_low (short)
- 1 trade per session per day (без overlap)

Методология ГАРАНТИРОВАННО та же что у gold_orb, меняется только инструмент.
Это критично для научной чистоты: gold_orb работает → применяем ТОЖЕ САМОЕ к oil.

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_oil_orb_v2
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

# ─────────────────── параметры (копия gold_orb) ───────────────────

PRIMARY = "CL=F"
REPLICATION = "BZ=F"
INSTRUMENTS = [PRIMARY, REPLICATION]

# M5 base
LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)      # 3 M5 bars
LONDON_CLOSE = time(12, 0)

NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)         # 3 M5 bars
NY_CLOSE = time(17, 0)

ORB_BARS = 3                       # 3 M5 bars = 15 мин
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0                  # R:R = 2
ATR_PERIOD = 14
COST_PIPS = 3.5                    # FxPro Oil round-trip

IS_FRACTION = 0.7


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    if not path.exists():
        return np.array([])
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((
                int(r["timestamp"]) // 1000,
                float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]),
                float(r["volume"]),
            ))
    dt = np.dtype([
        ("ts", "i8"), ("open", "f8"), ("high", "f8"),
        ("low", "f8"), ("close", "f8"), ("volume", "f8"),
    ])
    return np.array(rows, dtype=dt)


def atr_series(arr: np.ndarray, period: int = ATR_PERIOD) -> np.ndarray:
    high = arr["high"]
    low = arr["low"]
    close = arr["close"]
    tr = np.zeros(len(arr))
    tr[0] = high[0] - low[0]
    for i in range(1, len(arr)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    out = np.zeros(len(arr))
    if len(arr) >= period:
        out[period - 1] = np.mean(tr[:period])
        for i in range(period, len(arr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def _utc_time(ts: int) -> time:
    return datetime.fromtimestamp(ts, UTC).time()


def _utc_date(ts: int):
    return datetime.fromtimestamp(ts, UTC).date()


def _in_range(t: time, start: time, end: time) -> bool:
    return start <= t < end


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    direction: int
    exit_ts: int
    exit_price: float
    exit_reason: str
    atr_at_entry: float
    net_pips: float
    session: str


def backtest_orb_session(
    arr: np.ndarray, sym: str,
    orb_start: time, orb_end: time, trade_end: time,
    session_name: str,
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
) -> list[Trade]:
    """Симулятор одной сессии ORB (touch-break, как в gold_orb)."""
    atr = atr_series(arr, ATR_PERIOD)
    ps = pip_size(sym)
    trades: list[Trade] = []

    dates = np.array([_utc_date(int(t)) for t in arr["ts"]])
    unique_dates = sorted(set(dates))

    for d in unique_dates:
        day_mask = dates == d
        day_idx = np.where(day_mask)[0]
        if len(day_idx) == 0:
            continue

        box_bars_idx = [
            i for i in day_idx
            if _in_range(_utc_time(int(arr["ts"][i])), orb_start, orb_end)
        ]
        if len(box_bars_idx) < ORB_BARS:
            continue
        box_high = float(np.max(arr["high"][box_bars_idx]))
        box_low = float(np.min(arr["low"][box_bars_idx]))

        trade_bars_idx = [
            i for i in day_idx
            if _in_range(_utc_time(int(arr["ts"][i])), orb_end, trade_end)
        ]
        if not trade_bars_idx:
            continue

        # Одна сделка за сессию — первый бар, где high/low пробил box
        in_trade = False
        direction = 0
        entry_price = 0.0
        sl_price = 0.0
        tp_price = 0.0
        entry_idx = 0
        entry_atr = 0.0

        for i in trade_bars_idx:
            h = float(arr["high"][i])
            l = float(arr["low"][i])
            c = float(arr["close"][i])
            a = float(atr[i])

            if not in_trade:
                if a <= 0:
                    continue
                # Touch-break
                if h > box_high:
                    in_trade = True
                    direction = 1
                    entry_price = box_high  # enter at touch = breakout level
                    sl_price = entry_price - sl_mult * a
                    tp_price = entry_price + tp_mult * a
                    entry_idx = i
                    entry_atr = a
                elif l < box_low:
                    in_trade = True
                    direction = -1
                    entry_price = box_low
                    sl_price = entry_price + sl_mult * a
                    tp_price = entry_price - tp_mult * a
                    entry_idx = i
                    entry_atr = a
                continue

            # В позиции — ищем TP/SL в том же или следующих барах
            if direction == 1:
                # SL сработал?
                if l <= sl_price:
                    net = (sl_price - entry_price) / ps - COST_PIPS
                    trades.append(Trade(
                        entry_ts=int(arr["ts"][entry_idx]),
                        entry_price=entry_price, direction=1,
                        exit_ts=int(arr["ts"][i]), exit_price=sl_price,
                        exit_reason="SL", atr_at_entry=entry_atr,
                        net_pips=net, session=session_name,
                    ))
                    break
                if h >= tp_price:
                    net = (tp_price - entry_price) / ps - COST_PIPS
                    trades.append(Trade(
                        entry_ts=int(arr["ts"][entry_idx]),
                        entry_price=entry_price, direction=1,
                        exit_ts=int(arr["ts"][i]), exit_price=tp_price,
                        exit_reason="TP", atr_at_entry=entry_atr,
                        net_pips=net, session=session_name,
                    ))
                    break
            else:
                if h >= sl_price:
                    net = (entry_price - sl_price) / ps - COST_PIPS
                    trades.append(Trade(
                        entry_ts=int(arr["ts"][entry_idx]),
                        entry_price=entry_price, direction=-1,
                        exit_ts=int(arr["ts"][i]), exit_price=sl_price,
                        exit_reason="SL", atr_at_entry=entry_atr,
                        net_pips=net, session=session_name,
                    ))
                    break
                if l <= tp_price:
                    net = (entry_price - tp_price) / ps - COST_PIPS
                    trades.append(Trade(
                        entry_ts=int(arr["ts"][entry_idx]),
                        entry_price=entry_price, direction=-1,
                        exit_ts=int(arr["ts"][i]), exit_price=tp_price,
                        exit_reason="TP", atr_at_entry=entry_atr,
                        net_pips=net, session=session_name,
                    ))
                    break
        else:
            # Цикл дошёл до конца session без TP/SL — закрыть по EOD close
            if in_trade and trade_bars_idx:
                last = trade_bars_idx[-1]
                close = float(arr["close"][last])
                net = (close - entry_price) * direction / ps - COST_PIPS
                trades.append(Trade(
                    entry_ts=int(arr["ts"][entry_idx]),
                    entry_price=entry_price, direction=direction,
                    exit_ts=int(arr["ts"][last]), exit_price=close,
                    exit_reason="EOD", atr_at_entry=entry_atr,
                    net_pips=net, session=session_name,
                ))

    return trades


def backtest_oil_orb_v2(arr: np.ndarray, sym: str) -> list[Trade]:
    london = backtest_orb_session(
        arr, sym, LONDON_OPEN, LONDON_ORB_END, LONDON_CLOSE, "LDN",
    )
    ny = backtest_orb_session(
        arr, sym, NY_OPEN, NY_ORB_END, NY_CLOSE, "NY",
    )
    all_trades = london + ny
    all_trades.sort(key=lambda t: t.entry_ts)
    return all_trades


def summarize(trades: list[Trade], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "total": 0, "mean": 0, "wr": 0,
                "p": 1.0, "avg_win": 0, "avg_loss": 0, "pf": 0,
                "long": 0, "short": 0, "t": 0,
                "lon_n": 0, "ny_n": 0, "lon_total": 0, "ny_total": 0}
    pips = np.array([t.net_pips for t in trades])
    wins = pips[pips > 0]
    losses = pips[pips <= 0]
    n_long = sum(1 for t in trades if t.direction == 1)
    n_short = sum(1 for t in trades if t.direction == -1)
    lon = [t for t in trades if t.session == "LDN"]
    ny = [t for t in trades if t.session == "NY"]
    pf = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and abs(losses.sum()) > 0 else float("inf")
    t_stat, p = stats.ttest_1samp(pips, 0.0) if len(pips) > 1 else (0, 1)
    return {
        "label": label,
        "n": len(trades),
        "total": float(pips.sum()),
        "mean": float(pips.mean()),
        "wr": float(len(wins) / len(trades)),
        "p": float(p),
        "t": float(t_stat),
        "avg_win": float(wins.mean()) if len(wins) > 0 else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) > 0 else 0.0,
        "pf": float(pf) if pf != float("inf") else 999.0,
        "long": n_long, "short": n_short,
        "lon_n": len(lon), "ny_n": len(ny),
        "lon_total": float(sum(t.net_pips for t in lon)),
        "ny_total": float(sum(t.net_pips for t in ny)),
    }


def print_summary(s: dict) -> None:
    if s["n"] == 0:
        print(f"  [{s['label']}] no trades")
        return
    print(
        f"  [{s['label']:<6}] n={s['n']:>3} (LDN={s['lon_n']}: {s['lon_total']:+.0f}, "
        f"NY={s['ny_n']}: {s['ny_total']:+.0f})  "
        f"total={s['total']:+8.1f}  mean={s['mean']:+6.2f}  "
        f"WR={s['wr']*100:5.1f}%  PF={s['pf']:5.2f}  "
        f"avgW={s['avg_win']:+6.1f}  avgL={s['avg_loss']:+6.1f}  "
        f"t={s['t']:+5.2f}  p={s['p']:.4f}"
    )


def split_is_oos(arr: np.ndarray, frac: float = IS_FRACTION) -> tuple[np.ndarray, np.ndarray]:
    n = int(len(arr) * frac)
    return arr[:n], arr[n:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=" * 110)
    print(f"Oil ORB v2 — ТОЧНЫЙ клон gold_orb методологии")
    print(f"  London {LONDON_OPEN}-{LONDON_ORB_END} formation, {LONDON_ORB_END}-{LONDON_CLOSE} trade")
    print(f"  NY     {NY_OPEN}-{NY_ORB_END} formation, {NY_ORB_END}-{NY_CLOSE} trade")
    print(f"  M5 base, touch-break, SL={SL_ATR_MULT}×ATR TP={TP_ATR_MULT}×ATR (R:R=2)  "
          f"Cost R-T={COST_PIPS} pips")
    print("=" * 110)

    results = {}

    for sym in INSTRUMENTS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"\n[WARN] no data: {sym}")
            continue
        arr = raw  # M5 base (без resample)
        is_arr, oos_arr = split_is_oos(arr)
        days_is = (is_arr[-1]["ts"] - is_arr[0]["ts"]) / 86400
        days_oos = (oos_arr[-1]["ts"] - oos_arr[0]["ts"]) / 86400

        print(f"\n{'=' * 110}")
        print(f"{sym}  |  M5 bars total={len(arr)}  "
              f"IS={len(is_arr)} ({days_is:.1f}d)  OOS={len(oos_arr)} ({days_oos:.1f}d)")
        print(f"{'=' * 110}")

        is_trades = backtest_oil_orb_v2(is_arr, sym)
        oos_trades = backtest_oil_orb_v2(oos_arr, sym)

        s_is = summarize(is_trades, "IS")
        s_oos = summarize(oos_trades, "OOS")
        print_summary(s_is)
        print_summary(s_oos)

        if args.verbose:
            print(f"\n  IS trades (first 10):")
            for t in is_trades[:10]:
                dt = datetime.fromtimestamp(t.entry_ts, UTC)
                print(f"    {dt} [{t.session}] dir={'L' if t.direction==1 else 'S'} "
                      f"entry={t.entry_price:.3f} atr={t.atr_at_entry:.3f} "
                      f"exit={t.exit_reason} net={t.net_pips:+.1f}")

        results[sym] = {"is": s_is, "oos": s_oos,
                        "days_is": days_is, "days_oos": days_oos}

    # ── FINAL ──
    print("\n" + "=" * 110)
    print("FINAL VERDICT (clone of gold_orb methodology on CL/BZ)")
    print("=" * 110)
    print(f"{'Symbol':<10}{'Phase':<6}{'n':>5}{'LDN':>12}{'NY':>12}{'total':>10}{'WR':>7}{'PF':>7}{'p':>9}  Verdict")
    for sym, r in results.items():
        for phase, s in [("IS", r["is"]), ("OOS", r["oos"])]:
            line = (f"  {sym:<8}{phase:<6}{s['n']:>5}"
                    f"{s['lon_total']:>+11.0f}p"
                    f"{s['ny_total']:>+11.0f}p"
                    f"{s['total']:>+10.1f}"
                    f"{s['wr']*100:>6.1f}%{s['pf']:>7.2f}{s['p']:>9.4f}")
            if phase == "OOS":
                if s["n"] >= 5 and s["total"] > 0 and s["p"] < 0.15:
                    line += "  ✓ PASS"
                elif s["n"] >= 5 and s["total"] > 0:
                    line += "  ~ +OOS low signif"
                elif s["n"] < 5:
                    line += "  ~ NO DATA"
                else:
                    line += "  ✗ FAIL"
            print(line)

    pr = results.get(PRIMARY, {}).get("oos")
    rp = results.get(REPLICATION, {}).get("oos")
    if pr and rp:
        print("\nReplication check:")
        primary_pass = pr["n"] >= 5 and pr["total"] > 0
        repl_pass = rp["n"] >= 5 and rp["total"] > 0
        if primary_pass and repl_pass:
            print("  ✓ EDGE CONFIRMED на обоих инструментах OOS.")
        elif primary_pass or repl_pass:
            who = PRIMARY if primary_pass else REPLICATION
            print(f"  ~ EDGE PARTIAL: только {who} показал +OOS.")
        else:
            print("  ✗ EDGE FAILED на обоих.")


if __name__ == "__main__":
    main()
