"""Диагностика sweep_fade: баг входа/тренда ИЛИ режим/вариативность?

Чистая отсечка: 2026-07-10 08:05 UTC (после v0.18.34 — dead_market gate для
sweep_fade-семейства; меняет пул входов). Read-only. Цель — отличить системный
дефект (инверсия стороны, сломанный HTF-фильтр) от рыночного режима/шума.

    docker exec fx-pro-bot-scalp-bot-1 python3 /tmp/sf_diag.py
"""
import sqlite3
from datetime import datetime, timezone

DB = "/data/scalp_bot.sqlite"
CUT = datetime(2026, 7, 10, 8, 5, tzinfo=timezone.utc).timestamp()
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

rows = [dict(r) for r in con.execute(
    "SELECT symbol, side, pnl_usd, close_reason, ts_open FROM trades "
    "WHERE strategy='sweep_fade' AND status='closed' "
    "AND close_reason NOT LIKE 'entry_%' AND close_reason!='restart_flat' "
    "AND ts_open>=? ORDER BY ts_open", (CUT,)).fetchall()]


def block(title, rs):
    n = len(rs)
    if not n:
        print(f"{title}: нет сделок")
        return
    wins = [r["pnl_usd"] for r in rs if (r["pnl_usd"] or 0) > 0]
    loss = [r["pnl_usd"] for r in rs if (r["pnl_usd"] or 0) <= 0]
    net = sum((r["pnl_usd"] or 0) for r in rs)
    wr = len(wins) / n * 100.0
    aw = (sum(wins) / len(wins)) if wins else 0.0
    al = (sum(loss) / len(loss)) if loss else 0.0
    pf = (sum(wins) / -sum(loss)) if loss and sum(loss) < 0 else float("inf")
    rr = (aw / -al) if al < 0 else float("inf")
    print(f"{title}: n={n:3d} WR={wr:5.1f}% net=${net:+8.2f} "
          f"avgW=${aw:+6.2f} avgL=${al:+6.2f} R:R={rr:4.2f} PF={pf:4.2f}")


print("=" * 78)
print(f"sweep_fade ВСЕГО с 2026-07-10 08:05 UTC (n={len(rows)})")
print("=" * 78)
block("ИТОГО          ", rows)
print()
print("--- по СТОРОНЕ (детектит трендовый перекос/инверсию) ---")
block("LONG (fade dn) ", [r for r in rows if r["side"] == "long"])
block("SHORT (fade up)", [r for r in rows if r["side"] == "short"])
print()
print("--- по ПРИЧИНЕ ВЫХОДА ---")
reasons = {}
for r in rows:
    reasons.setdefault(r["close_reason"], []).append(r)
for cr, rs in sorted(reasons.items(), key=lambda x: sum((y["pnl_usd"] or 0) for y in x[1])):
    net = sum((y["pnl_usd"] or 0) for y in rs)
    print(f"  {cr:16s}: n={len(rs):3d}  net=${net:+8.2f}")
print()
print("--- по ДНЯМ × сторона ---")
byday = {}
for r in rows:
    day = datetime.fromtimestamp(r["ts_open"], timezone.utc).strftime("%m-%d")
    byday.setdefault(day, []).append(r)
for day in sorted(byday):
    block(f"  {day} all  ", byday[day])
    block(f"  {day} long ", [r for r in byday[day] if r["side"] == "long"])
    block(f"  {day} short", [r for r in byday[day] if r["side"] == "short"])
print()
print("=" * 78)
print("ДЕНЬ Jun-08 (главный минус) — по символам × сторона")
print("=" * 78)
j8 = [r for r in rows if datetime.fromtimestamp(r["ts_open"], timezone.utc)
      .strftime("%m-%d") == "06-08"]
bysym = {}
for r in j8:
    bysym.setdefault((r["symbol"], r["side"]), []).append(r)
for (sym, side), rs in sorted(bysym.items(), key=lambda x: sum((y["pnl_usd"] or 0) for y in x[1])):
    net = sum((y["pnl_usd"] or 0) for y in rs)
    w = sum(1 for y in rs if (y["pnl_usd"] or 0) > 0)
    print(f"  {sym:10s} {side:5s}: n={len(rs):2d} WR={w/len(rs)*100:5.1f}% net=${net:+7.2f}")
con.close()
