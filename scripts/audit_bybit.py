"""Сверка данных SQLite с Bybit API — запускать внутри контейнера bybit-bot."""

import sqlite3
import os
import sys
from datetime import datetime, UTC

sys.path.insert(0, "/app/src")
from bybit_bot.trading.client import BybitClient

client = BybitClient(
    api_key=os.environ["BYBIT_BOT_API_KEY"],
    api_secret=os.environ["BYBIT_BOT_API_SECRET"],
    demo=True,
)

db = sqlite3.connect("/data/bybit_stats.sqlite")
db.row_factory = sqlite3.Row

# 1. БАЛАНС
bal = client.get_balance()
print("=" * 60)
print("1. БАЛАНС (Bybit API)")
print("=" * 60)
print("  Equity:    $%.2f" % bal.total_equity)
print("  Available: $%.2f" % bal.available_balance)
print("  uPnL:      $%.2f" % bal.unrealised_pnl)

# 2. ОТКРЫТЫЕ ПОЗИЦИИ: API vs DB
print()
print("=" * 60)
print("2. ОТКРЫТЫЕ ПОЗИЦИИ: API vs DB")
print("=" * 60)

api_positions = client.get_positions()
db_open = db.execute("SELECT * FROM positions WHERE closed_at IS NULL").fetchall()

api_map = {}
for p in api_positions:
    api_map[p.symbol] = p

db_map = {}
for r in db_open:
    sym = r["symbol"]
    if sym not in db_map:
        db_map[sym] = []
    db_map[sym].append(r)

all_symbols = sorted(set(list(api_map.keys()) + list(db_map.keys())))

mismatches = 0
for sym in all_symbols:
    api_pos = api_map.get(sym)
    db_rows = db_map.get(sym, [])

    if api_pos and not db_rows:
        print("  !! %s: ЕСТЬ на Bybit (%s qty=%s uPnL=%.4f), НЕТ в БД!" % (
            sym, api_pos.side, api_pos.size, api_pos.unrealised_pnl))
        mismatches += 1
    elif not api_pos and db_rows:
        for dr in db_rows:
            print("  !! %s: НЕТ на Bybit, ЕСТЬ в БД (id=%s %s qty=%s strat=%s)" % (
                sym, dr["id"], dr["side"], dr["qty"], dr["strategy"]))
        mismatches += 1
    else:
        for dr in db_rows:
            api_qty = float(api_pos.size)
            db_qty = float(dr["qty"])
            side_match = api_pos.side == dr["side"]
            qty_match = abs(api_qty - db_qty) < 0.0001
            entry_match = abs(api_pos.entry_price - dr["entry_price"]) < 0.01

            status = "OK" if (side_match and qty_match) else "MISMATCH"
            if status != "OK":
                mismatches += 1

            print("  [%s] %s:" % (status, sym))
            print("    API: %s qty=%s entry=%.4f uPnL=%.4f lev=%s" % (
                api_pos.side, api_pos.size, api_pos.entry_price,
                api_pos.unrealised_pnl, api_pos.leverage))
            print("    DB:  %s qty=%s entry=%s strat=%s pair=%s" % (
                dr["side"], dr["qty"], dr["entry_price"],
                dr["strategy"], dr["pair_tag"] or "-"))
            if not entry_match:
                print("    !! Entry price mismatch: API=%.4f vs DB=%s" % (
                    api_pos.entry_price, dr["entry_price"]))

print("\n  Итого: API=%d позиций, DB open=%d, Расхождений: %d" % (
    len(api_positions), len(db_open), mismatches))

# 3. CLOSED PnL: API vs DB
print()
print("=" * 60)
print("3. CLOSED PnL: API vs DB")
print("=" * 60)

api_closed = client.get_closed_pnl(limit=50)
db_closed_all = db.execute(
    "SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC"
).fetchall()

api_total_pnl = 0.0
api_by_sym = {}
print("\n  --- Bybit API closed PnL (последние 30) ---")
for i, r in enumerate(api_closed):
    pnl = float(r["closedPnl"])
    api_total_pnl += pnl
    sym = r["symbol"]
    if sym not in api_by_sym:
        api_by_sym[sym] = {"count": 0, "pnl": 0.0}
    api_by_sym[sym]["count"] += 1
    api_by_sym[sym]["pnl"] += pnl
    if i < 30:
        ts = int(r["updatedTime"]) / 1000
        dt = datetime.utcfromtimestamp(ts).strftime("%m-%d %H:%M")
        print("  %4s %14s qty=%10s pnl=%+.4f [%s]" % (
            r["side"], r["symbol"], r["qty"], pnl, dt))

print("\n  API total closed PnL (%d trades): $%.4f" % (len(api_closed), api_total_pnl))

# DB closed (без sync_closed)
db_closed_real = [r for r in db_closed_all if (r["close_reason"] or "") != "sync_closed"]
db_closed_sync = [r for r in db_closed_all if (r["close_reason"] or "") == "sync_closed"]

db_total_pnl = 0.0
db_wins = 0
db_losses = 0
db_by_sym = {}
for r in db_closed_real:
    pnl = r["pnl_usd"] or 0.0
    db_total_pnl += pnl
    if pnl > 0:
        db_wins += 1
    elif pnl < 0:
        db_losses += 1
    sym = r["symbol"]
    if sym not in db_by_sym:
        db_by_sym[sym] = {"count": 0, "pnl": 0.0}
    db_by_sym[sym]["count"] += 1
    db_by_sym[sym]["pnl"] += pnl

print("\n  --- DB closed (excl sync_closed): %d trades ---" % len(db_closed_real))
print("  DB total PnL: $%.4f" % db_total_pnl)
print("  DB wins: %d, losses: %d" % (db_wins, db_losses))
if db_closed_real:
    print("  DB win rate: %.1f%%" % (db_wins / len(db_closed_real) * 100))

delta = api_total_pnl - db_total_pnl
print("\n  >>> РАЗНИЦА PnL: API=$%.4f vs DB=$%.4f" % (api_total_pnl, db_total_pnl))
print("  >>> Delta: $%.4f" % delta)
if abs(delta) > 0.01:
    print("  >>> !!! ДАННЫЕ НЕ СХОДЯТСЯ !!!")
else:
    print("  >>> Данные сходятся")

# Сравнение по символам
print("\n  --- Сравнение по символам ---")
all_syms = sorted(set(list(api_by_sym.keys()) + list(db_by_sym.keys())))
for sym in all_syms:
    a = api_by_sym.get(sym, {"count": 0, "pnl": 0.0})
    d = db_by_sym.get(sym, {"count": 0, "pnl": 0.0})
    flag = "" if abs(a["pnl"] - d["pnl"]) < 0.01 else " !!DIFF"
    print("  %14s: API(%d trades, $%+.4f) vs DB(%d trades, $%+.4f)%s" % (
        sym, a["count"], a["pnl"], d["count"], d["pnl"], flag))

# 4. СТРАТЕГИИ (DB)
print()
print("=" * 60)
print("4. DB ПО СТРАТЕГИЯМ (excl sync_closed)")
print("=" * 60)

strats = {}
for r in db_closed_real:
    s = r["strategy"] or "unknown"
    if s not in strats:
        strats[s] = {"count": 0, "pnl": 0.0, "wins": 0}
    pnl = r["pnl_usd"] or 0.0
    strats[s]["count"] += 1
    strats[s]["pnl"] += pnl
    if pnl > 0:
        strats[s]["wins"] += 1

for s, d in sorted(strats.items()):
    wr = (d["wins"] / d["count"] * 100) if d["count"] > 0 else 0
    print("  %20s: %3d trades, PnL=$%+.4f, WR=%.1f%%" % (s, d["count"], d["pnl"], wr))

# 5. EXIT REASONS (DB)
print()
print("=" * 60)
print("5. DB ПО EXIT REASON (все)")
print("=" * 60)

exits = {}
for r in db_closed_all:
    ex = r["close_reason"] or "unknown"
    pnl = r["pnl_usd"] or 0.0
    if ex not in exits:
        exits[ex] = {"count": 0, "pnl": 0.0}
    exits[ex]["count"] += 1
    exits[ex]["pnl"] += pnl

for e, d in sorted(exits.items()):
    print("  %25s: %3d trades, PnL=$%+.4f" % (e, d["count"], d["pnl"]))

# 6. SYNC_CLOSED
print()
print("=" * 60)
print("6. SYNC_CLOSED (позиции в DB, не найденные на API)")
print("=" * 60)
print("  Всего sync_closed: %d" % len(db_closed_sync))
for r in db_closed_sync[-10:]:
    print("  id=%s %s %s qty=%s strat=%s opened=%s" % (
        r["id"], r["side"], r["symbol"], r["qty"], r["strategy"], r["opened_at"]))

db.close()
print()
print("=" * 60)
print("АУДИТ ЗАВЕРШЁН")
print("=" * 60)
