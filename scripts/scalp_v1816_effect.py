"""Эффект v0.18.16 на стратегии scalp_bot (мониторинг, НЕ выводы — sample-size).

Деплой v0.18.16: 2026-06-08 15:23 UTC (taker-вход + CVD/ob confirmation density_break).
Скрипт сравнивает ДО/ПОСЛЕ этой отсечки:
- fill-rate density_break (главный измеримый эффект #1 taker: maker-лимитка не
  наливалась на пробое; entry_Cancelled/timeout = непролив);
- per-strategy per-day закрытые сделки (WR, net) — справочно, выборка мала.

Read-only.
    docker exec fx-pro-bot-scalp-bot-1 python3 /tmp/scalp_v1816_effect.py
"""
import sqlite3
from datetime import datetime, timezone

DB = "/data/scalp_bot.sqlite"
CUT = datetime(2026, 6, 8, 15, 23, tzinfo=timezone.utc).timestamp()
NONFILL = ("entry_Cancelled", "entry_timeout", "entry_Rejected")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row


def fill_rate(strategy, lo, hi):
    rows = con.execute(
        "SELECT close_reason FROM trades WHERE strategy=? AND ts_open>=? AND ts_open<?",
        (strategy, lo, hi)).fetchall()
    total = len(rows)
    nonfill = sum(1 for r in rows if r["close_reason"] in NONFILL)
    filled = total - nonfill
    rate = (filled / total * 100.0) if total else 0.0
    return total, filled, nonfill, rate


def closed_stats(strategy, lo, hi):
    rows = con.execute(
        "SELECT pnl_usd FROM trades WHERE strategy=? AND ts_open>=? AND ts_open<? "
        "AND status='closed' AND close_reason NOT LIKE 'entry_%' "
        "AND close_reason!='restart_flat'", (strategy, lo, hi)).fetchall()
    n = len(rows)
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
    net = sum((r["pnl_usd"] or 0) for r in rows)
    wr = (wins / n * 100.0) if n else 0.0
    return n, wins, wr, net


FAR = 9_999_999_999
print("=" * 70)
print("FILL-RATE density_break (главный эффект #1 taker)")
print("=" * 70)
for label, lo, hi in (("ДО  деплоя", 0, CUT), ("ПОСЛЕ деплоя", CUT, FAR)):
    tot, fil, nf, rate = fill_rate("density_break", lo, hi)
    print(f"{label}: сигналов={tot:3d}  налилось={fil:3d}  непролив={nf:3d}  "
          f"fill-rate={rate:5.1f}%")

print()
print("=" * 70)
print("ПОСЛЕ деплоя — закрытые сделки по стратегиям (выборка мала, мониторинг)")
print("=" * 70)
for strat in ("sweep_fade", "density_bounce", "density_break"):
    n, wins, wr, net = closed_stats(strat, CUT, FAR)
    print(f"{strat:15s}: сделок={n:3d}  WR={wr:5.1f}% ({wins}/{n})  net=${net:+.2f}")

print()
print("=" * 70)
print("По дням (UTC) × стратегия — закрытые сделки")
print("=" * 70)
rows = con.execute(
    "SELECT strategy, ts_open, pnl_usd FROM trades WHERE status='closed' "
    "AND close_reason NOT LIKE 'entry_%' AND close_reason!='restart_flat' "
    "AND ts_open>=? ORDER BY ts_open",
    (CUT - 4 * 86400,)).fetchall()
agg = {}
for r in rows:
    day = datetime.fromtimestamp(r["ts_open"], timezone.utc).strftime("%Y-%m-%d")
    key = (day, r["strategy"])
    a = agg.setdefault(key, [0, 0, 0.0])
    a[0] += 1
    if (r["pnl_usd"] or 0) > 0:
        a[1] += 1
    a[2] += (r["pnl_usd"] or 0)
for (day, strat), (n, w, net) in sorted(agg.items()):
    wr = (w / n * 100.0) if n else 0.0
    print(f"{day}  {strat:15s} n={n:3d}  WR={wr:5.1f}%  net=${net:+.2f}")
con.close()
