import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict

db = sqlite3.connect("/data/advisor_stats.sqlite")
db.row_factory = sqlite3.Row

cutoff = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()

rows = db.execute(
    """
    SELECT instrument, strategy, direction, profit_pips, profit_pct,
           status, exit_reason, broker_position_id, broker_volume
    FROM positions
    WHERE created_at >= ?
""",
    (cutoff,),
).fetchall()

strat_data = defaultdict(
    lambda: {
        "tot": 0,
        "w": 0,
        "l": 0,
        "be": 0,
        "pips": 0.0,
        "brok": 0,
        "pairs": defaultdict(lambda: {"n": 0, "w": 0, "pips": 0.0}),
        "exits": defaultdict(int),
    }
)

for r in rows:
    s = strat_data[r["strategy"]]
    pips = r["profit_pips"] or 0.0
    s["tot"] += 1
    s["pips"] += pips
    if pips > 0.5:
        s["w"] += 1
    elif pips < -0.5:
        s["l"] += 1
    else:
        s["be"] += 1
    if r["broker_position_id"] and r["broker_position_id"] > 0:
        s["brok"] += 1
    s["exits"][r["exit_reason"] or "open"] += 1
    pair = r["instrument"]
    s["pairs"][pair]["n"] += 1
    s["pairs"][pair]["pips"] += pips
    if pips > 0.5:
        s["pairs"][pair]["w"] += 1

open_rows = db.execute(
    """
    SELECT instrument, strategy, profit_pips, broker_position_id
    FROM positions WHERE status = 'open'
"""
).fetchall()

open_map = defaultdict(lambda: {"n": 0, "pips": 0.0})
for r in open_rows:
    open_map[r["strategy"]]["n"] += 1
    open_map[r["strategy"]]["pips"] += r["profit_pips"] or 0.0

for strat in sorted(strat_data.keys()):
    s = strat_data[strat]
    wr = (s["w"] / s["tot"] * 100) if s["tot"] > 0 else 0
    print(f"=== {strat.upper()} ===")
    print(f"  Сделок: {s['tot']} (broker: {s['brok']})")
    print(f"  W/L/BE: {s['w']}/{s['l']}/{s['be']}  WinRate: {wr:.0f}%")
    print(f"  Итого pips: {s['pips']:+.1f}")
    exits_str = ", ".join(
        f"{k}={v}" for k, v in sorted(s["exits"].items(), key=lambda x: -x[1])
    )
    print(f"  Закрытия: {exits_str}")
    print(f"  По парам:")
    for pair, pd in sorted(s["pairs"].items(), key=lambda x: x[1]["pips"]):
        pwr = (pd["w"] / pd["n"] * 100) if pd["n"] > 0 else 0
        print(
            f"    {pair:15s}  {pd['n']:2d} сд.  WR {pwr:3.0f}%  {pd['pips']:+.1f} pips"
        )
    op = open_map.get(strat)
    if op:
        print(f"  Открыто: {op['n']} поз., unrealized {op['pips']:+.1f} pips")
    print()

tt = sum(s["tot"] for s in strat_data.values())
tp = sum(s["pips"] for s in strat_data.values())
tw = sum(s["w"] for s in strat_data.values())
twr = (tw / tt * 100) if tt > 0 else 0
to_n = sum(o["n"] for o in open_map.values())
to_p = sum(o["pips"] for o in open_map.values())
print("=== ИТОГО ===")
print(f"  Закрытых: {tt}, Win Rate: {twr:.0f}%, Pips: {tp:+.1f}")
print(f"  Открытых: {to_n}, unrealized: {to_p:+.1f} pips")
