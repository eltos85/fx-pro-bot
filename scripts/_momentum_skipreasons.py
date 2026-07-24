import sqlite3
from collections import Counter
from datetime import datetime, timezone

db = sqlite3.connect("/data/momentum_bot.sqlite")
PERIODS = [
    ("P0 do pravok", None, "2026-07-02 11:28"),
    ("P1 friday-block", "2026-07-02 11:28", "2026-07-10 08:41"),
    ("P2 guard v1", "2026-07-10 08:41", "2026-07-13 07:08"),
    ("P3 guard re-applied", "2026-07-13 07:08", "2026-07-15 07:05"),
    ("P4 profit-protect", "2026-07-15 07:05", "2026-07-22 09:25"),
    ("P5 posle otmeny", "2026-07-22 09:25", "2026-07-24 08:27"),
    ("P6 exit-hyst+NY+ADX+gap", "2026-07-24 08:27", None),
]

def p(s): return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()

def period_of(ts):
    for i, (_, st, en) in enumerate(PERIODS):
        if st and ts < p(st): continue
        if en and ts >= p(en): continue
        return i
    return 0

rows = db.execute("select created_at,executed,note from momentum_decisions").fetchall()
print(f"{'period':<22} {'dec':>6} {'exec':>5} {'friday':>7} {'already':>7} {'offses':>7} {'event':>6} {'same':>5} {'flat':>5} {'ok':>4} {'paper':>5} {'other':>5}")
for i, (label, _, _) in enumerate(PERIODS):
    cnt = Counter(); total = 0; ex = 0
    for ca, e, n in rows:
        try: ts = datetime.strptime(ca, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except: continue
        if period_of(ts) != i: continue
        total += 1
        if e: ex += 1
        n = n or ""
        if "friday_flat" in n: cnt["friday"] += 1
        elif "already_open" in n: cnt["already"] += 1
        elif "off_session" in n: cnt["offses"] += 1
        elif "event" in n: cnt["event"] += 1
        elif n == "same_direction": cnt["same"] += 1
        elif n == "flat": cnt["flat"] += 1
        elif "live_open:ok" in n: cnt["ok"] += 1
        elif "paper" in n: cnt["paper"] += 1
        else: cnt["other"] += 1
    print(f"{label:<22} {total:>6} {ex:>5} {cnt['friday']:>7} {cnt['already']:>7} {cnt['offses']:>7} {cnt['event']:>6} {cnt['same']:>5} {cnt['flat']:>5} {cnt['ok']:>4} {cnt['paper']:>5} {cnt['other']:>5}")
