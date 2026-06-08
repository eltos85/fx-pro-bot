"""sweep_fade: детально серийный перефейд (вход после SL по той же монете).

Показывает каждый перезаход: интервал от предыдущего SL, попал бы он под текущий
кулдаун 300с или нет, и до/после деплоя фикса v0.18.12 (8 июня 07:45 UTC).
Read-only.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

FIX_TS = datetime(2026, 6, 8, 7, 45, tzinfo=UTC).timestamp()  # v0.18.12 deploy
COOLDOWN_S = 300  # текущий пост-SL кулдаун (v0.15.0)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-06-05T11:00")
    p.add_argument("--to", default="2026-06-09T00:00")
    p.add_argument("--window-min", type=int, default=90)
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()
    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,pnl_usd,close_reason,ts_open FROM trades "
        "WHERE ts_open>=? AND ts_open<? AND strategy='sweep_fade' "
        "AND status='closed' AND close_reason NOT LIKE 'entry_%' "
        "AND close_reason!='restart_flat' AND pnl_usd IS NOT NULL "
        "ORDER BY ts_open", (frm, to))]
    con.close()

    last_sl = {}
    reentries = []
    for x in rows:
        prev = last_sl.get(x["symbol"])
        if prev is not None and (x["ts_open"] - prev) < args.window_min * 60:
            x["_gap_min"] = (x["ts_open"] - prev) / 60.0
            reentries.append(x)
        if x["close_reason"] == "sl_hit":
            last_sl[x["symbol"]] = x["ts_open"]

    print(f"=== серийный перефейд (<{args.window_min}м) {args.frm}→{args.to} ===")
    print(f"всего перезаходов: {len(reentries)}")
    print(f"\n{'время(UTC)':<17}{'монета':<10}{'side':<6}{'gap,м':>7}{'pnl':>8}"
          f"{'кулдаун300?':>13}  фикс")
    le5 = gt5 = 0
    for x in reentries:
        t = datetime.fromtimestamp(x["ts_open"], UTC).strftime("%m-%d %H:%M")
        caught = x["_gap_min"] * 60 <= COOLDOWN_S
        le5 += caught
        gt5 += not caught
        fix = "ПОСЛЕ" if x["ts_open"] >= FIX_TS else "до"
        print(f"{t:<17}{x['symbol']:<10}{x['side']:<6}{x['_gap_min']:>7.1f}"
              f"{x['pnl_usd']:>8.2f}{'да' if caught else 'НЕТ':>13}  {fix}")

    print(f"\n-- покрытие текущим кулдауном 300с --")
    print(f"  попали бы под 300с (gap<=5м): {le5}")
    print(f"  НЕ покрыты (gap>5м): {gt5}")

    before = [x for x in reentries if x["ts_open"] < FIX_TS]
    after = [x for x in reentries if x["ts_open"] >= FIX_TS]
    print(f"\n-- до/после фикса кулдауна (v0.18.12, 8 июня 07:45 UTC) --")
    for name, grp in (("до фикса", before), ("после фикса", after)):
        n = len(grp)
        net = sum(x["pnl_usd"] for x in grp)
        w = sum(1 for x in grp if x["pnl_usd"] > 0)
        wr = (100 * w / n) if n else 0
        print(f"  {name:<14} перезаходов={n:<3} WR={wr:>3.0f}% net={net:>8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
