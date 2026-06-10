"""Полный аудит scalp_bot с 06-05 11:00 UTC (чистый baseline после v0.18.10).

Цель: найти ГДЕ течёт минус по двум стратам (sweep_fade, density_break) на
выборке n>=50. Декомпозиция: сторона, символ, close_reason, fill-rate,
before/after каждой правки (v0.18.14..v0.18.17).

Запуск: ssh root@VPS "docker exec -i fx-pro-bot-scalp-bot-1 python3 -" < scripts/scalp_full_audit.py
"""
import sqlite3
from datetime import datetime, UTC
from collections import defaultdict

DB = "/data/scalp_bot.sqlite"
FILL = ("flow_exit", "sl_hit", "tp_hit")


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


BASE = ts("2026-06-05T11:00:00")
WIN = {
    "v0.18.14 cooldown": ts("2026-06-08T11:00:00"),
    "v0.18.15 d_bounce": ts("2026-06-08T14:30:00"),
    "v0.18.16 d_break ": ts("2026-06-08T15:30:00"),
    "v0.18.17 ZEC-long": ts("2026-06-09T08:04:00"),
}

c = sqlite3.connect(DB)
rows = c.execute(
    "SELECT strategy,side,symbol,pnl_usd,close_reason,ts_open FROM trades "
    "WHERE ts_open>=? AND status='closed'", (BASE,)
).fetchall()


def stat(v):
    n = len(v)
    if not n:
        return "n=0"
    w = [p for p in v if p > 0]
    l = [p for p in v if p <= 0]
    wr = 100 * len(w) / n
    aw = sum(w) / len(w) if w else 0
    al = sum(l) / len(l) if l else 0
    rr = aw / abs(al) if al else 0
    return (f"n={n:3d} WR={wr:5.1f}%({len(w):2d}/{n:2d}) net={sum(v):+8.2f} "
            f"avgW={aw:+6.2f} avgL={al:+6.2f} R:R={rr:.2f}")


print(f"=== ОКНО: 2026-06-05 11:00 UTC -> now ===")
print(f"всего closed={len(rows)}")

for st in ("sweep_fade", "density_break", "density_bounce"):
    srows = [r for r in rows if r[0] == st]
    filled = [r for r in srows if r[4] in FILL]
    nofill = [r for r in srows if r[4] not in FILL]
    fr = 100 * len(filled) / len(srows) if srows else 0
    print(f"\n############## {st} ##############")
    print(f"всего={len(srows)} налитых={len(filled)} не-налитых={len(nofill)} "
          f"fill-rate={fr:.0f}%")
    if not filled:
        continue
    print("  [ВСЕГО налитых]   ", stat([r[3] for r in filled]))
    for sd in ("long", "short"):
        print(f"  [{sd:5s}]          ", stat([r[3] for r in filled if r[1] == sd]))

    # по символам
    by = defaultdict(list)
    for r in filled:
        by[r[2]].append(r[3])
    print("  -- по символам (sort by net) --")
    for sym, v in sorted(by.items(), key=lambda kv: sum(kv[1])):
        print(f"     {sym:12s}", stat(v))

    # по close_reason
    cr = defaultdict(list)
    for r in filled:
        cr[r[4]].append(r[3])
    print("  -- по close_reason --")
    for k in FILL:
        if cr[k]:
            v = cr[k]
            print(f"     {k:10s} n={len(v):3d} net={sum(v):+8.2f} "
                  f"avg={sum(v)/len(v):+6.2f}")

    # before/after правок (накопительные окна)
    print("  -- по окнам правок (налитые) --")
    bounds = [("baseline->14", BASE, WIN["v0.18.14 cooldown"])]
    keys = list(WIN.items())
    for i, (name, t0) in enumerate(keys):
        t1 = keys[i + 1][1] if i + 1 < len(keys) else 1e12
        bounds.append((name, t0, t1))
    for name, t0, t1 in bounds:
        v = [r[3] for r in filled if t0 <= r[5] < t1]
        if v:
            print(f"     {name:20s}", stat(v))
