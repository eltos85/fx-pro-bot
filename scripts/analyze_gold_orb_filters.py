#!/usr/bin/env python3
"""OOS-проверка 3-х фильтров качества для `gold_orb` (29.04.2026).

Обоснование — разбор сегодняшней live-сессии (London 29.04, 5 шортов в
один box, day-net −62 pip, см. BUILDLOG.md). Гипотезы трейдера:

    F1. min_break_atr — не входить, если пробой box-границы < N ATR
        (отрезает шумовые тык-возвраты).
    F2. sl_cooldown — после первого SL в текущей сессии в направлении X
        новые сигналы X в этой сессии блокируются (стоп-после-стопа).
    F3. level_proximity — не шортить ближе K pip к вчерашнему low,
        не лонговать ближе K pip к вчерашнему high (поддержка/сопрот).

Скрипт **только аналитический** — не меняет торговую логику.
Прогоняется на 90d in-sample (28.01–28.04) + fresh 30d OOS (28.12–28.01),
плюс отдельный replay сегодняшней сессии 29.04 из 122d data.

Запуск:
    PYTHONPATH=src python3 -m scripts.analyze_gold_orb_filters
"""

from __future__ import annotations

import argparse
import logging
import statistics
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx
from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar

from scripts.analyze_gold_orb_session_guard import (
    _load_bars_csv, _slice_bars,
)
from scripts.analyze_gold_orb_trail_compare import (
    TradeResult,
    _simulate_canon as _sim_canon_inner,
)
from scripts.backtest_fxpro_all import LIVE_WINDOW_BARS, MAX_HOLD_BARS
from scripts.backtest_fxpro_candidates import (
    GOLD_ORB_ADX_MAX, GOLD_ORB_SL_ATR, GOLD_ORB_TP_ATR,
    LONDON_CLOSE, LONDON_ORB_END, NY_CLOSE, NY_ORB_END,
    _session_box_gold,
)
from fx_pro_bot.strategies.scalping.indicators import ema_slope

log = logging.getLogger("analyze_gold_orb_filters")


@dataclass
class FilterConfig:
    label: str
    min_break_atr: float = 0.0      # F1: 0 = выкл
    sl_cooldown: bool = False       # F2: после SL в направлении блок
    level_prox_pips: float = 0.0    # F3: 0 = выкл


# ─── pre-compute previous-day H/L для F3 ─────────────────────────────


def _build_prev_day_levels(bars: list[Bar]) -> dict[date, tuple[float, float]]:
    """Возвращает dict day → (prev_high, prev_low). Учитывает только торговые дни."""
    by_day: dict[date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.ts.date(), []).append(b)
    days = sorted(by_day)
    prev: dict[date, tuple[float, float]] = {}
    for i, d in enumerate(days):
        if i == 0:
            continue
        p = days[i - 1]
        bs = by_day[p]
        if not bs:
            continue
        prev[d] = (max(b.high for b in bs), min(b.low for b in bs))
    return prev


# ─── проверка сигнала с применением фильтров ─────────────────────────


@dataclass
class Signal:
    idx: int
    direction: str
    entry_price: float
    sl: float
    tp: float
    atr_v: float
    session: str
    ts: datetime
    break_atr: float
    blocked: str = ""   # "" = принят, иначе — причина блока


def _check_signal(
    bars: list[Bar],
    i: int,
    cfg: FilterConfig,
    prev_levels: dict[date, tuple[float, float]],
    sl_session_dir_blocked: set[tuple[date, str, str]],
    ps: float,
) -> Signal | None:
    """Проверка сигнала. Возвращает Signal только если все фильтры пройдены."""
    last = bars[i]
    t = last.ts.time()
    if LONDON_ORB_END <= t < LONDON_CLOSE:
        session = "london"
    elif NY_ORB_END <= t < NY_CLOSE:
        session = "ny"
    else:
        return None

    window = bars[i - LIVE_WINDOW_BARS: i + 1]
    atr_v = _atr(window)
    if atr_v <= 0 or compute_adx(window) > GOLD_ORB_ADX_MAX:
        return None

    box = _session_box_gold(window, last.ts)
    if box is None:
        return None
    box_high, box_low, _ = box

    if last.high > box_high:
        direction = "long"
        entry = box_high
        break_atr = (last.high - box_high) / atr_v
    elif last.low < box_low:
        direction = "short"
        entry = box_low
        break_atr = (box_low - last.low) / atr_v
    else:
        return None

    closes = [b.close for b in window]
    slope = ema_slope(_ema(closes, 50), 5)
    if direction == "long" and slope < 0:
        return None
    if direction == "short" and slope > 0:
        return None

    # ── F1: min_break_atr ─────────────────────────────────────────
    if cfg.min_break_atr > 0 and break_atr < cfg.min_break_atr:
        return None

    # ── F2: sl_cooldown ───────────────────────────────────────────
    if cfg.sl_cooldown and (last.ts.date(), session, direction) in sl_session_dir_blocked:
        return None

    # ── F3: level_proximity ───────────────────────────────────────
    if cfg.level_prox_pips > 0:
        lvls = prev_levels.get(last.ts.date())
        if lvls is not None:
            prev_high, prev_low = lvls
            cur_price = last.close
            if direction == "short":
                # шорт около вчерашнего low (поддержка) — блок
                dist_pips = (cur_price - prev_low) / ps
                if 0 <= dist_pips < cfg.level_prox_pips:
                    return None
            else:
                # лонг около вчерашнего high (сопротивление) — блок
                dist_pips = (prev_high - cur_price) / ps
                if 0 <= dist_pips < cfg.level_prox_pips:
                    return None

    if direction == "long":
        sl = entry - GOLD_ORB_SL_ATR * atr_v
        tp = entry + GOLD_ORB_TP_ATR * atr_v
    else:
        sl = entry + GOLD_ORB_SL_ATR * atr_v
        tp = entry - GOLD_ORB_TP_ATR * atr_v

    return Signal(
        idx=i, direction=direction, entry_price=entry,
        sl=sl, tp=tp, atr_v=atr_v, session=session, ts=last.ts,
        break_atr=round(break_atr, 2),
    )


def simulate(
    bars: list[Bar],
    cfg: FilterConfig,
    spread: float,
    ps: float,
) -> list[TradeResult]:
    prev_levels = _build_prev_day_levels(bars)
    trades: list[TradeResult] = []
    active_until = -1
    sl_blocked: set[tuple[date, str, str]] = set()

    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= active_until:
            continue
        sig = _check_signal(bars, i, cfg, prev_levels, sl_blocked, ps)
        if sig is None:
            continue
        tr = _sim_canon_inner(
            bars, sig.idx, sig.direction, sig.entry_price,
            sig.sl, sig.tp, sig.atr_v, spread, ps,
        )
        if tr is None:
            continue
        trades.append(tr)
        active_until = sig.idx + tr.bars_held
        if cfg.sl_cooldown and tr.reason == "sl":
            sl_blocked.add((sig.ts.date(), sig.session, sig.direction))
    return trades


# ─── метрики ─────────────────────────────────────────────────────────


def _summary(trades: list[TradeResult]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "net": 0.0, "pf": 0.0, "sharpe": 0.0, "maxdd": 0.0}
    nets = [t.net_pips for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gp = sum(wins)
    gl = -sum(losses) or 1e-9
    cum = 0.0; peak = 0.0; dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    sd = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    sh = (statistics.mean(nets) / sd) * math.sqrt(len(nets)) if sd > 0 else 0.0
    return {
        "n": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "net": round(sum(nets), 0),
        "pf": round(gp / gl, 2),
        "sharpe": round(sh, 2),
        "maxdd": round(dd, 0),
    }


# ─── main ────────────────────────────────────────────────────────────


CONFIGS = [
    FilterConfig(label="BASELINE"),
    FilterConfig(label="F1 break>=0.3", min_break_atr=0.3),
    FilterConfig(label="F1 break>=0.5", min_break_atr=0.5),
    FilterConfig(label="F2 sl_cooldown", sl_cooldown=True),
    FilterConfig(label="F3 level 50pip", level_prox_pips=50),
    FilterConfig(label="F3 level 100pip", level_prox_pips=100),
    FilterConfig(label="F1+F2", min_break_atr=0.3, sl_cooldown=True),
    FilterConfig(label="F1+F2+F3", min_break_atr=0.3, sl_cooldown=True, level_prox_pips=50),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--in-sample", default="data/fxpro_klines/GC_F_M5.csv")
    p.add_argument("--full", default="data/fxpro_klines/GC_F_M5_122d.csv")
    p.add_argument("--oos-end", default="2026-01-28T11:40:00+00:00")
    p.add_argument("--out", default="data/gold_orb_filters_out.txt")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    bars_is = _load_bars_csv(Path(args.in_sample))
    bars_full = _load_bars_csv(Path(args.full))
    oos_end = datetime.fromisoformat(args.oos_end.replace("Z", "+00:00"))
    bars_oos = _slice_bars(bars_full, None, oos_end)

    log.info("In-sample: %d баров (%s → %s)", len(bars_is),
             bars_is[0].ts.date(), bars_is[-1].ts.date())
    log.info("Fresh OOS: %d баров (%s → %s)", len(bars_oos),
             bars_oos[0].ts.date(), bars_oos[-1].ts.date())

    out_lines = []
    out_lines.append("═" * 92)
    out_lines.append(" gold_orb FILTERS analysis (analyze_gold_orb_filters.py)")
    out_lines.append("═" * 92)
    out_lines.append(" CANON exit only (ATR SL=1.5×, TP=3.0×, time-stop 6h)")
    out_lines.append("")

    for label, bars, period_name in [
        ("In-sample 90d", bars_is, "90d in-sample"),
        ("Fresh OOS 30d", bars_oos, "30d OOS"),
    ]:
        out_lines.append("─" * 92)
        out_lines.append(f" {label} ({bars[0].ts.date()} → {bars[-1].ts.date()})")
        out_lines.append("─" * 92)
        out_lines.append(f" {'config':<22}  {'n':>4}  {'WR%':>6}  {'NET':>9}  "
                         f"{'PF':>6}  {'Sharpe':>7}  {'maxDD':>8}")
        out_lines.append(" " + "─" * 70)
        for cfg in CONFIGS:
            trs = simulate(bars, cfg, spread, ps)
            s = _summary(trs)
            out_lines.append(
                f" {cfg.label:<22}  {s['n']:>4}  {s['wr']:>6}  {s['net']:>+9.0f}  "
                f"{s['pf']:>6}  {s['sharpe']:>7}  {s['maxdd']:>+8.0f}"
            )
        out_lines.append("")

    # ── walk-forward T1/T2/T3 для каждой конфигурации (90d) ──────────
    out_lines.append("─" * 92)
    out_lines.append(" Walk-forward на 90d in-sample (T1/T2/T3 — равные трети по числу trades)")
    out_lines.append("─" * 92)
    out_lines.append(f" {'config':<22}  {'T1 NET':>8}  {'T1 PF':>6}  {'T1 DD':>7}  "
                     f"{'T2 NET':>8}  {'T2 PF':>6}  {'T2 DD':>7}  "
                     f"{'T3 NET':>8}  {'T3 PF':>6}  {'T3 DD':>7}")
    out_lines.append(" " + "─" * 88)
    wf_configs = [c for c in CONFIGS
                  if c.label in ("BASELINE", "F1 break>=0.3", "F1 break>=0.5",
                                 "F2 sl_cooldown", "F1+F2")]
    for cfg in wf_configs:
        trs = simulate(bars_is, cfg, spread, ps)
        trs_sorted = sorted(trs, key=lambda t: t.entry_ts)
        n = len(trs_sorted)
        if n < 3:
            continue
        chunk = n // 3
        thirds = [trs_sorted[:chunk], trs_sorted[chunk:2*chunk], trs_sorted[2*chunk:]]
        cells = []
        for t in thirds:
            s = _summary(t)
            cells.extend([s["net"], s["pf"], s["maxdd"]])
        out_lines.append(
            f" {cfg.label:<22}  "
            f"{cells[0]:>+8.0f}  {cells[1]:>6}  {cells[2]:>+7.0f}  "
            f"{cells[3]:>+8.0f}  {cells[4]:>6}  {cells[5]:>+7.0f}  "
            f"{cells[6]:>+8.0f}  {cells[7]:>6}  {cells[8]:>+7.0f}"
        )
    out_lines.append("")

    # ── replay сегодняшней London-сессии (29.04) ─────────────────────
    out_lines.append("─" * 92)
    out_lines.append(" REPLAY 29.04.2026 London (08:15–12:00 UTC) — какие сигналы прошли каждый фильтр")
    out_lines.append("─" * 92)

    today = date(2026, 4, 29)
    today_bars = [b for b in bars_full
                  if b.ts.date() <= today]
    prev_levels = _build_prev_day_levels(today_bars)

    out_lines.append(f" {'config':<22}  {'signals':>7}  {'P&L pip':>9}  {'detail':<40}")
    out_lines.append(" " + "─" * 80)
    for cfg in CONFIGS:
        trs = simulate(today_bars, cfg, spread, ps)
        # фильтруем только сегодняшние трейды
        today_trs = [t for t in trs if t.entry_ts.date() == today]
        net = sum(t.net_pips for t in today_trs)
        det = ", ".join(
            f"{t.direction[0].upper()}@{t.entry_price:.0f}→{t.net_pips:+.0f}"
            for t in today_trs
        )
        out_lines.append(
            f" {cfg.label:<22}  {len(today_trs):>7}  {net:>+9.0f}  {det:<40}"
        )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    log.info("Отчёт: %s", args.out)
    print("\n".join(out_lines))


if __name__ == "__main__":
    main()
