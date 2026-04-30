#!/usr/bin/env python3
"""Аудит shadow-фильтров F1 + F2 (gold_orb) по live-сделкам.

Реконструирует shadow-вердикт для каждой gold_orb позиции, открытой
после deploy `2fb0b65` (29.04.2026 17:32 UTC). Сравнивает «что бы
сказал фильтр» с реальным P&L. Цель — данные для будущего решения
о включении F1/F2 как hard filter (по `sample-size.mdc`).

Не меняет торговую логику. Только observability/audit.

Запуск:
    PYTHONPATH=src python3 -m scripts.audit_gold_orb_f1_f2_shadow \\
        --db /tmp/advisor_stats.sqlite \\
        --m5 data/fxpro_klines/GC_F_M5_recent.csv \\
        --since 2026-04-29T17:32:00+00:00
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr
from fx_pro_bot.config.settings import pip_size
from fx_pro_bot.market_data.models import Bar, InstrumentId
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
    created_at: datetime
    direction: str
    session: str
    profit_pips: float
    atr_pips: float
    box_high: float
    box_low: float
    break_distance_atr: float
    bars_since_box_end: int
    f1_status: str            # ok | BLOCK
    f2_status: str            # ok | BLOCK


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
    bars: list[Bar], created_at: datetime, direction: str, session: str, ps: float,
) -> tuple[float, int, float, float, float]:
    """Реконструирует (break_dist_atr, bars_since_box_end, box_high,
    box_low, atr_v) для позиции открытой в `created_at`.

    Алгоритм:
    1. Берём session_bars сегодняшнего дня от session_start (08:00/14:30).
    2. Box по первым ORB_BARS=3 свечам.
    3. Ищем earliest M5 после box_end (08:15/14:45) и до created_at где
       last.high > box_high (long) / last.low < box_low (short) — это
       и есть бар пробоя, который видел бот. Эта логика повторяет
       `_check_orb` (gold_orb.py).
    """
    session_start_t = LONDON_OPEN if session == "london" else NY_OPEN
    session_close_t = LONDON_CLOSE if session == "london" else NY_CLOSE
    box_end_t = LONDON_ORB_END if session == "london" else NY_ORB_END

    today = created_at.date()
    today_session_bars = [
        b for b in bars
        if b.ts.date() == today and session_start_t <= b.ts.time() < session_close_t
    ]
    if len(today_session_bars) < ORB_BARS + 1:
        return 0.0, 0, 0.0, 0.0, 0.0

    box_high, box_low = session_range(today_session_bars, ORB_BARS)
    if box_high == 0 or box_low == 0:
        return 0.0, 0, 0.0, 0.0, 0.0

    # ATR как gold_orb считает: на 50+ M5 барах перед пробоем (LIVE_WINDOW).
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
        return 0.0, 0, box_high, box_low, 0.0

    # last_bar — последний полностью закрытый M5-бар перед created_at.
    # Бот в gold_orb видит именно этот бар как `bars[-1]` в момент scan.
    # Бар [ts, ts+5min). Закрыт если ts+5min ≤ created_at.
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
        return 0.0, 0, box_high, box_low, atr_v

    if direction == "long" and breakout_bar.high <= box_high:
        return 0.0, 0, box_high, box_low, atr_v
    if direction == "short" and breakout_bar.low >= box_low:
        return 0.0, 0, box_high, box_low, atr_v

    if direction == "long":
        break_dist = breakout_bar.high - box_high
    else:
        break_dist = box_low - breakout_bar.low

    break_dist_atr = break_dist / atr_v if atr_v > 0 else 0.0

    box_end_dt = datetime.combine(today, box_end_t, tzinfo=UTC)
    bars_since = max(
        0, int((breakout_bar.ts - box_end_dt).total_seconds() // 300),
    )

    return break_dist_atr, bars_since, box_high, box_low, atr_v


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/tmp/advisor_stats.sqlite")
    p.add_argument("--m5", default="data/fxpro_klines/GC_F_M5_recent.csv")
    p.add_argument("--since", default="2026-04-29T17:32:00+00:00")
    p.add_argument("--out", default="data/gold_orb_f1_f2_shadow_audit.csv")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    bars = load_m5_bars(Path(args.m5))
    log.info(
        "M5: %d баров, %s → %s",
        len(bars), bars[0].ts, bars[-1].ts,
    )
    ps = pip_size("GC=F")

    since_dt = datetime.fromisoformat(args.since)
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT created_at, direction, profit_pips FROM positions "
        "WHERE strategy='gold_orb' AND status='closed' "
        "  AND created_at >= ? "
        "ORDER BY created_at",
        (args.since,),
    ).fetchall()
    log.info("DB: %d gold_orb сделок после %s", len(rows), args.since)

    audits: list[TradeAudit] = []
    sl_block_state: dict[tuple, bool] = {}

    for created_at_str, direction, profit_pips in rows:
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        session = _classify_session(created_at)
        if session == "off":
            continue

        break_dist_atr, bars_since, bh, bl, atr_v = _compute_shadow_metrics(
            bars, created_at, direction, session, ps,
        )

        f1 = "ok" if break_dist_atr >= SHADOW_F1_MIN_BREAK_ATR else "BLOCK"
        key = (created_at.date(), session, direction)
        f2 = "BLOCK" if sl_block_state.get(key, False) else "ok"
        if profit_pips < 0:
            sl_block_state[key] = True

        atr_pips = atr_v / ps if ps > 0 else 0.0

        audits.append(TradeAudit(
            created_at=created_at,
            direction=direction,
            session=session,
            profit_pips=round(profit_pips, 1),
            atr_pips=round(atr_pips, 1),
            box_high=round(bh, 2),
            box_low=round(bl, 2),
            break_distance_atr=round(break_dist_atr, 2),
            bars_since_box_end=bars_since,
            f1_status=f1,
            f2_status=f2,
        ))

    if not audits:
        log.warning("Не найдено сделок для аудита")
        return

    print("\n" + "═" * 110)
    print(" AUDIT shadow F1+F2: gold_orb после deploy 29.04 17:32 UTC")
    print("═" * 110)
    print(
        f" {'created_at':<26}  {'dir':<5}  {'sess':<7}  "
        f"{'P&L_p':>7}  {'ATR_p':>7}  "
        f"{'brk_ATR':>8}  {'box_age':>7}  {'F1':>5}  {'F2':>5}"
    )
    print(" " + "─" * 100)
    for a in audits:
        print(
            f" {a.created_at.isoformat():<26}  {a.direction:<5}  {a.session:<7}  "
            f"{a.profit_pips:>+7.1f}  {a.atr_pips:>7.1f}  "
            f"{a.break_distance_atr:>8.2f}  {a.bars_since_box_end:>7d}  "
            f"{a.f1_status:>5}  {a.f2_status:>5}"
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
    print(f"   Реально (без фильтров): NET = {actual_net:>+7.1f}p  (n={n})")
    n1 = sum(1 for a in audits if a.f1_status == "ok")
    n2 = sum(1 for a in audits if a.f2_status == "ok")
    nb = sum(
        1 for a in audits
        if a.f1_status == "ok" and a.f2_status == "ok"
    )
    print(f"   С F1 (block <0.3 ATR): NET = {if_f1:>+7.1f}p  (n={n1})  Δ={if_f1 - actual_net:>+7.1f}p")
    print(f"   С F2 (sl_cooldown):    NET = {if_f2:>+7.1f}p  (n={n2})  Δ={if_f2 - actual_net:>+7.1f}p")
    print(f"   С F1+F2:               NET = {if_both:>+7.1f}p  (n={nb})  Δ={if_both - actual_net:>+7.1f}p")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "created_at", "direction", "session", "profit_pips",
            "atr_pips", "box_high", "box_low", "break_distance_atr",
            "bars_since_box_end", "f1_status", "f2_status",
        ])
        w.writeheader()
        for a in audits:
            w.writerow({
                "created_at": a.created_at.isoformat(),
                "direction": a.direction,
                "session": a.session,
                "profit_pips": a.profit_pips,
                "atr_pips": a.atr_pips,
                "box_high": a.box_high,
                "box_low": a.box_low,
                "break_distance_atr": a.break_distance_atr,
                "bars_since_box_end": a.bars_since_box_end,
                "f1_status": a.f1_status,
                "f2_status": a.f2_status,
            })
    print(f"\n Per-trade CSV: {out_path}")
    print(
        " Sample size: n={} (≪100 по `sample-size.mdc` — только observation)".format(n)
    )
    print("═" * 110)


if __name__ == "__main__":
    main()
