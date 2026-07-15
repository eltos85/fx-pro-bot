"""Простая стата scalp_bot из БД: по дням и по стратегиям, с указанной даты.

Реальные сделки = status='closed' и close_reason НЕ entry_*/restart_flat
(т.е. позиция реально открылась и закрылась). pnl_usd — как записал бот
(net с учётом комиссий, см. executor). Read-only.

    docker exec fx-pro-bot-scalp-bot-1 python3 /tmp/scalp_daily_stats.py --since 2026-07-10
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime


def _agg(items: list[dict]) -> tuple[int, int, float, float]:
    n = len(items)
    w = sum(1 for x in items if x["pnl_usd"] > 0)
    net = sum(x["pnl_usd"] for x in items)
    wr = (100 * w / n) if n else 0.0
    return n, w, wr, net


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-07-10")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC).timestamp()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol, side, pnl_usd, close_reason, strategy, ts_open, "
        "ts_close, status FROM trades WHERE ts_open >= ?", (since,))]
    con.close()

    real = [r for r in rows
            if r["status"] == "closed"
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["close_reason"] != "restart_flat"
            and r["pnl_usd"] is not None]

    print(f"Период с {args.since} (UTC) | реальных сделок: {len(real)}")

    days: dict[str, list[dict]] = {}
    for r in real:
        d = datetime.fromtimestamp(r["ts_close"] or r["ts_open"], UTC).strftime("%Y-%m-%d")
        days.setdefault(d, []).append(r)

    print("\n=== ПО ДНЯМ (UTC) ===")
    print(f"{'день':<12}{'сделок':>7}{'вины':>6}{'WR%':>7}{'net$':>10}")
    tot = 0.0
    for d in sorted(days):
        n, w, wr, net = _agg(days[d])
        tot += net
        print(f"{d:<12}{n:>7}{w:>6}{wr:>6.0f}%{net:>10.2f}")
    print(f"{'ИТОГО':<12}{len(real):>7}{'':>6}{'':>7}{tot:>10.2f}")

    st: dict[str, list[dict]] = {}
    for r in real:
        st.setdefault(r["strategy"] or "?", []).append(r)

    print("\n=== ПО СТРАТЕГИЯМ ===")
    print(f"{'страта':<16}{'сделок':>7}{'вины':>6}{'WR%':>7}{'net$':>10}")
    for s in sorted(st, key=lambda k: -_agg(st[k])[3]):
        n, w, wr, net = _agg(st[s])
        print(f"{s:<16}{n:>7}{w:>6}{wr:>6.0f}%{net:>10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
