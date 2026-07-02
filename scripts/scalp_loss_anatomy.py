"""Анатомия убытков scalp_bot + динамика страт по сегментам их правок. Read-only.

Артефакт-источник для решений 2026-07-02 (BUILDLOG_SCALP):
- flow_scratch у sweep_fade_run: 23 скретча = −$257 (31% всех потерь окна),
  реализация −1.13R при пороге −0.7R → OFF;
- посегментная динамика каждой страты между ЕЁ деплоями (git-таймстемпы).

Запуск на VPS (БД в контейнере):

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_loss_anatomy.py

Локально: SCALP_DB=path/to/scalp_bot.sqlite python3 scripts/scalp_loss_anatomy.py
"""
from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

DB = os.environ.get("SCALP_DB", "/data/scalp_bot.sqlite")
NT = ("restart_flat", "entry_Cancelled", "entry_Rejected",
      "entry_timeout", "entry_Deactivated")

# Пер-стратегийные cutoffs текущего конфига (см. scalp_perstrat_since.py).
CUTOFF = {
    "sweep_fade": "2026-06-28T15:35:00",
    "sweep_fade_canon": "2026-06-28T15:27:00",
    "sweep_fade_run": "2026-06-27T06:06:00",
    "sweep_fade_trend": "2026-06-27T06:06:00",
    "density_break": "2026-06-28T15:55:00",
    "density_bounce": "2026-06-28T15:35:00",
}

# Сегменты: (метка правки, ts начала UTC); конец сегмента = следующая правка
# или now. Таймстемпы — из `git log --pretty='%h %cI %s' -- src/scalp_bot`
# (MSK→UTC −3ч).
SEGMENTS = {
    "sweep_fade": [
        ("v0.18.14 SL-cd 60м (925d105)",            "2026-06-08T10:53"),
        ("v0.18.26 skip_round+reclaim (0d5b96f)",   "2026-06-16T10:10"),
        ("universe→momentum (171d1da)",             "2026-06-17T06:31"),
        ("revert→rvol (6326e7f)",                   "2026-06-19T10:50"),
        ("universe blacklist+floor (c633366)",      "2026-06-28T15:35"),
    ],
    "sweep_fade_canon": [
        ("v0.18.20 создана maker (9fa9113)",        "2026-06-11T08:10"),
        ("v0.18.22 сняты EMA/DMI (3819c67)",        "2026-06-11T13:16"),
        ("v0.18.24 taker-вход (6a35134)",           "2026-06-14T05:19"),
        ("DISABLED для A/B (d410fd0)",              "2026-06-28T15:27"),
    ],
    "sweep_fade_run": [
        ("создана, starving (e4efe43)",             "2026-06-26T11:02"),
        ("round-robin, реальный старт (c4fe43a)",   "2026-06-27T06:06"),
        ("be-lock 34040+идемпотент (b3d0e9e)",      "2026-06-29T06:51"),
        ("be-lock знак adverse (058e695)",          "2026-06-29T10:18"),
    ],
    "sweep_fade_trend": [
        ("создана (e4efe43)",                       "2026-06-26T11:02"),
        ("round-robin, реальный старт (c4fe43a)",   "2026-06-27T06:06"),
    ],
    "density_break": [
        ("v0.18.16 taker+CVD/ob (9837331)",         "2026-06-08T15:20"),
        ("v0.18.25 close-confirm (9c88410)",        "2026-06-15T06:54"),
        ("no-trade BTC/ZEC/TAO (25fa872)",          "2026-06-28T15:55"),
    ],
    "density_bounce": [
        ("v0.18.15 persist 20м (27b4248)",          "2026-06-08T14:28"),
    ],
}


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp()


def pct(a: float, b: float) -> float:
    return a / b * 100 if b else 0.0


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = datetime.now(UTC).timestamp()
    rows = [dict(r) for r in con.execute(
        "SELECT id, ts_open, ts_close, symbol, side, qty, entry, sl, pnl_usd, "
        "close_reason, strategy FROM trades WHERE status='closed' "
        "AND ts_close IS NOT NULL AND close_reason NOT IN ({}) "
        "AND ts_close >= strftime('%s','2026-06-01') ORDER BY ts_close"
        .format(",".join("?" * len(NT))), NT)]

    # ── 1. Динамика по сегментам правок каждой страты ──
    print("=" * 72)
    print("ДИНАМИКА ПО СТРАТАМ: сегменты между их правками")
    for st, marks in SEGMENTS.items():
        print(f"\n--- {st} ---")
        for i, (label, iso) in enumerate(marks):
            t0 = _ts(iso)
            t1 = _ts(marks[i + 1][1]) if i + 1 < len(marks) else now
            seg = [r for r in rows if r["strategy"] == st
                   and t0 <= r["ts_close"] < t1]
            if not seg:
                print(f"  {iso[5:16]} {label:40s} — сделок нет")
                continue
            pnls = [r["pnl_usd"] or 0.0 for r in seg]
            w = [p for p in pnls if p > 0]
            loss = [p for p in pnls if p <= 0]
            pf = (sum(w) / abs(sum(loss))) if loss and sum(loss) else float("inf")
            days = (min(t1, now) - t0) / 86400
            print(f"  {iso[5:16]} {label:40s} n={len(pnls):4d} "
                  f"WR={pct(len(w), len(pnls)):3.0f}% net=${sum(pnls):+8.2f} "
                  f"avg=${sum(pnls) / len(pnls):+5.2f} PF={pf:4.2f} ({days:.1f}д)")

    # ── 2. Вклад в убытки (strategy × reason), post-cutoff ──
    post = [r for r in rows
            if r["ts_close"] >= _ts(CUTOFF.get(r["strategy"], "2026-06-01T00:00:00"))]
    losses = [r for r in post if (r["pnl_usd"] or 0) < 0]
    tot_l = sum(r["pnl_usd"] for r in losses)
    print("\n" + "=" * 72)
    print(f"POST-CUTOFF: n={len(post)}, лузов {len(losses)} (${tot_l:+.0f})")
    print("--- вклад в убытки (strategy × reason), топ по $ ---")
    b: dict[tuple, list] = defaultdict(lambda: [0, 0.0])
    for r in losses:
        k = (r["strategy"], r["close_reason"])
        b[k][0] += 1
        b[k][1] += r["pnl_usd"]
    for k, v in sorted(b.items(), key=lambda kv: kv[1][1])[:12]:
        print(f"  {k[0]:18s} {str(k[1]):14s} n={v[0]:3d} ${v[1]:+9.2f} "
              f"({pct(v[1], tot_l):.0f}% лузов)")

    # ── 3. Час дня (UTC) ──
    print("\n--- час дня UTC (post-cutoff) ---")
    byh: dict[int, list] = defaultdict(lambda: [0, 0, 0.0])
    for r in post:
        h = datetime.fromtimestamp(r["ts_open"], UTC).hour
        byh[h][0] += 1
        byh[h][1] += (r["pnl_usd"] or 0) > 0
        byh[h][2] += r["pnl_usd"] or 0
    for h in sorted(byh):
        n, w, p = byh[h]
        print(f"  h{h:02d}: n={n:2d} WR={pct(w, n):3.0f}% ${p:+7.2f}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
