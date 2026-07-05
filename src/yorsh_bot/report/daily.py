"""Ежедневная сводка ёрш-сканера (M5).

CLI: ``python -m yorsh_bot.report.daily [--date YYYY-MM-DD]``.
Сводка за день: active-кандидаты, прострелов/сутки, медианная амплитуда,
медианное время до отката (revert_ms), теоретический P&L как **UPPER BOUND**
(без exit-slippage — аудит п.3а; явно помечаем в выводе).

Теоретический P&L: для каждого прострела, прошедшего фильтры, upper-bound
profit = amplitude_pct × notional. Не учитывает exit-slippage в дырявом
стакане, fees, funding, market-impact. Это ceiling, не прогноз.
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys

from yorsh_bot.config.settings import load_settings
from yorsh_bot.state.db import YorshDB

# Условный notional на прострел для upper-bound P&L ($). Стартовая точка
# для оценки ceiling; реальный sizing — Фаза 2 после калибровки.
NOTIONAL_USD = 10.0


def _day_bounds(date_str: str | None) -> tuple[float, float, str]:
    if date_str:
        d = dt.date.fromisoformat(date_str)
    else:
        d = dt.date.today() - dt.timedelta(days=1)   # вчерашний день по умолчанию
    start = dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    return start.timestamp(), end.timestamp(), d.isoformat()


def build_report(db: YorshDB, *, date_str: str | None = None,
                 notional_usd: float = NOTIONAL_USD) -> str:
    start_ts, end_ts, label = _day_bounds(date_str)
    lines: list[str] = []
    lines.append(f"=== ёрш daily report: {label} (UTC) ===")
    lines.append("")

    # кандидаты
    cands = db.active_candidates()
    lines.append(f"Active candidates: {len(cands)}")
    for c in cands:
        lines.append(
            f"  {c['exchange']:6s} {c['symbol']:12s} "
            f"spurts/day={c['spurts_per_day']:.2f} "
            f"p={c['regularity_pvalue'] if c['regularity_pvalue'] is not None else float('nan'):.4f} "
            f"cluster=${c['print_cluster_size']}")
    lines.append("")

    # прострелы за день
    rows = db.conn.execute(
        "SELECT * FROM spurt_events WHERE ts>=? AND ts<? ORDER BY ts",
        (start_ts, end_ts)).fetchall()
    n = len(rows)
    passed = [r for r in rows if r["passed_filters"]]
    lines.append(f"Spurts total: {n}  (passed filters: {len(passed)})")
    if rows:
        amps = [r["amplitude_pct"] for r in rows]
        reverts = [r["revert_ms"] for r in rows if r["revert_ms"] is not None]
        lines.append(f"  median amplitude: {statistics.median(amps):.3f}%")
        if reverts:
            lines.append(f"  median revert_ms: {statistics.median(reverts):.0f}ms")
        else:
            lines.append("  median revert_ms: n/a (no reverts recorded)")
    lines.append("")

    # upper-bound P&L (только passed)
    ub = sum(r["amplitude_pct"] / 100.0 * notional_usd for r in passed)
    lines.append(f"Theoretical P&L (UPPER BOUND, no exit-slippage): ${ub:.2f}")
    lines.append("  ⚠ ceiling only — assumes perfect exit at start price,")
    lines.append("    ignores slippage/fees/funding/impact (audit п.3а).")
    lines.append("  (assumed notional ${:.0f}/spurt — sizing is Фаза 2)".format(notional_usd))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="yorsh daily report")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: yesterday UTC)")
    ap.add_argument("--notional", type=float, default=NOTIONAL_USD,
                    help="assumed notional per spurt for upper-bound P&L")
    args = ap.parse_args(argv)
    cfg = load_settings()
    db = YorshDB(cfg.data_dir)
    try:
        print(build_report(db, date_str=args.date, notional_usd=args.notional))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
