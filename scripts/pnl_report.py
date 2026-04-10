#!/usr/bin/env python3
"""P&L report by instrument (broker trades since 2026-04-07)."""
import sqlite3, os

db_path = os.environ.get("DB_PATH", "/data/advisor_stats.sqlite")
db = sqlite3.connect(db_path)

rows = db.execute("""
    SELECT instrument,
           COUNT(*) as trades,
           SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(profit_pips), 1) as gross,
           ROUND(AVG(profit_pips), 1) as avg_p,
           ROUND(MIN(profit_pips), 1) as worst,
           ROUND(MAX(profit_pips), 1) as best
    FROM positions
    WHERE status = 'closed'
      AND created_at >= '2026-04-07'
      AND broker_position_id > 0
      AND strategy NOT LIKE 'paper_%'
    GROUP BY instrument
    ORDER BY gross ASC
""").fetchall()

print(f"{'INSTRUMENT':<14} {'TRADES':>6} {'WIN%':>5} {'GROSS':>8} {'AVG':>7} {'WORST':>7} {'BEST':>7}")
print("-" * 62)
tp = tt = 0
for r in rows:
    wr = r[2] / r[1] * 100 if r[1] else 0
    print(f"{r[0]:<14} {r[1]:>6} {wr:>4.0f}% {r[3]:>+8.1f} {r[4]:>+7.1f} {r[5]:>+7.1f} {r[6]:>+7.1f}")
    tp += r[3] or 0
    tt += r[1]
print("-" * 62)
print(f"{'TOTAL':<14} {tt:>6}       {tp:>+8.1f}")

print("\n--- By strategy + instrument ---")
rows2 = db.execute("""
    SELECT instrument, strategy,
           COUNT(*) as trades,
           SUM(CASE WHEN profit_pips > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(profit_pips), 1) as gross
    FROM positions
    WHERE status = 'closed'
      AND created_at >= '2026-04-07'
      AND broker_position_id > 0
      AND strategy NOT LIKE 'paper_%'
    GROUP BY instrument, strategy
    ORDER BY gross ASC
""").fetchall()

print(f"{'INSTRUMENT':<14} {'STRATEGY':<16} {'TRADES':>6} {'WIN%':>5} {'GROSS':>8}")
print("-" * 55)
for r in rows2:
    wr = r[3] / r[2] * 100 if r[2] else 0
    print(f"{r[0]:<14} {r[1]:<16} {r[2]:>6} {wr:>4.0f}% {r[4]:>+8.1f}")

db.close()
