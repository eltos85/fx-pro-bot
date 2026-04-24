#!/usr/bin/env python3
"""Backtest Oil ORB (КАНДИДАТ №3): Opening Range Breakout на CL/BZ.

Методология копирует gold_orb:
- ORB box: 00:00-02:00 UTC (8 M15 bars)
- Trading window: 02:00-08:00 UTC (close-break entries only)
- TP = 3×ATR(14), SL = 1×ATR(14), R:R = 3:1
- Time stop = 24 M15 bars (6h) после entry
- Cost R-T = 3.5 pips (FxPro реальная)

Гипотеза:
- CL=F и BZ=F показали Hurst > 0.53 (trending bias)
- EDA hourly profile (не пережил Bonferroni, но как идея): BZ h=02 +5.00 pips —
  Asian→London transition для oil исторически high-volatility period

Анти-overfit:
- Параметры фиксированы ДО бэктеста.
- Primary = CL; BZ = replication на ТОЙ ЖЕ конфигурации.
- IS/OOS split 70/30 per instrument.
- Robustness: window ±30min, TP mult {2.5, 3.0, 3.5} — только проверка, не подгонка.

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_oil_orb
    PYTHONPATH=src python3 -m scripts.backtest_oil_orb --verbose
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

# ─────────────────── зафиксированные параметры ───────────────────

PRIMARY = "CL=F"
REPLICATION = "BZ=F"
INSTRUMENTS = [PRIMARY, REPLICATION]

BOX_START_UTC = time(0, 0)     # 00:00 UTC
BOX_END_UTC = time(2, 0)       # 02:00 UTC (exclusive)
TRADE_END_UTC = time(8, 0)     # 08:00 UTC (exclusive)

TP_MULT = 3.0                  # 3×ATR
SL_MULT = 1.0                  # 1×ATR (R:R = 3:1)
ATR_PERIOD = 14
TIME_STOP_BARS = 24            # 6 hours M15
COST_PIPS = 3.5                # FxPro round-trip

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


def resample_m15(arr: np.ndarray) -> np.ndarray:
    sec = 15 * 60
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        g = arr[s:e]
        out[i] = (
            unique[i], g["open"][0], g["high"].max(),
            g["low"].min(), g["close"][-1], g["volume"].sum(),
        )
    return out


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


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    direction: int  # +1 long, -1 short
    exit_ts: int
    exit_price: float
    exit_reason: str  # TP / SL / TIME
    atr_at_entry: float
    net_pips: float


def backtest_oil_orb(
    arr: np.ndarray, sym: str,
    box_start: time = BOX_START_UTC,
    box_end: time = BOX_END_UTC,
    trade_end: time = TRADE_END_UTC,
    tp_mult: float = TP_MULT,
    sl_mult: float = SL_MULT,
) -> list[Trade]:
    """Симулятор ORB-стратегии."""
    atr = atr_series(arr, ATR_PERIOD)
    ps = pip_size(sym)
    trades: list[Trade] = []

    # Группируем по дате
    dates = np.array([_utc_date(int(t)) for t in arr["ts"]])
    unique_dates = sorted(set(dates))

    in_position = False
    pos_entry_idx = 0
    pos_direction = 0
    pos_entry_price = 0.0
    pos_tp = 0.0
    pos_sl = 0.0
    pos_atr = 0.0
    pos_time_stop_idx = 0

    def close_position(idx: int, price: float, reason: str) -> None:
        nonlocal in_position
        net_points = (price - pos_entry_price) * pos_direction
        net_pips = net_points / ps - COST_PIPS
        trades.append(Trade(
            entry_ts=int(arr["ts"][pos_entry_idx]),
            entry_price=pos_entry_price,
            direction=pos_direction,
            exit_ts=int(arr["ts"][idx]),
            exit_price=price,
            exit_reason=reason,
            atr_at_entry=pos_atr,
            net_pips=net_pips,
        ))
        in_position = False

    for d in unique_dates:
        day_mask = dates == d
        day_idx = np.where(day_mask)[0]
        if len(day_idx) == 0:
            continue

        # Строим box
        box_bars_idx = [
            i for i in day_idx
            if _in_window(_utc_time(int(arr["ts"][i])), box_start, box_end)
        ]
        if len(box_bars_idx) < 4:
            continue
        box_high = float(np.max(arr["high"][box_bars_idx]))
        box_low = float(np.min(arr["low"][box_bars_idx]))
        last_box_idx = box_bars_idx[-1]

        # Trading window (после box, до trade_end)
        trade_bars_idx = [
            i for i in day_idx
            if i > last_box_idx
            and _in_window(_utc_time(int(arr["ts"][i])), box_end, trade_end)
        ]
        if not trade_bars_idx:
            continue

        for i in trade_bars_idx:
            # Закрыть позицию если есть: проверка SL/TP/time
            if in_position:
                h = float(arr["high"][i])
                l = float(arr["low"][i])
                # Time stop
                if i >= pos_time_stop_idx:
                    close_position(i, float(arr["close"][i]), "TIME")
                    continue
                # LONG
                if pos_direction == 1:
                    if l <= pos_sl:
                        close_position(i, pos_sl, "SL")
                        continue
                    if h >= pos_tp:
                        close_position(i, pos_tp, "TP")
                        continue
                else:  # SHORT
                    if h >= pos_sl:
                        close_position(i, pos_sl, "SL")
                        continue
                    if l <= pos_tp:
                        close_position(i, pos_tp, "TP")
                        continue

            if in_position:
                continue

            # Проверка breakout: close бара пробил box
            c = float(arr["close"][i])
            a = float(atr[i])
            if a <= 0:
                continue
            if c > box_high:
                # LONG breakout
                in_position = True
                pos_entry_idx = i
                pos_direction = 1
                pos_entry_price = c
                pos_tp = c + tp_mult * a
                pos_sl = c - sl_mult * a
                pos_atr = a
                pos_time_stop_idx = i + TIME_STOP_BARS
            elif c < box_low:
                # SHORT breakout
                in_position = True
                pos_entry_idx = i
                pos_direction = -1
                pos_entry_price = c
                pos_tp = c - tp_mult * a
                pos_sl = c + sl_mult * a
                pos_atr = a
                pos_time_stop_idx = i + TIME_STOP_BARS

        # В конце trade_window закрыть открытую позицию
        if in_position and trade_bars_idx:
            last = trade_bars_idx[-1]
            close_position(last, float(arr["close"][last]), "TIME_EOD")

    return trades


def summarize(trades: list[Trade], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "total": 0, "mean": 0, "wr": 0, "p": 1.0,
                "avg_win": 0, "avg_loss": 0, "pf": 0, "long": 0, "short": 0}
    pips = np.array([t.net_pips for t in trades])
    wins = pips[pips > 0]
    losses = pips[pips <= 0]
    n_long = sum(1 for t in trades if t.direction == 1)
    n_short = sum(1 for t in trades if t.direction == -1)
    pf = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and abs(losses.sum()) > 0 else float("inf")
    t_stat, p = stats.ttest_1samp(pips, 0.0)
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
    }


def exit_breakdown(trades: list[Trade]) -> dict:
    reasons = [t.exit_reason for t in trades]
    out = {"TP": 0, "SL": 0, "TIME": 0, "TIME_EOD": 0}
    for r in reasons:
        out[r] = out.get(r, 0) + 1
    return out


def split_is_oos(arr: np.ndarray, frac: float = IS_FRACTION) -> tuple[np.ndarray, np.ndarray]:
    n = int(len(arr) * frac)
    return arr[:n], arr[n:]


def print_summary(s: dict, exits: dict | None = None) -> None:
    if s["n"] == 0:
        print(f"  [{s['label']}] no trades")
        return
    line = (
        f"  [{s['label']:<8}] n={s['n']:>3} (L={s['long']}, S={s['short']})  "
        f"total={s['total']:+8.1f}  mean={s['mean']:+6.2f}  "
        f"WR={s['wr']*100:5.1f}%  PF={s['pf']:4.2f}  "
        f"avgW={s['avg_win']:+5.1f}  avgL={s['avg_loss']:+5.1f}  "
        f"t={s['t']:+5.2f}  p={s['p']:.4f}"
    )
    if exits:
        line += f"  TP:{exits.get('TP',0)}/SL:{exits.get('SL',0)}/TIME:{exits.get('TIME',0)+exits.get('TIME_EOD',0)}"
    print(line)


def robustness_grid(arr_is: np.ndarray, sym: str) -> None:
    """Grid: box window ±30min × TP mult ∈ {2.5, 3.0, 3.5}."""
    print(f"\n  Robustness grid on IS — net_total/n (trades, mean-net):")
    box_starts = [time(23, 30), time(0, 0), time(0, 30)]
    box_ends = [time(1, 30), time(2, 0), time(2, 30)]
    tp_mults = [2.5, 3.0, 3.5]

    print(f"  {'window':<15}" + "".join(f"{'TP='+str(tp):>12}" for tp in tp_mults))
    for bs, be in zip(box_starts, box_ends):
        row = f"  {bs.isoformat(timespec='minutes')}-{be.isoformat(timespec='minutes'):<7}"
        for tp in tp_mults:
            t = backtest_oil_orb(arr_is, sym, box_start=bs, box_end=be, tp_mult=tp)
            s = summarize(t, "grid")
            if s["n"] == 0:
                row += f"{'---':>12}"
            else:
                mark = "*" if s["p"] < 0.05 else " "
                row += f"{s['total']:+5.0f}/{s['n']:>2}{mark:<2}"[:12].rjust(12)
        print(row)


# ─────────────────── main ───────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=" * 110)
    print(f"Oil ORB Backtest | Box {BOX_START_UTC}-{BOX_END_UTC} UTC | "
          f"Trade → {TRADE_END_UTC} | TP={TP_MULT}×ATR SL={SL_MULT}×ATR | "
          f"Time-stop={TIME_STOP_BARS} bars (6h) | Cost R-T={COST_PIPS} pips")
    print("=" * 110)

    results: dict[str, dict] = {}

    for sym in INSTRUMENTS:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"\n[WARN] no data: {sym}")
            continue
        arr = resample_m15(raw)
        is_arr, oos_arr = split_is_oos(arr)
        days_total = (arr[-1]["ts"] - arr[0]["ts"]) / 86400
        days_is = (is_arr[-1]["ts"] - is_arr[0]["ts"]) / 86400
        days_oos = (oos_arr[-1]["ts"] - oos_arr[0]["ts"]) / 86400

        print(f"\n{'=' * 110}")
        print(f"{sym}  |  bars total={len(arr)} ({days_total:.1f}d)  "
              f"IS={len(is_arr)} ({days_is:.1f}d)  OOS={len(oos_arr)} ({days_oos:.1f}d)")
        print(f"{'=' * 110}")

        # IS
        is_trades = backtest_oil_orb(is_arr, sym)
        s_is = summarize(is_trades, "IS")
        print_summary(s_is, exit_breakdown(is_trades))

        # OOS
        oos_trades = backtest_oil_orb(oos_arr, sym)
        s_oos = summarize(oos_trades, "OOS")
        print_summary(s_oos, exit_breakdown(oos_trades))

        # Robustness (на IS, только проверка)
        if len(is_trades) > 5:
            robustness_grid(is_arr, sym)

        results[sym] = {"is": s_is, "oos": s_oos,
                        "is_trades": is_trades, "oos_trades": oos_trades,
                        "days_is": days_is, "days_oos": days_oos}

        if args.verbose and oos_trades:
            print(f"\n  OOS trades detail:")
            for t in oos_trades[:20]:
                dt = datetime.fromtimestamp(t.entry_ts, UTC)
                print(f"    {dt} dir={'L' if t.direction==1 else 'S'} "
                      f"entry={t.entry_price:.2f} atr={t.atr_at_entry:.3f} "
                      f"exit={t.exit_reason} net={t.net_pips:+.1f}")

    # ── Финальный вердикт ──
    print("\n" + "=" * 110)
    print("FINAL VERDICT (после cost 3.5 pips R-T)")
    print("=" * 110)
    print(f"{'Symbol':<10}{'Phase':<6}{'n':>5}{'total':>10}{'mean':>9}{'WR':>7}{'PF':>7}{'p':>9}  Verdict")
    for sym, r in results.items():
        for phase, s in [("IS", r["is"]), ("OOS", r["oos"])]:
            line = (f"  {sym:<8}{phase:<6}{s['n']:>5}"
                    f"{s['total']:>+10.1f}{s['mean']:>+9.2f}"
                    f"{s['wr']*100:>6.1f}%{s['pf']:>7.2f}{s['p']:>9.4f}")
            if phase == "OOS":
                if s["n"] >= 5 and s["total"] > 0 and s["p"] < 0.1:
                    line += "  ✓ PASS"
                elif s["n"] >= 5 and s["total"] > 0:
                    line += "  ~ OOS+ but not significant"
                else:
                    line += "  ✗ FAIL"
            print(line)

    # replication check
    primary = results.get(PRIMARY)
    repl = results.get(REPLICATION)
    if primary and repl:
        print("\nReplication check (должно работать на BZ=F с ТЕМИ ЖЕ параметрами):")
        primary_oos = primary["oos"]
        repl_oos = repl["oos"]
        if (primary_oos["total"] > 0 and repl_oos["total"] > 0
                and primary_oos["p"] < 0.15 and repl_oos["p"] < 0.15):
            print("  ✓ EDGE CONFIRMED — оба инструмента +OOS, edge воспроизводится.")
        elif primary_oos["total"] > 0 or repl_oos["total"] > 0:
            print("  ~ EDGE WEAK — только один инструмент показал +OOS.")
        else:
            print("  ✗ EDGE FAILED — ни один инструмент не подтвердил edge на OOS.")


if __name__ == "__main__":
    main()
