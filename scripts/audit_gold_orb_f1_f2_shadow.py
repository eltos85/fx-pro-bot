#!/usr/bin/env python3
"""Аудит shadow-фильтров F1 + F2 (gold_orb) по live-сделкам.

Источник правды — таблица `position_diagnostics` (deploy `7a6786d`,
30.04.2026). Для каждой `gold_orb` сделки читаем сохранённые в момент
открытия `shadow_f1_status / shadow_f2_status / break_distance_atr /
bars_since_box_end / atr_at_open_pips`, плюс close-метрики
(`peak_pips / tp_target_pips / trail_*` / shadow_intrabar). Сравниваем
с реальным `profit_pips` из `positions`.

Если по позиции нет diag-записи (исторические сделки, открытые до
deploy diagnostics-persistence) — fallback к реконструкции из M5
с пометкой `[recon]`. Это используется для backfill отдельным шагом.

Не меняет торговую логику. Только observability/audit.

Запуск:
    PYTHONPATH=src python3 -m scripts.audit_gold_orb_f1_f2_shadow \\
        --db /tmp/advisor_stats.sqlite \\
        --m5 data/fxpro_klines/GC_F_M5_recent.csv \\
        --since 2026-04-27T18:00:00+00:00
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr
from fx_pro_bot.config.settings import pip_size
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.gold_orb import (
    LONDON_CLOSE,
    LONDON_OPEN,
    LONDON_ORB_END,
    NY_CLOSE,
    NY_OPEN,
    NY_ORB_END,
    ORB_BARS,
    SHADOW_F1_MIN_BREAK_ATR,
)
from fx_pro_bot.strategies.scalping.indicators import session_range


log = logging.getLogger("audit_gold_orb_f1_f2_shadow")


@dataclass
class TradeAudit:
    position_id: str
    created_at: datetime
    direction: str
    session: str
    profit_pips: float
    atr_pips: float
    break_distance_atr: float
    bars_since_box_end: int
    f1_status: str            # ok | BLOCK
    f2_status: str            # ok | BLOCK
    source: str               # db | recon
    peak_pips: float | None = None
    tp_target_pips: float | None = None
    trail_trigger_pips: float | None = None
    trail_distance_pips: float | None = None
    shadow_intrabar_triggered: int | None = None
    shadow_intrabar_peak_pips: float | None = None


def load_m5_bars(path: Path) -> list[Bar]:
    instr = InstrumentId(symbol="GC=F")
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


def _classify_session(ts: datetime) -> str:
    """Классификация по `last_bar.ts` стратегии (~M5 close 5 минут до created_at)."""
    t = (ts - timedelta(minutes=5)).time()
    if LONDON_OPEN <= t < LONDON_CLOSE:
        return "london"
    if NY_OPEN <= t < NY_CLOSE:
        return "ny"
    return "off"


def _compute_shadow_metrics(
    bars: list[Bar], created_at: datetime, direction: str, session: str,
) -> tuple[float, int, float]:
    """Реконструирует (break_dist_atr, bars_since_box_end, atr_v) для
    позиции, открытой в `created_at`. Используется только для сделок
    без diag-записи (fallback)."""
    session_start_t = LONDON_OPEN if session == "london" else NY_OPEN
    session_close_t = LONDON_CLOSE if session == "london" else NY_CLOSE
    box_end_t = LONDON_ORB_END if session == "london" else NY_ORB_END

    today = created_at.date()
    today_session_bars = [
        b for b in bars
        if b.ts.date() == today and session_start_t <= b.ts.time() < session_close_t
    ]
    if len(today_session_bars) < ORB_BARS + 1:
        return 0.0, 0, 0.0

    box_high, box_low = session_range(today_session_bars, ORB_BARS)
    if box_high == 0 or box_low == 0:
        return 0.0, 0, 0.0

    cutoff_idx = -1
    for i, b in enumerate(bars):
        if b.ts > created_at:
            cutoff_idx = i - 1
            break
    if cutoff_idx < 0:
        cutoff_idx = len(bars) - 1
    atr_window = bars[max(0, cutoff_idx - 50):cutoff_idx + 1]
    atr_v = _atr(atr_window) if len(atr_window) >= 14 else 0.0
    if atr_v <= 0:
        return 0.0, 0, 0.0

    breakout_bar: Bar | None = None
    for b in reversed(today_session_bars):
        if b.ts.time() < box_end_t:
            continue
        bar_close_ts = b.ts.replace(tzinfo=UTC) if b.ts.tzinfo is None else b.ts
        if (created_at - bar_close_ts).total_seconds() < 300:
            continue
        breakout_bar = b
        break

    if breakout_bar is None:
        return 0.0, 0, atr_v
    if direction == "long" and breakout_bar.high <= box_high:
        return 0.0, 0, atr_v
    if direction == "short" and breakout_bar.low >= box_low:
        return 0.0, 0, atr_v

    if direction == "long":
        break_dist = breakout_bar.high - box_high
    else:
        break_dist = box_low - breakout_bar.low
    break_dist_atr = break_dist / atr_v if atr_v > 0 else 0.0

    box_end_dt = datetime.combine(today, box_end_t, tzinfo=UTC)
    bars_since = max(
        0, int((breakout_bar.ts - box_end_dt).total_seconds() // 300),
    )
    return break_dist_atr, bars_since, atr_v


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/tmp/advisor_stats.sqlite")
    p.add_argument("--m5", default="data/fxpro_klines/GC_F_M5_recent.csv")
    p.add_argument("--since", default="2026-04-27T18:00:00+00:00")
    p.add_argument("--out", default="data/gold_orb_f1_f2_shadow_audit.csv")
    p.add_argument(
        "--no-fallback", action="store_true",
        help="Не реконструировать F1/F2 для сделок без diag-записи",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    bars = load_m5_bars(Path(args.m5)) if not args.no_fallback else []
    if bars:
        log.info("M5: %d баров, %s → %s", len(bars), bars[0].ts, bars[-1].ts)
    ps = pip_size("GC=F")

    # Гарантируем что миграция position_diagnostics применена к локальной БД
    # (если запускаем по копии, в которой ещё нет таблицы).
    StatsStore(Path(args.db))

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT
            p.id, p.created_at, p.direction, p.profit_pips,
            d.shadow_f1_status, d.shadow_f2_status,
            d.break_distance_atr, d.bars_since_box_end,
            d.atr_at_open_pips,
            d.peak_pips, d.tp_target_pips,
            d.trail_trigger_pips, d.trail_distance_pips,
            d.shadow_intrabar_triggered, d.shadow_intrabar_peak_pips
        FROM positions p
        LEFT JOIN position_diagnostics d ON d.position_id = p.id
        WHERE p.strategy='gold_orb' AND p.status='closed'
          AND p.created_at >= ?
        ORDER BY p.created_at
        """,
        (args.since,),
    ).fetchall()
    log.info("DB: %d gold_orb сделок после %s", len(rows), args.since)

    audits: list[TradeAudit] = []
    sl_block_state: dict[tuple, bool] = {}
    n_db = n_recon = n_skip = 0

    for row in rows:
        (
            pid, created_at_str, direction, profit_pips,
            d_f1, d_f2, d_break_atr, d_bars_since, d_atr_open,
            d_peak, d_tp, d_trig, d_dist, d_sh_trig, d_sh_peak,
        ) = row
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        session = _classify_session(created_at)
        if session == "off":
            continue

        if d_f1 is not None and d_f2 is not None:
            f1 = d_f1
            f2 = d_f2
            break_dist_atr = float(d_break_atr or 0.0)
            bars_since = int(d_bars_since or 0)
            atr_pips = float(d_atr_open or 0.0)
            source = "db"
            n_db += 1
        elif args.no_fallback or not bars:
            n_skip += 1
            continue
        else:
            break_dist_atr, bars_since, atr_v = _compute_shadow_metrics(
                bars, created_at, direction, session,
            )
            f1 = "ok" if break_dist_atr >= SHADOW_F1_MIN_BREAK_ATR else "BLOCK"
            key = (created_at.date(), session, direction)
            f2 = "BLOCK" if sl_block_state.get(key, False) else "ok"
            atr_pips = atr_v / ps if ps > 0 else 0.0
            source = "recon"
            n_recon += 1

        # F2-state поддерживаем для recon-fallback (db уже содержит свой f2).
        key = (created_at.date(), session, direction)
        if profit_pips is not None and profit_pips < 0:
            sl_block_state[key] = True

        audits.append(TradeAudit(
            position_id=pid,
            created_at=created_at,
            direction=direction,
            session=session,
            profit_pips=round(profit_pips or 0.0, 1),
            atr_pips=round(atr_pips, 1),
            break_distance_atr=round(break_dist_atr, 2),
            bars_since_box_end=bars_since,
            f1_status=f1,
            f2_status=f2,
            source=source,
            peak_pips=round(d_peak, 1) if d_peak is not None else None,
            tp_target_pips=round(d_tp, 1) if d_tp is not None else None,
            trail_trigger_pips=round(d_trig, 1) if d_trig is not None else None,
            trail_distance_pips=round(d_dist, 1) if d_dist is not None else None,
            shadow_intrabar_triggered=d_sh_trig,
            shadow_intrabar_peak_pips=round(d_sh_peak, 1) if d_sh_peak is not None else None,
        ))

    if not audits:
        log.warning("Не найдено сделок для аудита")
        return

    log.info(
        "Источники: db=%d, recon=%d, skip=%d (всего audit=%d)",
        n_db, n_recon, n_skip, len(audits),
    )

    print("\n" + "═" * 130)
    print(" AUDIT shadow F1+F2: gold_orb (DB-первый источник, recon — fallback)")
    print("═" * 130)
    print(
        f" {'created_at':<26}  {'src':<5}  {'dir':<5}  {'sess':<7}  "
        f"{'P&L_p':>7}  {'ATR_p':>6}  "
        f"{'brk_ATR':>8}  {'box_age':>7}  "
        f"{'F1':>5}  {'F2':>5}  {'peak':>6}  {'TP':>6}"
    )
    print(" " + "─" * 120)
    for a in audits:
        peak_s = f"{a.peak_pips:+.1f}" if a.peak_pips is not None else "  —  "
        tp_s = f"{a.tp_target_pips:.1f}" if a.tp_target_pips is not None else "  —  "
        print(
            f" {a.created_at.isoformat():<26}  {a.source:<5}  "
            f"{a.direction:<5}  {a.session:<7}  "
            f"{a.profit_pips:>+7.1f}  {a.atr_pips:>6.1f}  "
            f"{a.break_distance_atr:>8.2f}  {a.bars_since_box_end:>7d}  "
            f"{a.f1_status:>5}  {a.f2_status:>5}  {peak_s:>6}  {tp_s:>6}"
        )

    n = len(audits)
    by_f1 = {"ok": [], "BLOCK": []}
    by_f2 = {"ok": [], "BLOCK": []}
    by_both = {"ok-ok": [], "ok-BLOCK": [], "BLOCK-ok": [], "BLOCK-BLOCK": []}
    for a in audits:
        by_f1[a.f1_status].append(a)
        by_f2[a.f2_status].append(a)
        by_both[f"{a.f1_status}-{a.f2_status}"].append(a)

    print()
    print(" Сводка по F1 (min_break_atr ≥ 0.30):")
    for status, lst in by_f1.items():
        if lst:
            net = sum(a.profit_pips for a in lst)
            wins = sum(1 for a in lst if a.profit_pips > 0)
            print(
                f"   F1={status:<5}: n={len(lst):>2}  NET={net:>+8.1f}p  "
                f"WR={wins/len(lst)*100:>5.1f}%  avg={net/len(lst):>+7.1f}p"
            )

    print()
    print(" Сводка по F2 (sl_cooldown в той же session×direction):")
    for status, lst in by_f2.items():
        if lst:
            net = sum(a.profit_pips for a in lst)
            wins = sum(1 for a in lst if a.profit_pips > 0)
            print(
                f"   F2={status:<5}: n={len(lst):>2}  NET={net:>+8.1f}p  "
                f"WR={wins/len(lst)*100:>5.1f}%  avg={net/len(lst):>+7.1f}p"
            )

    print()
    print(" Кросс F1×F2:")
    for combo, lst in by_both.items():
        if lst:
            net = sum(a.profit_pips for a in lst)
            wins = sum(1 for a in lst if a.profit_pips > 0)
            print(
                f"   {combo:<13}: n={len(lst):>2}  NET={net:>+8.1f}p  "
                f"WR={wins/len(lst)*100:>5.1f}%"
            )

    print()
    print(" Что было бы при включении фильтров (block = пропустить сделку):")
    actual_net = sum(a.profit_pips for a in audits)
    if_f1 = sum(a.profit_pips for a in audits if a.f1_status == "ok")
    if_f2 = sum(a.profit_pips for a in audits if a.f2_status == "ok")
    if_both = sum(
        a.profit_pips for a in audits
        if a.f1_status == "ok" and a.f2_status == "ok"
    )
    n1 = sum(1 for a in audits if a.f1_status == "ok")
    n2 = sum(1 for a in audits if a.f2_status == "ok")
    nb = sum(
        1 for a in audits
        if a.f1_status == "ok" and a.f2_status == "ok"
    )
    print(f"   Реально (без фильтров): NET = {actual_net:>+7.1f}p  (n={n})")
    print(f"   С F1 (block <0.3 ATR): NET = {if_f1:>+7.1f}p  (n={n1})  Δ={if_f1 - actual_net:>+7.1f}p")
    print(f"   С F2 (sl_cooldown):    NET = {if_f2:>+7.1f}p  (n={n2})  Δ={if_f2 - actual_net:>+7.1f}p")
    print(f"   С F1+F2:               NET = {if_both:>+7.1f}p  (n={nb})  Δ={if_both - actual_net:>+7.1f}p")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "position_id", "created_at", "source", "direction", "session",
            "profit_pips", "atr_pips", "break_distance_atr",
            "bars_since_box_end", "f1_status", "f2_status",
            "peak_pips", "tp_target_pips",
            "trail_trigger_pips", "trail_distance_pips",
            "shadow_intrabar_triggered", "shadow_intrabar_peak_pips",
        ])
        w.writeheader()
        for a in audits:
            w.writerow({
                "position_id": a.position_id,
                "created_at": a.created_at.isoformat(),
                "source": a.source,
                "direction": a.direction,
                "session": a.session,
                "profit_pips": a.profit_pips,
                "atr_pips": a.atr_pips,
                "break_distance_atr": a.break_distance_atr,
                "bars_since_box_end": a.bars_since_box_end,
                "f1_status": a.f1_status,
                "f2_status": a.f2_status,
                "peak_pips": a.peak_pips,
                "tp_target_pips": a.tp_target_pips,
                "trail_trigger_pips": a.trail_trigger_pips,
                "trail_distance_pips": a.trail_distance_pips,
                "shadow_intrabar_triggered": a.shadow_intrabar_triggered,
                "shadow_intrabar_peak_pips": a.shadow_intrabar_peak_pips,
            })
    print(f"\n Per-trade CSV: {out_path}")
    print(
        " Sample size: n={} (≪100 по `sample-size.mdc` — только observation)".format(n)
    )
    print("═" * 130)


if __name__ == "__main__":
    main()
