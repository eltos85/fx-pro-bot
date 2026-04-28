#!/usr/bin/env python3
"""Аналитический артефакт: сравнение `gold_orb` с/без bot-side `scalp_trail`.

Цель — понять, режет ли live-режим (bot-side trailing на 5-мин циклах,
SCALPING_TRAIL_TRIGGER/DISTANCE из monitor.py) прибыль `gold_orb` относительно
канонической схемы из исследования (ATR-SL/TP only, без trail), на которой
обоснован baseline +6146 net pip за 90d (см. STRATEGIES.md §3b-bis).

Скрипт **только аналитический** — не меняет торговую логику, не читает state
бота, не вмешивается в стратегию. Результат подлежит обсуждению с
пользователем перед любыми правками (правило `strategy-guard.mdc`).

## Что проверяем

Один и тот же набор entry-сигналов `gold_orb` (touch-break box, EMA-slope,
1 trade per session per day) симулируется в двух вариантах:

- **CANON**: ATR-SL=1.5×ATR, ATR-TP=3.0×ATR, time-stop по `MAX_HOLD_BARS`
  (`backtest_fxpro_all.py`). Всё, как в +6146-pip baseline.
- **LIVE**: те же ATR-SL/TP, но **дополнительно** на каждом баре close
  проверяется bot-side `scalp_trail` exit:
    - peak_pips = (entry-peak)/pip_size  (для long: peak=max(closes); short: min)
    - trigger = max(SCALPING_TRAIL_TRIGGER_ATR_MULT * atr_pips,
                    SCALPING_TRAIL_TRIGGER_PIPS)
    - trail_d = max(SCALPING_TRAIL_DISTANCE_ATR_MULT * atr_pips,
                    SCALPING_TRAIL_DISTANCE_PIPS)
    - если peak_pips >= trigger AND (peak_pips - cur_pips) >= trail_d → exit
      по close текущего бара.
  Hard-stop SCALPING_HARD_STOP_HOURS=4h (12 баров M5) применяется как time-stop.

Этот вариант моделирует **верхнюю границу** live-перформанса: live-бот ещё
страдает от broker-amend REJECTED и spread variance, не учтённых в backtest.

## Как читать результат

Если CANON значимо лучше LIVE по net_pips / PF / WR — это аргумент в пользу
**отключения bot-side `scalp_trail` для gold_orb** (вернуть к каноничной форме
из research). Решение принимается **только** при выполнении `sample-size.mdc`
(достаточная выборка, p-value, OOS-проверка) и явном согласовании.

Запуск:
    PYTHONPATH=src python3 -m scripts.analyze_gold_orb_trail_compare \\
        --data-dir data/fxpro_klines --out-dir data
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx
from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.strategies.scalping.indicators import ema_slope

from scripts.backtest_fxpro_all import (
    LIVE_WINDOW_BARS, MAX_HOLD_BARS, load_bars, _hit_sl_tp,
)
from scripts.backtest_fxpro_candidates import (
    _session_box_gold, GOLD_ORB_SL_ATR, GOLD_ORB_TP_ATR, GOLD_ORB_ADX_MAX,
    LONDON_ORB_END, LONDON_CLOSE, NY_ORB_END, NY_CLOSE, ORB_BARS,
)


log = logging.getLogger("analyze_gold_orb_trail_compare")


# ── параметры из live monitor.py ────────────────────────────────────
SCALPING_TRAIL_TRIGGER_PIPS = 5.0
SCALPING_TRAIL_TRIGGER_ATR_MULT = 0.6
SCALPING_TRAIL_DISTANCE_PIPS = 3.0
SCALPING_TRAIL_DISTANCE_ATR_MULT = 0.3
SCALPING_HARD_STOP_HOURS = 4.0
SCALPING_HARD_STOP_BARS = int(SCALPING_HARD_STOP_HOURS * 60 / 5)  # M5 → 48 баров


@dataclass
class TradeResult:
    variant: str           # "CANON" | "LIVE"
    direction: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    sl: float
    tp: float
    atr: float
    reason: str            # tp | sl | scalp_trail | time | endofdata
    bars_held: int
    peak_price: float      # лучший close по направлению
    peak_pips: float       # peak P&L в pips
    pnl_pips: float        # gross
    net_pips: float        # net (минус spread round-trip)


def _calc_pips(direction: str, entry: float, price: float, ps: float) -> float:
    diff = (price - entry) if direction == "long" else (entry - price)
    return diff / ps if ps > 0 else 0.0


def _simulate_canon(
    bars: list[Bar], entry_idx: int, direction: str,
    entry_price: float, sl: float, tp: float,
    atr_v: float, spread_pips: float, ps: float,
    max_hold: int = MAX_HOLD_BARS,
) -> TradeResult | None:
    """Канон: SL/TP по бар high/low, time-stop по max_hold (12 баров → 6h)."""
    is_long = direction == "long"
    peak = entry_price
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        b = bars[j]
        peak = max(peak, b.close) if is_long else min(peak, b.close)
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sl, tp)
        if hit_sl and hit_tp:
            exit_price, reason = sl, "sl"
        elif hit_sl:
            exit_price, reason = sl, "sl"
        elif hit_tp:
            exit_price, reason = tp, "tp"
        else:
            continue
        return _build_result(
            "CANON", direction, bars[entry_idx], b, entry_price, exit_price,
            sl, tp, atr_v, reason, j - entry_idx, peak, ps, spread_pips,
        )
    end = min(entry_idx + max_hold, len(bars) - 1)
    b = bars[end]
    return _build_result(
        "CANON", direction, bars[entry_idx], b, entry_price, b.close,
        sl, tp, atr_v, "time", end - entry_idx, peak, ps, spread_pips,
    )


def _simulate_live(
    bars: list[Bar], entry_idx: int, direction: str,
    entry_price: float, sl: float, tp: float,
    atr_v: float, spread_pips: float, ps: float,
) -> TradeResult | None:
    """LIVE: те же SL/TP intra-bar + bot-side scalp_trail exit на close."""
    is_long = direction == "long"
    atr_pips = atr_v / ps if ps > 0 else 0.0
    trigger_pips = max(SCALPING_TRAIL_TRIGGER_ATR_MULT * atr_pips,
                       SCALPING_TRAIL_TRIGGER_PIPS)
    trail_d_pips = max(SCALPING_TRAIL_DISTANCE_ATR_MULT * atr_pips,
                       SCALPING_TRAIL_DISTANCE_PIPS)
    peak = entry_price
    max_hold = SCALPING_HARD_STOP_BARS  # 48 для scalping (4h)
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        b = bars[j]
        # Сначала проверка intra-bar SL/TP — broker-side, реагирует на тики раньше.
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sl, tp)
        if hit_sl and hit_tp:
            exit_price, reason = sl, "sl"
            return _build_result(
                "LIVE", direction, bars[entry_idx], b, entry_price, exit_price,
                sl, tp, atr_v, reason, j - entry_idx, peak, ps, spread_pips,
            )
        if hit_sl:
            return _build_result(
                "LIVE", direction, bars[entry_idx], b, entry_price, sl,
                sl, tp, atr_v, "sl", j - entry_idx, peak, ps, spread_pips,
            )
        if hit_tp:
            return _build_result(
                "LIVE", direction, bars[entry_idx], b, entry_price, tp,
                sl, tp, atr_v, "tp", j - entry_idx, peak, ps, spread_pips,
            )
        # SL/TP не сработали — обновляем peak по close (как делает live monitor),
        # затем проверяем scalp_trail.
        peak = max(peak, b.close) if is_long else min(peak, b.close)
        cur_pips = _calc_pips(direction, entry_price, b.close, ps)
        peak_pips = _calc_pips(direction, entry_price, peak, ps)
        if peak_pips >= trigger_pips and (peak_pips - cur_pips) >= trail_d_pips:
            return _build_result(
                "LIVE", direction, bars[entry_idx], b, entry_price, b.close,
                sl, tp, atr_v, "scalp_trail", j - entry_idx, peak, ps, spread_pips,
            )
    end = min(entry_idx + max_hold, len(bars) - 1)
    b = bars[end]
    return _build_result(
        "LIVE", direction, bars[entry_idx], b, entry_price, b.close,
        sl, tp, atr_v, "scalp_time_4h", end - entry_idx, peak, ps, spread_pips,
    )


def _build_result(
    variant: str, direction: str, entry_bar: Bar, exit_bar: Bar,
    entry: float, exit_p: float, sl: float, tp: float, atr_v: float,
    reason: str, bars_held: int, peak: float, ps: float, spread_pips: float,
) -> TradeResult:
    gross = (exit_p - entry) / ps
    if direction != "long":
        gross = -gross
    peak_pips = _calc_pips(direction, entry, peak, ps)
    net = gross - spread_pips
    return TradeResult(
        variant=variant, direction=direction,
        entry_ts=entry_bar.ts, entry_price=entry,
        exit_ts=exit_bar.ts, exit_price=exit_p,
        sl=sl, tp=tp, atr=atr_v, reason=reason, bars_held=bars_held,
        peak_price=peak, peak_pips=round(peak_pips, 2),
        pnl_pips=round(gross, 2), net_pips=round(net, 2),
    )


# ── генератор сигналов = копия `backtest_gold_orb` без simulate ──────


def find_gold_orb_signals(bars: list[Bar]) -> list[dict]:
    """Ищет entry-моменты `gold_orb`. Возвращает список dict-ов с:
    {idx, direction, entry_price, sl, tp, atr_v}.
    """
    signals: list[dict] = []
    sym = "GC=F"
    ps = pip_size(sym)
    open_until = -1
    traded_session: set[tuple[date, str]] = set()

    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= open_until:
            continue
        last = bars[i]
        t = last.ts.time()
        if LONDON_ORB_END <= t < LONDON_CLOSE:
            session_tag = "london"
        elif NY_ORB_END <= t < NY_CLOSE:
            session_tag = "ny"
        else:
            continue
        key = (last.ts.date(), session_tag)
        if key in traded_session:
            continue

        window = bars[i - LIVE_WINDOW_BARS: i + 1]
        atr_v = _atr(window)
        if atr_v <= 0:
            continue
        if compute_adx(window) > GOLD_ORB_ADX_MAX:
            continue

        box = _session_box_gold(window, last.ts)
        if box is None:
            continue
        box_high, box_low, _ = box

        direction: str | None = None
        entry_break: float | None = None
        if last.high > box_high:
            direction, entry_break = "long", box_high
        elif last.low < box_low:
            direction, entry_break = "short", box_low
        else:
            continue

        closes = [b.close for b in window]
        ema_vals = _ema(closes, 50)
        slope = ema_slope(ema_vals, 5)
        if direction == "long" and slope < 0:
            continue
        if direction == "short" and slope > 0:
            continue

        if direction == "long":
            sl = entry_break - GOLD_ORB_SL_ATR * atr_v
            tp = entry_break + GOLD_ORB_TP_ATR * atr_v
        else:
            sl = entry_break + GOLD_ORB_SL_ATR * atr_v
            tp = entry_break - GOLD_ORB_TP_ATR * atr_v

        signals.append({
            "idx": i, "direction": direction, "entry_price": entry_break,
            "sl": sl, "tp": tp, "atr_v": atr_v, "session": session_tag,
            "ts": last.ts,
        })
        traded_session.add(key)
        open_until = i + MAX_HOLD_BARS

    return signals


# ── статистика ───────────────────────────────────────────────────────


def _summarize(label: str, trades: list[TradeResult]) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    n = len(trades)
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    nets = [t.net_pips for t in trades]
    gross_p = sum(t.net_pips for t in wins)
    gross_l = -sum(t.net_pips for t in losses) or 1e-9
    pf = gross_p / gross_l
    avg_win = statistics.mean([t.net_pips for t in wins]) if wins else 0.0
    avg_loss = statistics.mean([t.net_pips for t in losses]) if losses else 0.0
    return {
        "label": label, "n": n,
        "wr": len(wins) / n * 100,
        "net": round(sum(nets), 1),
        "avg": round(statistics.mean(nets), 2),
        "median": round(statistics.median(nets), 2),
        "pf": round(pf, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max(nets), 1),
        "max_loss": round(min(nets), 1),
    }


def _by_reason(trades: list[TradeResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[t.reason] = out.get(t.reason, 0) + 1
    return out


def _walk_forward_thirds(trades: list[TradeResult]) -> list[dict]:
    if not trades:
        return []
    sorted_t = sorted(trades, key=lambda t: t.entry_ts)
    n = len(sorted_t)
    third = n // 3
    parts: list[list[TradeResult]] = []
    parts.append(sorted_t[:third])
    parts.append(sorted_t[third:2 * third])
    parts.append(sorted_t[2 * third:])
    return [_summarize(f"T{i+1}", p) for i, p in enumerate(parts)]


# ── main ─────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data/fxpro_klines")
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    bars = load_bars(Path(args.data_dir), "GC=F")
    if not bars:
        log.error("Нет баров GC=F в %s", args.data_dir)
        return
    log.info("GC=F: %d M5 баров (%s → %s)",
             len(bars), bars[0].ts.date(), bars[-1].ts.date())

    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    sigs = find_gold_orb_signals(bars)
    log.info("Найдено %d gold_orb сигналов за период", len(sigs))

    canon: list[TradeResult] = []
    live: list[TradeResult] = []
    for s in sigs:
        c = _simulate_canon(
            bars, s["idx"], s["direction"], s["entry_price"],
            s["sl"], s["tp"], s["atr_v"], spread, ps,
        )
        l = _simulate_live(
            bars, s["idx"], s["direction"], s["entry_price"],
            s["sl"], s["tp"], s["atr_v"], spread, ps,
        )
        if c:
            canon.append(c)
        if l:
            live.append(l)

    # ── вывод ─────────────────────────────────────────────────────────
    print("\n" + "═" * 80)
    print(" gold_orb: CANON (ATR-SL/TP only) vs LIVE (+ bot-side scalp_trail)")
    print("═" * 80)
    print(f" Период: {bars[0].ts.date()} → {bars[-1].ts.date()}, {len(bars)} M5 баров")
    print(f" Сигналов: {len(sigs)}, симулировано: CANON={len(canon)}, LIVE={len(live)}")
    print()

    sc = _summarize("CANON", canon)
    sl_ = _summarize("LIVE",  live)
    print(f" {'Метрика':<18}  {'CANON':>10}  {'LIVE':>10}  {'Δ (LIVE-CANON)':>15}")
    print(" " + "─" * 58)
    for key, lab in [
        ("n", "trades"), ("wr", "win-rate %"), ("net", "net pips"),
        ("pf", "profit factor"), ("avg", "avg pip"), ("median", "median pip"),
        ("avg_win", "avg win"), ("avg_loss", "avg loss"),
        ("max_win", "max win"), ("max_loss", "max loss"),
    ]:
        c = sc.get(key, 0)
        l = sl_.get(key, 0)
        if isinstance(c, float):
            d = round(l - c, 2)
            print(f" {lab:<18}  {c:>10.2f}  {l:>10.2f}  {d:>+15.2f}")
        else:
            print(f" {lab:<18}  {c:>10}  {l:>10}  {l - c:>+15}")

    print()
    print(" Распределение exit reasons:")
    cr = _by_reason(canon)
    lr = _by_reason(live)
    all_reasons = sorted(set(cr) | set(lr))
    for r in all_reasons:
        print(f"   {r:<18}  CANON={cr.get(r, 0):>4}  LIVE={lr.get(r, 0):>4}")

    print()
    print(" Walk-forward (трети по времени):")
    wf_c = _walk_forward_thirds(canon)
    wf_l = _walk_forward_thirds(live)
    print(f" {'period':<6}  {'n':>4}  {'WR_C%':>6}  {'WR_L%':>6}  {'NET_C':>8}  {'NET_L':>8}  {'PF_C':>6}  {'PF_L':>6}")
    for c, l in zip(wf_c, wf_l):
        print(f" {c['label']:<6}  {c['n']:>4}  {c['wr']:>6.1f}  {l['wr']:>6.1f}  "
              f"{c['net']:>8}  {l['net']:>8}  {c['pf']:>6}  {l['pf']:>6}")

    # Сохраняем per-trade CSV для аудита
    out_path = Path(args.out_dir) / "gold_orb_trail_compare.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["variant", "direction", "entry_ts", "entry_price", "exit_ts",
              "exit_price", "sl", "tp", "atr", "reason", "bars_held",
              "peak_price", "peak_pips", "pnl_pips", "net_pips"]
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in canon + live:
            w.writerow({
                "variant": t.variant, "direction": t.direction,
                "entry_ts": t.entry_ts.isoformat(),
                "entry_price": round(t.entry_price, 5),
                "exit_ts": t.exit_ts.isoformat(),
                "exit_price": round(t.exit_price, 5),
                "sl": round(t.sl, 5), "tp": round(t.tp, 5),
                "atr": round(t.atr, 5), "reason": t.reason,
                "bars_held": t.bars_held,
                "peak_price": round(t.peak_price, 5),
                "peak_pips": t.peak_pips,
                "pnl_pips": t.pnl_pips, "net_pips": t.net_pips,
            })
    print()
    print(f" Per-trade CSV: {out_path}")
    print("═" * 80)


if __name__ == "__main__":
    main()
