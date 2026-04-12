"""Сверка DB vs API за период после последнего деплоя (09:16 UTC 2026-04-12)."""

import sqlite3
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/src")
from bybit_bot.trading.client import BybitClient

CUTOFF = "2026-04-12T08:34:00+00:00"
CUTOFF_TS_MS = int(datetime(2026, 4, 12, 8, 34, 0, tzinfo=timezone.utc).timestamp() * 1000)

client = BybitClient(
    api_key=os.environ["BYBIT_BOT_API_KEY"],
    api_secret=os.environ["BYBIT_BOT_API_SECRET"],
    demo=True,
)

db = sqlite3.connect("/data/bybit_stats.sqlite")
db.row_factory = sqlite3.Row

print("=" * 65)
print("СВЕРКА DB vs API с %s" % CUTOFF)
print("=" * 65)

# --- 1. Баланс ---
bal = client.get_balance()
print("\n1. БАЛАНС (API)")
print("   Equity: $%.2f  Available: $%.2f  uPnL: $%.2f" % (
    bal.total_equity, bal.available_balance, bal.unrealised_pnl))

# --- 2. Открытые позиции ---
print("\n" + "=" * 65)
print("2. ОТКРЫТЫЕ ПОЗИЦИИ: API vs DB")
print("=" * 65)

api_pos = client.get_positions()
db_open = db.execute(
    "SELECT * FROM positions WHERE closed_at IS NULL"
).fetchall()

api_map = {}
for p in api_pos:
    api_map[p.symbol] = p

db_map = {}
for r in db_open:
    sym = r["symbol"]
    if sym not in db_map:
        db_map[sym] = []
    db_map[sym].append(r)

all_syms = sorted(set(list(api_map.keys()) + list(db_map.keys())))
mismatches = 0

for sym in all_syms:
    ap = api_map.get(sym)
    drs = db_map.get(sym, [])

    if ap and not drs:
        print("  !! %s: на Bybit (%s qty=%s), НЕТ в БД" % (sym, ap.side, ap.size))
        mismatches += 1
    elif not ap and drs:
        for d in drs:
            print("  !! %s: в БД (id=%s %s qty=%s), НЕТ на Bybit" % (
                sym, d["id"], d["side"], d["qty"]))
        mismatches += 1
    else:
        for d in drs:
            ok_side = ap.side == d["side"]
            ok_qty = abs(float(ap.size) - float(d["qty"])) < 0.0001
            tag = "OK" if (ok_side and ok_qty) else "MISMATCH"
            if tag != "OK":
                mismatches += 1
            print("  [%s] %s  API(%s qty=%s entry=%.4f uPnL=%.4f)  DB(id=%s %s qty=%s strat=%s)" % (
                tag, sym, ap.side, ap.size, ap.entry_price, ap.unrealised_pnl,
                d["id"], d["side"], d["qty"], d["strategy"]))

print("\n  API: %d  DB open: %d  Расхождений: %d" % (len(api_pos), len(db_open), mismatches))

# --- 3. Closed PnL после cutoff ---
print("\n" + "=" * 65)
print("3. CLOSED PnL ПОСЛЕ %s" % CUTOFF)
print("=" * 65)

# API: все closed-pnl, фильтруем по updatedTime >= cutoff
api_closed_all = client.get_closed_pnl(limit=50)
api_recent = []
for r in api_closed_all:
    ts = int(r["updatedTime"])
    if ts >= CUTOFF_TS_MS:
        api_recent.append(r)

api_pnl = 0.0
api_by_sym = {}
print("\n  --- API closed PnL (после cutoff) ---")
for r in api_recent:
    pnl = float(r["closedPnl"])
    api_pnl += pnl
    sym = r["symbol"]
    ts = int(r["updatedTime"]) / 1000
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
    if sym not in api_by_sym:
        api_by_sym[sym] = {"count": 0, "pnl": 0.0, "rows": []}
    api_by_sym[sym]["count"] += 1
    api_by_sym[sym]["pnl"] += pnl
    api_by_sym[sym]["rows"].append(r)
    print("  %4s %14s qty=%10s pnl=%+.4f [%s UTC]" % (
        r["side"], sym, r["qty"], pnl, dt))

print("\n  API итого: %d сделок, PnL = $%.4f" % (len(api_recent), api_pnl))

# DB: closed после cutoff, исключая sync_closed
db_recent_all = db.execute(
    "SELECT * FROM positions WHERE closed_at IS NOT NULL AND closed_at >= ? ORDER BY closed_at",
    (CUTOFF,)
).fetchall()

db_recent_real = [r for r in db_recent_all if (r["close_reason"] or "") != "sync_closed"]
db_recent_sync = [r for r in db_recent_all if (r["close_reason"] or "") == "sync_closed"]

db_pnl = 0.0
db_wins = 0
db_losses = 0
db_by_sym = {}
print("\n  --- DB closed (excl sync_closed, после cutoff) ---")
for r in db_recent_real:
    pnl = r["pnl_usd"] or 0.0
    db_pnl += pnl
    if pnl > 0:
        db_wins += 1
    elif pnl < 0:
        db_losses += 1
    sym = r["symbol"]
    if sym not in db_by_sym:
        db_by_sym[sym] = {"count": 0, "pnl": 0.0}
    db_by_sym[sym]["count"] += 1
    db_by_sym[sym]["pnl"] += pnl
    print("  %4s %14s qty=%10s pnl=%+.4f exit=%s closed=%s strat=%s" % (
        r["side"], sym, r["qty"], pnl,
        r["close_reason"] or "-", (r["closed_at"] or "")[:19], r["strategy"] or "-"))

print("\n  DB итого: %d сделок, PnL = $%.4f (wins=%d, losses=%d)" % (
    len(db_recent_real), db_pnl, db_wins, db_losses))
if db_recent_real:
    print("  Win rate: %.1f%%" % (db_wins / len(db_recent_real) * 100))

# sync_closed после cutoff
if db_recent_sync:
    print("\n  DB sync_closed после cutoff: %d" % len(db_recent_sync))
    for r in db_recent_sync:
        print("    id=%s %s %s qty=%s strat=%s" % (
            r["id"], r["side"], r["symbol"], r["qty"], r["strategy"]))

# --- 4. Сравнение ---
print("\n" + "=" * 65)
print("4. СРАВНЕНИЕ")
print("=" * 65)

delta = api_pnl - db_pnl
print("  API PnL:   $%.4f (%d trades)" % (api_pnl, len(api_recent)))
print("  DB PnL:    $%.4f (%d trades)" % (db_pnl, len(db_recent_real)))
print("  Delta:     $%.4f" % delta)
print("  sync_closed (потерянные): %d" % len(db_recent_sync))

if abs(delta) < 0.05 and len(db_recent_sync) == 0:
    print("\n  >>> ДАННЫЕ СХОДЯТСЯ")
else:
    print("\n  >>> ЕСТЬ РАСХОЖДЕНИЯ")

# По символам
print("\n  --- По символам ---")
all_s = sorted(set(list(api_by_sym.keys()) + list(db_by_sym.keys())))
for sym in all_s:
    a = api_by_sym.get(sym, {"count": 0, "pnl": 0.0})
    d = db_by_sym.get(sym, {"count": 0, "pnl": 0.0})
    diff = abs(a["pnl"] - d["pnl"])
    flag = " OK" if diff < 0.01 else " !!DIFF(%.4f)" % diff
    print("  %14s: API(%d, $%+.4f) DB(%d, $%+.4f)%s" % (
        sym, a["count"], a["pnl"], d["count"], d["pnl"], flag))

db.close()
print("\n" + "=" * 65)
print("АУДИТ ЗАВЕРШЁН")
print("=" * 65)
