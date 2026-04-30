#!/usr/bin/env python3
"""Разовый backfill `position_diagnostics` для исторических `gold_orb`
сделок (открытых ДО deploy `7a6786d` 30.04.2026 — когда стратегия
начала писать diag сама).

Реконструирует из M5-баров `shadow_f1_status / shadow_f2_status /
break_distance_atr / bars_since_box_end / atr_at_open_pips` для
каждой `gold_orb` позиции и пишет через `StatsStore.save_open_diagnostics`.

Close-метрики (`peak_pips / shadow_intrabar`) — runtime-only, их
backfill из БД не делаем (они появятся для новых сделок естественно).

`--dry-run` — показать что было бы записано без модификации БД.
`--apply`   — применить.

Запуск:
    PYTHONPATH=src python3 -m scripts.backfill_gold_orb_diagnostics \\
        --db /tmp/advisor_stats.sqlite \\
        --m5 data/fxpro_klines/GC_F_M5_recent.csv \\
        --since 2026-04-27T18:00:00+00:00 \\
        --apply
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
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


log = logging.getLogger("backfill_gold_orb_diag")


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
    t = (ts - timedelta(minutes=5)).time()
    if LONDON_OPEN <= t < LONDON_CLOSE:
        return "london"
    if NY_OPEN <= t < NY_CLOSE:
        return "ny"
    return "off"


def _compute_metrics(
    bars: list[Bar], created_at: datetime, direction: str, session: str,
) -> tuple[float, int, float]:
    """Возвращает (break_dist_atr, bars_since_box_end, atr_v).

    Логика повторяет `_check_orb` (gold_orb.py) — ищем последний
    закрытый M5 перед `created_at` в session×box-window.
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
    p.add_argument(
        "--apply", action="store_true",
        help="Применить backfill (по умолчанию — dry-run)",
    )
    p.add_argument(
        "--overwrite", action="store_true",
        help="Перезаписать diag даже если запись уже существует",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    bars = load_m5_bars(Path(args.m5))
    log.info("M5: %d баров, %s → %s", len(bars), bars[0].ts, bars[-1].ts)
    ps = pip_size("GC=F")

    store = StatsStore(Path(args.db))

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT
            p.id, p.created_at, p.direction, p.profit_pips,
            d.shadow_f1_status
        FROM positions p
        LEFT JOIN position_diagnostics d ON d.position_id = p.id
        WHERE p.strategy='gold_orb'
          AND p.created_at >= ?
        ORDER BY p.created_at
        """,
        (args.since,),
    ).fetchall()
    log.info("DB: %d gold_orb позиций после %s", len(rows), args.since)

    sl_block_state: dict[tuple, bool] = {}
    n_skip_off = n_skip_exists = n_apply = 0

    print()
    print(f" {'created_at':<26}  {'pid':>4}  {'dir':<5}  {'sess':<7}  "
          f"{'P&L_p':>7}  {'brk_ATR':>8}  {'box_age':>7}  "
          f"{'F1':>5}  {'F2':>5}  {'action':<10}")
    print(" " + "─" * 110)

    for pid, created_at_str, direction, profit_pips, existing_f1 in rows:
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        session = _classify_session(created_at)
        if session == "off":
            n_skip_off += 1
            continue

        if existing_f1 is not None and not args.overwrite:
            n_skip_exists += 1
            action = "skip-exists"
            print(
                f" {created_at.isoformat():<26}  {str(pid):>4}  "
                f"{direction:<5}  {session:<7}  "
                f"{(profit_pips or 0.0):>+7.1f}  "
                f"{'—':>8}  {'—':>7}  {'—':>5}  {'—':>5}  {action:<10}"
            )
            continue

        break_dist_atr, bars_since, atr_v = _compute_metrics(
            bars, created_at, direction, session,
        )
        f1 = "ok" if break_dist_atr >= SHADOW_F1_MIN_BREAK_ATR else "BLOCK"
        key = (created_at.date(), session, direction)
        f2 = "BLOCK" if sl_block_state.get(key, False) else "ok"
        if profit_pips is not None and profit_pips < 0:
            sl_block_state[key] = True

        atr_pips = atr_v / ps if ps > 0 else 0.0

        action = "APPLY" if args.apply else "dry-run"
        print(
            f" {created_at.isoformat():<26}  {str(pid):>4}  "
            f"{direction:<5}  {session:<7}  "
            f"{(profit_pips or 0.0):>+7.1f}  "
            f"{break_dist_atr:>8.2f}  {bars_since:>7d}  "
            f"{f1:>5}  {f2:>5}  {action:<10}"
        )

        if args.apply:
            store.save_open_diagnostics(
                pid,
                shadow_f1_status=f1,
                shadow_f2_status=f2,
                break_distance_atr=round(break_dist_atr, 4),
                bars_since_box_end=bars_since,
                atr_at_open_pips=round(atr_pips, 2),
            )
            n_apply += 1

    print()
    log.info(
        "Итого: skip_off_session=%d, skip_already_exists=%d, applied=%d, mode=%s",
        n_skip_off, n_skip_exists, n_apply,
        "APPLY" if args.apply else "DRY-RUN",
    )
    if not args.apply:
        log.info("Перезапусти с --apply для записи в БД")


if __name__ == "__main__":
    main()
