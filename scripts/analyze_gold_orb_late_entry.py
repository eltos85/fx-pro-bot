#!/usr/bin/env python3
"""Аналитический артефакт: late-entry фильтр для `gold_orb`.

## Контекст

28.04.2026 на live-сделке `gold_orb` SHORT XAUUSD #150097702 наблюдался
проблемный паттерн:
- Box ORB (London 08:00–08:15 UTC) `[4633.51..4614.83]`.
- Touch-break SHORT произошёл в 12:01 UTC по цене 4562.56 — это
  **524 pip ниже** `box_low`, через ~46 M5-баров (~3 часа 45 мин)
  после конца ORB-окна. Это «exhaustion entry» в дампе золота.
- Slippage входа +68.7 pip (бот хотел SHORT @4562.56, fill @4555.69),
  далее моментальный отскок → broker SL hit, NET −127.1 pip.

Гипотеза: late-entry в exhausted move (большое расстояние от
пробитой границы и/или много баров спустя) систематически
проигрывает. Фильтр — пропускать touch-break, если выполнено
**одно из**:
- `break_distance_atr > X` — current high/low отклонился от
  box-границы более чем на X ATR.
- `bars_since_box_end > N` — пробой произошёл слишком поздно
  относительно конца ORB-окна.

## Что проверяем

На тех же 90d M5-данных (что и `analyze_gold_orb_trail_compare`)
варьируем эти два порога и измеряем: WR, NET, PF, count.
**Никаких изменений в торговой логике**: чистый аналитический
артефакт. Решение «внедрить фильтр» — только при выполнении
`sample-size.mdc` + согласовании.

## Экспортируем

- `data/gold_orb_late_entry_grid.csv` — grid результатов.
- `data/gold_orb_late_entry_skipped.csv` — какие сделки фильтр
  отрезал бы при разных порогах.
- `data/gold_orb_late_entry_out.txt` — текстовый отчёт.

Запуск:
    PYTHONPATH=src python3 -m scripts.analyze_gold_orb_late_entry \\
        --data-dir data/fxpro_klines --out-dir data
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date
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
    LONDON_OPEN, LONDON_ORB_END, LONDON_CLOSE, NY_OPEN, NY_ORB_END, NY_CLOSE,
    ORB_BARS,
)


log = logging.getLogger("analyze_gold_orb_late_entry")


@dataclass
class Signal:
    idx: int
    direction: str
    entry_price: float
    sl: float
    tp: float
    atr_v: float
    session: str
    bars_since_box_end: int
    break_dist_atr: float


@dataclass
class TradeResult:
    direction: str
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    atr: float
    reason: str
    bars_held: int
    pnl_pips: float
    net_pips: float
    bars_since_box_end: int
    break_dist_atr: float


def _simulate_canon(
    bars: list[Bar], entry_idx: int, sig: Signal,
    spread_pips: float, ps: float,
    max_hold: int = MAX_HOLD_BARS,
) -> TradeResult | None:
    """Простая симуляция CANON (ATR-SL/TP, time-stop), без trail."""
    is_long = sig.direction == "long"
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        b = bars[j]
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sig.sl, sig.tp)
        if hit_sl and hit_tp:
            exit_price, reason = sig.sl, "sl"
        elif hit_sl:
            exit_price, reason = sig.sl, "sl"
        elif hit_tp:
            exit_price, reason = sig.tp, "tp"
        else:
            continue
        return _build(sig, b, exit_price, reason, j - entry_idx, ps, spread_pips)
    end = min(entry_idx + max_hold, len(bars) - 1)
    b = bars[end]
    return _build(sig, b, b.close, "time", end - entry_idx, ps, spread_pips)


def _build(
    sig: Signal, exit_bar: Bar, exit_price: float, reason: str,
    bars_held: int, ps: float, spread_pips: float,
) -> TradeResult:
    is_long = sig.direction == "long"
    gross = (exit_price - sig.entry_price) / ps
    if not is_long:
        gross = -gross
    return TradeResult(
        direction=sig.direction, entry_price=sig.entry_price,
        exit_price=exit_price, sl=sig.sl, tp=sig.tp, atr=sig.atr_v,
        reason=reason, bars_held=bars_held,
        pnl_pips=round(gross, 2),
        net_pips=round(gross - spread_pips, 2),
        bars_since_box_end=sig.bars_since_box_end,
        break_dist_atr=sig.break_dist_atr,
    )


def find_signals(bars: list[Bar]) -> list[Signal]:
    """Ищет gold_orb entry-сигналы и обогащает их late-entry метриками."""
    sigs: list[Signal] = []
    open_until = -1
    traded_session: set[tuple[date, str]] = set()

    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= open_until:
            continue
        last = bars[i]
        t = last.ts.time()
        if LONDON_ORB_END <= t < LONDON_CLOSE:
            session_tag = "london"
            box_end_min = LONDON_ORB_END.hour * 60 + LONDON_ORB_END.minute
        elif NY_ORB_END <= t < NY_CLOSE:
            session_tag = "ny"
            box_end_min = NY_ORB_END.hour * 60 + NY_ORB_END.minute
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
        break_dist_atr = 0.0
        if last.high > box_high:
            direction, entry_break = "long", box_high
            break_dist_atr = (last.high - box_high) / atr_v
        elif last.low < box_low:
            direction, entry_break = "short", box_low
            break_dist_atr = (box_low - last.low) / atr_v
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

        cur_min = t.hour * 60 + t.minute
        bars_since = max(0, (cur_min - box_end_min) // 5)

        sigs.append(Signal(
            idx=i, direction=direction, entry_price=entry_break,
            sl=sl, tp=tp, atr_v=atr_v, session=session_tag,
            bars_since_box_end=bars_since,
            break_dist_atr=round(break_dist_atr, 2),
        ))
        traded_session.add(key)
        open_until = i + MAX_HOLD_BARS

    return sigs


def _stats(label: str, trades: list[TradeResult]) -> dict:
    if not trades:
        return {"label": label, "n": 0, "wr": 0.0, "net": 0.0, "pf": 0.0,
                "avg": 0.0}
    n = len(trades)
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    nets = [t.net_pips for t in trades]
    gp = sum(t.net_pips for t in wins)
    gl = -sum(t.net_pips for t in losses) or 1e-9
    return {
        "label": label, "n": n,
        "wr": len(wins) / n * 100,
        "net": round(sum(nets), 1),
        "pf": round(gp / gl, 2),
        "avg": round(statistics.mean(nets), 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data/fxpro_klines")
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    bars = load_bars(Path(args.data_dir), "GC=F")
    if not bars:
        log.error("Нет баров GC=F в %s", args.data_dir)
        return
    log.info("GC=F: %d M5 баров (%s → %s)",
             len(bars), bars[0].ts.date(), bars[-1].ts.date())

    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    sigs = find_signals(bars)
    log.info("Найдено %d gold_orb сигналов за период", len(sigs))

    # Симуляция всех trades (CANON), сохранение метрик late-entry
    all_trades: list[TradeResult] = []
    for s in sigs:
        t = _simulate_canon(bars, s.idx, s, spread, ps)
        if t:
            all_trades.append(t)

    base = _stats("BASELINE (all)", all_trades)

    # ── Grid: фильтр по break_distance_atr ───────────────────────────
    print("\n" + "═" * 80)
    print(" gold_orb late-entry filter — break_distance_atr (BD)")
    print("═" * 80)
    print(f" Период: {bars[0].ts.date()} → {bars[-1].ts.date()}")
    print(f" Baseline (без фильтра): n={base['n']}, WR={base['wr']:.1f}%, "
          f"NET={base['net']}, PF={base['pf']}, avg={base['avg']}")
    print()
    print(f" {'Фильтр':<22}  {'kept':>6}  {'skipped':>8}  {'WR%':>6}  {'NET':>9}  {'PF':>6}  {'avg':>7}")
    print(" " + "─" * 70)

    grid_bd = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    grid_results: list[dict] = []
    for thr in grid_bd:
        kept = [t for t in all_trades if t.break_dist_atr <= thr]
        skipped = [t for t in all_trades if t.break_dist_atr > thr]
        s_kept = _stats(f"BD≤{thr}", kept)
        s_skip = _stats(f"BD>{thr}", skipped)
        print(f" break_dist_atr ≤ {thr:>4.1f}    {s_kept['n']:>6}  "
              f"{s_skip['n']:>8}  {s_kept['wr']:>6.1f}  {s_kept['net']:>9}  "
              f"{s_kept['pf']:>6}  {s_kept['avg']:>7}")
        grid_results.append({
            "filter": f"break_dist_atr<={thr}", "thr": thr, "kept_n": s_kept["n"],
            "skipped_n": s_skip["n"], "kept_wr": s_kept["wr"],
            "kept_net": s_kept["net"], "kept_pf": s_kept["pf"],
            "skipped_net": s_skip["net"],
        })

    # ── Grid: фильтр по bars_since_box_end ───────────────────────────
    print()
    print(" gold_orb late-entry filter — bars_since_box_end (BSE)")
    print(" " + "─" * 70)
    grid_bse = [0, 1, 2, 3, 6, 12, 18, 24, 36]
    for thr in grid_bse:
        kept = [t for t in all_trades if t.bars_since_box_end <= thr]
        skipped = [t for t in all_trades if t.bars_since_box_end > thr]
        s_kept = _stats(f"BSE≤{thr}", kept)
        s_skip = _stats(f"BSE>{thr}", skipped)
        print(f" bars_since_box_end ≤{thr:>3}   {s_kept['n']:>6}  "
              f"{s_skip['n']:>8}  {s_kept['wr']:>6.1f}  {s_kept['net']:>9}  "
              f"{s_kept['pf']:>6}  {s_kept['avg']:>7}")
        grid_results.append({
            "filter": f"bars_since_box_end<={thr}", "thr": thr, "kept_n": s_kept["n"],
            "skipped_n": s_skip["n"], "kept_wr": s_kept["wr"],
            "kept_net": s_kept["net"], "kept_pf": s_kept["pf"],
            "skipped_net": s_skip["net"],
        })

    # ── Combined: BD ≤ X AND BSE ≤ N ─────────────────────────────────
    print()
    print(" Combined filter (AND):")
    print(" " + "─" * 70)
    combos = [(1.0, 6), (1.5, 6), (2.0, 6), (2.0, 12), (3.0, 12), (3.0, 18)]
    for bd_thr, bse_thr in combos:
        kept = [t for t in all_trades
                if t.break_dist_atr <= bd_thr and t.bars_since_box_end <= bse_thr]
        skipped = [t for t in all_trades
                   if not (t.break_dist_atr <= bd_thr and t.bars_since_box_end <= bse_thr)]
        s_kept = _stats(f"BD≤{bd_thr}+BSE≤{bse_thr}", kept)
        s_skip = _stats(f"reject", skipped)
        print(f" BD≤{bd_thr} AND BSE≤{bse_thr:<3}   {s_kept['n']:>6}  "
              f"{s_skip['n']:>8}  {s_kept['wr']:>6.1f}  {s_kept['net']:>9}  "
              f"{s_kept['pf']:>6}  {s_kept['avg']:>7}")
        grid_results.append({
            "filter": f"BD<={bd_thr} AND BSE<={bse_thr}",
            "thr": f"{bd_thr}+{bse_thr}", "kept_n": s_kept["n"],
            "skipped_n": s_skip["n"], "kept_wr": s_kept["wr"],
            "kept_net": s_kept["net"], "kept_pf": s_kept["pf"],
            "skipped_net": s_skip["net"],
        })

    # ── Distribution of skipped trades ────────────────────────────────
    print()
    print(" Distribution всех сделок:")
    print(f" {'bin':<22}  {'count':>6}  {'WR%':>6}  {'NET':>8}  {'PF':>6}")
    bins_bd = [(0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 999.0)]
    for lo, hi in bins_bd:
        sub = [t for t in all_trades if lo <= t.break_dist_atr < hi]
        s = _stats(f"BD[{lo},{hi})", sub)
        print(f" break_dist_atr [{lo},{hi})    {s['n']:>6}  {s['wr']:>6.1f}  "
              f"{s['net']:>8}  {s['pf']:>6}")
    print()
    bins_bse = [(0, 3), (3, 6), (6, 12), (12, 24), (24, 999)]
    for lo, hi in bins_bse:
        sub = [t for t in all_trades if lo <= t.bars_since_box_end < hi]
        s = _stats(f"BSE[{lo},{hi})", sub)
        print(f" bars_since_box_end [{lo},{hi}) {s['n']:>6}  {s['wr']:>6.1f}  "
              f"{s['net']:>8}  {s['pf']:>6}")

    # CSV экспорт
    grid_path = Path(args.out_dir) / "gold_orb_late_entry_grid.csv"
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    with grid_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(grid_results[0].keys()))
        w.writeheader()
        for row in grid_results:
            w.writerow(row)
    print()
    print(f" Grid CSV: {grid_path}")

    trades_path = Path(args.out_dir) / "gold_orb_late_entry_trades.csv"
    with trades_path.open("w", newline="") as f:
        fields = ["direction", "entry_price", "exit_price", "sl", "tp",
                  "atr", "reason", "bars_held", "pnl_pips", "net_pips",
                  "bars_since_box_end", "break_dist_atr"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in all_trades:
            w.writerow({k: getattr(t, k) for k in fields})
    print(f" Per-trade CSV: {trades_path}")
    print("═" * 80)


if __name__ == "__main__":
    main()
