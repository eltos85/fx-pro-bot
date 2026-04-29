#!/usr/bin/env python3
"""OOS-проверка canonical session-guard для `gold_orb`.

Цель — ответить на вопрос: должно ли `process_signals` приводиться в
соответствие с docstring `gold_orb.py` («1 trade per session per day»,
канон Carter 2012 ch.7)? Сегодняшняя сессия 29.04 показала 5 SHORT
входов в один London ORB box за 3 часа (см. BUILDLOG.md 2026-04-29).
Текущий код проверяет только `count_open_positions` и пропускает re-entry
сразу после закрытия предыдущей сделки.

Скрипт **только аналитический** — не меняет торговую логику. Сравнивает
4 конфигурации на 2 датасетах (90d in-sample + fresh 30d OOS):

    {baseline, canonical-guard} × {canonical-exit, live-exit}

- `baseline`: открываем по любому валидному touch-break, как только
  предыдущая позиция закрыта (текущий код).
- `canonical-guard`: 1 entry per (date, session) — после первой
  открытой позиции в сессии новые сигналы блокируются до следующей
  сессии (London/NY × date).
- `canonical-exit` (CANON): SL/TP intra-bar + time-stop MAX_HOLD_BARS=72.
- `live-exit` (LIVE): canonical SL/TP + bot-side `scalp_trail` exit
  (scalping_trail_trigger/distance из monitor.py) + hard-stop 4h.

## Pass / Fail / Inconclusive

Pass-критерий (план OOS gold_orb session guard):
- canonical-guard >= baseline по Net pips / PF / Sharpe в обоих датасетах
- Walk-forward T1/T2/T3 не deteriorate >10%

Fail-критерий: canonical-guard хуже >5% по любому ключевому метрику.
Inconclusive: разница в пределах 5% — отдельное обсуждение.

## Использование

    PYTHONPATH=src python3 -m scripts.analyze_gold_orb_session_guard \\
        --in-sample data/fxpro_klines/GC_F_M5.csv \\
        --oos data/fxpro_klines/GC_F_M5_122d.csv \\
        --oos-end 2026-01-28T00:00:00Z \\
        --out-dir data
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx
from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import ema_slope

from scripts.analyze_gold_orb_trail_compare import (
    SCALPING_HARD_STOP_BARS,
    TradeResult,
    _build_result,
    _by_reason,
    _calc_pips,
    _simulate_canon as _sim_canon_inner,
    _simulate_live as _sim_live_inner,
)
from scripts.backtest_fxpro_all import (
    LIVE_WINDOW_BARS,
    MAX_HOLD_BARS,
    _hit_sl_tp,
)
from scripts.backtest_fxpro_candidates import (
    GOLD_ORB_ADX_MAX,
    GOLD_ORB_SL_ATR,
    GOLD_ORB_TP_ATR,
    LONDON_CLOSE,
    LONDON_ORB_END,
    NY_CLOSE,
    NY_ORB_END,
    _session_box_gold,
)

log = logging.getLogger("analyze_gold_orb_session_guard")


# ─── загрузка/фильтр баров ───────────────────────────────────────────


def _load_bars_csv(path: Path, yf_symbol: str = "GC=F") -> list[Bar]:
    if not path.exists():
        return []
    instr = InstrumentId(symbol=yf_symbol)
    bars: list[Bar] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            ts_ms = int(row["timestamp"])
            bars.append(Bar(
                instrument=instr,
                ts=datetime.fromtimestamp(ts_ms / 1000, UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


def _slice_bars(bars: list[Bar], start: datetime | None, end: datetime | None) -> list[Bar]:
    if start is None and end is None:
        return bars
    out = []
    for b in bars:
        if start and b.ts < start:
            continue
        if end and b.ts >= end:
            continue
        out.append(b)
    return out


# ─── чистая проверка сигнала на конкретном баре ──────────────────────


@dataclass
class Signal:
    idx: int
    direction: str        # "long" | "short"
    entry_price: float
    sl: float
    tp: float
    atr_v: float
    session: str          # "london" | "ny"
    ts: datetime


def _check_signal_at(bars: list[Bar], i: int) -> Signal | None:
    """Pure-проверка сигнала `gold_orb` для бара i. Без дедупликации."""
    last = bars[i]
    t = last.ts.time()
    if LONDON_ORB_END <= t < LONDON_CLOSE:
        session_tag = "london"
    elif NY_ORB_END <= t < NY_CLOSE:
        session_tag = "ny"
    else:
        return None

    window = bars[i - LIVE_WINDOW_BARS: i + 1]
    atr_v = _atr(window)
    if atr_v <= 0:
        return None
    if compute_adx(window) > GOLD_ORB_ADX_MAX:
        return None

    box = _session_box_gold(window, last.ts)
    if box is None:
        return None
    box_high, box_low, _ = box

    if last.high > box_high:
        direction, entry_break = "long", box_high
    elif last.low < box_low:
        direction, entry_break = "short", box_low
    else:
        return None

    closes = [b.close for b in window]
    ema_vals = _ema(closes, 50)
    slope = ema_slope(ema_vals, 5)
    if direction == "long" and slope < 0:
        return None
    if direction == "short" and slope > 0:
        return None

    if direction == "long":
        sl = entry_break - GOLD_ORB_SL_ATR * atr_v
        tp = entry_break + GOLD_ORB_TP_ATR * atr_v
    else:
        sl = entry_break + GOLD_ORB_SL_ATR * atr_v
        tp = entry_break - GOLD_ORB_TP_ATR * atr_v

    return Signal(
        idx=i, direction=direction, entry_price=entry_break,
        sl=sl, tp=tp, atr_v=atr_v, session=session_tag, ts=last.ts,
    )


# ─── главный симулятор: signals + simulate, с/без guard ─────────────


def simulate_with_guard(
    bars: list[Bar],
    sim_fn,
    *,
    session_guard: bool,
    spread: float,
    ps: float,
) -> list[TradeResult]:
    """Прогон gold_orb от первого до последнего бара.

    sim_fn(bars, entry_idx, direction, entry_price, sl, tp, atr_v, spread, ps)
        → TradeResult | None.

    session_guard=True → 1 trade per (date, session_tag).
    session_guard=False → multi-entry, блокируется только активной позицией.
    """
    trades: list[TradeResult] = []
    active_until_idx = -1
    traded_session: set[tuple[date, str]] = set()

    n = len(bars)
    for i in range(LIVE_WINDOW_BARS, n):
        if i <= active_until_idx:
            continue

        sig = _check_signal_at(bars, i)
        if sig is None:
            continue

        key = (sig.ts.date(), sig.session)
        if session_guard and key in traded_session:
            continue

        tr = sim_fn(
            bars, sig.idx, sig.direction, sig.entry_price,
            sig.sl, sig.tp, sig.atr_v, spread, ps,
        )
        if tr is None:
            traded_session.add(key)
            continue

        trades.append(tr)
        active_until_idx = sig.idx + tr.bars_held
        traded_session.add(key)

    return trades


# ─── метрики ─────────────────────────────────────────────────────────


def _sharpe(nets: list[float]) -> float:
    if len(nets) < 2:
        return 0.0
    mu = statistics.mean(nets)
    sd = statistics.pstdev(nets)
    if sd == 0:
        return 0.0
    # Annualized Sharpe (assumes ~250 trading days; trades-per-day varies).
    # Используем naive trade-level Sharpe (mu/sd × sqrt(n)) для сравнимости.
    return (mu / sd) * math.sqrt(len(nets))


def _max_dd(nets: list[float]) -> float:
    """Max drawdown по cumulative P&L."""
    if not nets:
        return 0.0
    cum = 0.0
    peak = 0.0
    dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return round(dd, 1)


def _summary(label: str, trades: list[TradeResult]) -> dict:
    if not trades:
        return {
            "label": label, "n": 0, "wr": 0, "net": 0, "pf": 0,
            "sharpe": 0, "max_dd": 0, "avg": 0, "avg_win": 0, "avg_loss": 0,
            "max_win": 0, "max_loss": 0,
        }
    nets = [t.net_pips for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp = sum(wins)
    gl = -sum(losses) or 1e-9
    return {
        "label": label,
        "n": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "net": round(sum(nets), 1),
        "pf": round(gp / gl, 2),
        "sharpe": round(_sharpe(nets), 2),
        "max_dd": _max_dd(nets),
        "avg": round(statistics.mean(nets), 2),
        "avg_win": round(statistics.mean(wins) if wins else 0.0, 2),
        "avg_loss": round(statistics.mean(losses) if losses else 0.0, 2),
        "max_win": round(max(nets), 1),
        "max_loss": round(min(nets), 1),
    }


def _walk_forward(trades: list[TradeResult], n_buckets: int = 3) -> list[dict]:
    if not trades:
        return []
    sorted_t = sorted(trades, key=lambda t: t.entry_ts)
    n = len(sorted_t)
    chunk = max(1, n // n_buckets)
    out: list[dict] = []
    for k in range(n_buckets):
        a = k * chunk
        b = (k + 1) * chunk if k < n_buckets - 1 else n
        out.append(_summary(f"T{k + 1}", sorted_t[a:b]))
    return out


# ─── вывод ───────────────────────────────────────────────────────────


def _format_matrix(label: str, summaries: dict[str, dict]) -> list[str]:
    rows: list[str] = []
    rows.append("")
    rows.append("─" * 92)
    rows.append(f" {label}")
    rows.append("─" * 92)
    keys = ["baseline_canon", "guard_canon", "baseline_live", "guard_live"]
    titles = {
        "baseline_canon": "BASE×CANON",
        "guard_canon":    "GUARD×CANON",
        "baseline_live":  "BASE×LIVE",
        "guard_live":     "GUARD×LIVE",
    }
    metrics = [
        ("n", "trades"),
        ("wr", "win-rate %"),
        ("net", "net pips"),
        ("pf", "profit factor"),
        ("sharpe", "Sharpe (trade-level)"),
        ("max_dd", "max DD pips"),
        ("avg", "avg pip"),
        ("avg_win", "avg win"),
        ("avg_loss", "avg loss"),
        ("max_win", "max win"),
        ("max_loss", "max loss"),
    ]
    rows.append(f" {'metric':<22}  {titles['baseline_canon']:>12}  {titles['guard_canon']:>12}  "
                f"{titles['baseline_live']:>12}  {titles['guard_live']:>12}")
    rows.append(" " + "─" * 76)
    for k, lab in metrics:
        cells = []
        for tag in keys:
            v = summaries[tag].get(k, 0)
            cells.append(f"{v:>12}")
        rows.append(f" {lab:<22}  {cells[0]}  {cells[1]}  {cells[2]}  {cells[3]}")
    return rows


def _format_walkforward(label: str, wf: list[dict]) -> list[str]:
    rows: list[str] = []
    rows.append("")
    rows.append(f" {label} — Walk-forward (трети по времени)")
    rows.append(f" {'period':<6}  {'n':>4}  {'WR%':>6}  {'NET':>8}  {'PF':>6}  {'Sharpe':>7}  {'maxDD':>7}")
    rows.append(" " + "─" * 56)
    for s in wf:
        rows.append(f" {s['label']:<6}  {s['n']:>4}  {s['wr']:>6}  {s['net']:>8}  "
                    f"{s['pf']:>6}  {s['sharpe']:>7}  {s['max_dd']:>7}")
    return rows


def _decision_for(
    is90: dict[str, dict], oos30: dict[str, dict], wf_guard: list[dict],
) -> tuple[str, list[str]]:
    """Применяет Pass/Fail/Inconclusive критерии на сравнении BASE vs GUARD."""
    notes: list[str] = []

    def cmp_pair(label: str, base: dict, guard: dict) -> tuple[bool, list[str]]:
        local_pass = True
        local_notes = []
        # Net pips
        base_net = base["net"] or 1e-9
        guard_net = guard["net"]
        if base_net > 0:
            net_delta_pct = (guard_net - base_net) / abs(base_net) * 100
        else:
            # base убыточен — guard принимаем если стал положительным или менее отрицательным
            net_delta_pct = (guard_net - base_net) / abs(base_net) * 100
        local_notes.append(f"  {label}: net base={base_net:.0f} guard={guard_net:.0f} ΔP={net_delta_pct:+.1f}%")
        # PF
        pf_delta = guard["pf"] - base["pf"]
        local_notes.append(f"  {label}: PF base={base['pf']} guard={guard['pf']} Δ={pf_delta:+.2f}")
        # Sharpe
        sh_delta = guard["sharpe"] - base["sharpe"]
        local_notes.append(f"  {label}: Sharpe base={base['sharpe']} guard={guard['sharpe']} Δ={sh_delta:+.2f}")

        # Pass-критерий: guard >= base (нет deterioration >5% ни в одной из 3-х метрик)
        if base_net > 0 and net_delta_pct < -5:
            local_pass = False
        if pf_delta < -0.05:
            local_pass = False
        if sh_delta < -0.05:
            local_pass = False
        return local_pass, local_notes

    notes.append("Сравнение BASE vs GUARD на CANON-симуляторе:")
    pass_canon_is, n1 = cmp_pair("90d CANON", is90["baseline_canon"], is90["guard_canon"])
    notes.extend(n1)
    pass_canon_oos, n2 = cmp_pair("30d CANON", oos30["baseline_canon"], oos30["guard_canon"])
    notes.extend(n2)
    notes.append("")
    notes.append("Сравнение BASE vs GUARD на LIVE-симуляторе:")
    pass_live_is, n3 = cmp_pair("90d LIVE",  is90["baseline_live"], is90["guard_live"])
    notes.extend(n3)
    pass_live_oos, n4 = cmp_pair("30d LIVE",  oos30["baseline_live"], oos30["guard_live"])
    notes.extend(n4)

    # Walk-forward stability check для guard CANON
    notes.append("")
    notes.append("Walk-forward GUARD×CANON stability:")
    wf_pass = True
    if wf_guard and len(wf_guard) >= 3:
        nets = [s["net"] for s in wf_guard]
        notes.append(f"  T1={nets[0]} T2={nets[1]} T3={nets[2]}")
        # каждая треть должна быть в пределах 10% от среднего? нет — проверяем
        # что ни одна треть не deteriorates более чем на 50% от средней (loose).
        avg_net = sum(nets) / len(nets)
        for i, n in enumerate(nets):
            if avg_net > 0 and n < avg_net * 0.5:
                notes.append(f"  T{i+1} deterioration: {n:.0f} < 50% avg {avg_net:.0f}")
                wf_pass = False
            if avg_net < 0 and n < avg_net * 1.5:
                notes.append(f"  T{i+1} worse than avg by >50%")
                wf_pass = False

    all_pass = pass_canon_is and pass_canon_oos and pass_live_is and pass_live_oos and wf_pass
    inconclusive_marks = sum(1 for x in [pass_canon_is, pass_canon_oos, pass_live_is, pass_live_oos]
                             if not x)
    if all_pass:
        return "PASS", notes
    if inconclusive_marks >= 3:
        return "FAIL", notes
    return "INCONCLUSIVE", notes


# ─── main ────────────────────────────────────────────────────────────


def _run_matrix(bars: list[Bar], spread: float, ps: float) -> dict[str, dict]:
    """Запускает 4 комбинации {baseline,guard} × {canon,live}, возвращает summaries."""
    out: dict[str, dict] = {}
    for guard in (False, True):
        for sim_label, sim_fn in (("canon", _sim_canon_inner), ("live", _sim_live_inner)):
            tag = f"{'guard' if guard else 'baseline'}_{sim_label}"
            trades = simulate_with_guard(
                bars, sim_fn, session_guard=guard, spread=spread, ps=ps,
            )
            out[tag] = _summary(tag, trades)
            out[tag]["_trades"] = trades
            out[tag]["_reasons"] = _by_reason(trades)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--in-sample", default="data/fxpro_klines/GC_F_M5.csv")
    p.add_argument("--oos", default="data/fxpro_klines/GC_F_M5_122d.csv")
    p.add_argument("--oos-end", default="2026-01-28T11:40:00+00:00",
                   help="Граница OOS-выборки: bars с ts < oos-end (default: начало in-sample)")
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2
    log.info("ps=%s spread=%.2f pips", ps, spread)

    bars_is = _load_bars_csv(Path(args.in_sample))
    if not bars_is:
        log.error("Нет in-sample баров: %s", args.in_sample)
        return
    log.info("In-sample: %d баров (%s → %s)", len(bars_is),
             bars_is[0].ts.date(), bars_is[-1].ts.date())

    bars_122 = _load_bars_csv(Path(args.oos))
    if not bars_122:
        log.error("Нет 122d-датасета: %s", args.oos)
        return

    oos_end = datetime.fromisoformat(args.oos_end.replace("Z", "+00:00"))
    bars_oos = _slice_bars(bars_122, None, oos_end)
    log.info("OOS (до %s): %d баров (%s → %s)", oos_end.isoformat(),
             len(bars_oos),
             bars_oos[0].ts.date() if bars_oos else "—",
             bars_oos[-1].ts.date() if bars_oos else "—")

    # ── 4×2 матрица на in-sample 90d ──────────────────────────────────
    log.info("Прогон in-sample 90d…")
    is90 = _run_matrix(bars_is, spread, ps)

    # ── 4×2 матрица на fresh 30d OOS ──────────────────────────────────
    log.info("Прогон OOS 30d…")
    oos30 = _run_matrix(bars_oos, spread, ps)

    # ── walk-forward для GUARD×CANON и BASE×CANON на 90d ────────────
    wf_guard = _walk_forward(is90["guard_canon"]["_trades"])
    wf_base = _walk_forward(is90["baseline_canon"]["_trades"])

    # ── формирование отчёта ──────────────────────────────────────────
    out_lines: list[str] = []
    out_lines.append("═" * 92)
    out_lines.append(" gold_orb session-guard OOS analysis (analyze_gold_orb_session_guard.py)")
    out_lines.append("═" * 92)
    out_lines.append(f" In-sample: {bars_is[0].ts.date()} → {bars_is[-1].ts.date()} ({len(bars_is)} bars)")
    out_lines.append(f" OOS:       {bars_oos[0].ts.date()} → {bars_oos[-1].ts.date()} ({len(bars_oos)} bars)")
    out_lines.append("")
    out_lines.append(" BASELINE: текущий код gold_orb (multi-entry, блокируется только активной позицией)")
    out_lines.append(" GUARD:    canonical 1 trade per (date, session) — соответствует docstring")
    out_lines.append(" CANON exit: ATR SL/TP + time-stop MAX_HOLD_BARS=72 (6h)")
    out_lines.append(" LIVE  exit: ATR SL/TP + bot-side scalp_trail + 4h hard-stop")

    out_lines.extend(_format_matrix(
        f"In-sample 90d ({bars_is[0].ts.date()} → {bars_is[-1].ts.date()})",
        is90,
    ))
    out_lines.extend(_format_matrix(
        f"OOS 30d ({bars_oos[0].ts.date()} → {bars_oos[-1].ts.date()})",
        oos30,
    ))
    out_lines.extend(_format_walkforward("In-sample 90d BASE×CANON",  wf_base))
    out_lines.extend(_format_walkforward("In-sample 90d GUARD×CANON", wf_guard))

    # ── case studies: дни с большой разницей BASE vs GUARD ──────────
    out_lines.append("")
    out_lines.append("─" * 92)
    out_lines.append(" Case studies: дни с наибольшей разницей BASE×CANON vs GUARD×CANON (in-sample)")
    out_lines.append("─" * 92)

    def _trades_by_date(trs: list[TradeResult]) -> dict[date, list[TradeResult]]:
        out: dict[date, list[TradeResult]] = {}
        for t in trs:
            out.setdefault(t.entry_ts.date(), []).append(t)
        return out

    base_trs = is90["baseline_canon"]["_trades"]
    guard_trs = is90["guard_canon"]["_trades"]
    base_by = _trades_by_date(base_trs)
    guard_by = _trades_by_date(guard_trs)
    deltas: list[tuple[date, float, float, int]] = []
    for d, btrs in base_by.items():
        gtrs = guard_by.get(d, [])
        b_net = sum(t.net_pips for t in btrs)
        g_net = sum(t.net_pips for t in gtrs)
        deltas.append((d, b_net, g_net, len(btrs)))
    # 5 дней с наибольшей разницей в пользу baseline и 5 — в пользу guard
    deltas.sort(key=lambda x: x[1] - x[2], reverse=True)
    out_lines.append(" Топ-5 дней где BASE >> GUARD (multi-entry помогает):")
    out_lines.append(f"   {'date':<12}  {'BASE_NET':>10}  {'GUARD_NET':>10}  {'Δ':>10}  {'BASE n':>6}")
    for d, bn, gn, bcnt in deltas[:5]:
        out_lines.append(f"   {d.isoformat():<12}  {bn:>10.0f}  {gn:>10.0f}  {bn - gn:>+10.0f}  {bcnt:>6}")
    deltas.sort(key=lambda x: x[1] - x[2])
    out_lines.append(" Топ-5 дней где GUARD > BASE (multi-entry проигрывает):")
    out_lines.append(f"   {'date':<12}  {'BASE_NET':>10}  {'GUARD_NET':>10}  {'Δ':>10}  {'BASE n':>6}")
    for d, bn, gn, bcnt in deltas[:5]:
        out_lines.append(f"   {d.isoformat():<12}  {bn:>10.0f}  {gn:>10.0f}  {bn - gn:>+10.0f}  {bcnt:>6}")

    # exit reasons distribution на 90d
    out_lines.append("")
    out_lines.append("─" * 92)
    out_lines.append(" Exit reasons distribution (90d in-sample)")
    out_lines.append("─" * 92)
    all_reasons = sorted(set(
        r for tag in is90 for r in is90[tag].get("_reasons", {})
    ))
    out_lines.append(f" {'reason':<14}  {'BASE×CANON':>11}  {'GUARD×CANON':>11}  "
                     f"{'BASE×LIVE':>11}  {'GUARD×LIVE':>11}")
    for r in all_reasons:
        cells = []
        for tag in ("baseline_canon", "guard_canon", "baseline_live", "guard_live"):
            cells.append(f"{is90[tag].get('_reasons', {}).get(r, 0):>11}")
        out_lines.append(f" {r:<14}  {cells[0]}  {cells[1]}  {cells[2]}  {cells[3]}")

    # decision
    decision, notes = _decision_for(is90, oos30, wf_guard)
    out_lines.append("")
    out_lines.append("═" * 92)
    out_lines.append(f" РЕШЕНИЕ: {decision}")
    out_lines.append("═" * 92)
    out_lines.extend(notes)

    # write
    out_path = Path(args.out_dir) / "gold_orb_session_guard_out.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write("\n".join(out_lines) + "\n")
    log.info("Отчёт: %s", out_path)

    print("\n".join(out_lines))


if __name__ == "__main__":
    main()
