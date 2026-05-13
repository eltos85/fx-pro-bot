"""Полный дамп positions + decisions в fx-ai-trader БД."""
from __future__ import annotations
import sqlite3
import sys

from fx_ai_trader.config.settings import AiFxTraderSettings


def main() -> int:
    s = AiFxTraderSettings()
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row

    print("=" * 72)
    print("ALL positions in fx-ai-trader DB")
    print("=" * 72)
    cur = conn.execute(
        """SELECT id, symbol, side, volume_lots, entry_price, exit_price,
                  realized_pnl_usd, opened_at, closed_at,
                  broker_position_id, is_paper, close_reason, llm_reason
           FROM positions ORDER BY id DESC"""
    )
    for r in cur.fetchall():
        print(f"\n  id={r['id']} symbol={r['symbol']} side={r['side']} "
              f"lots={r['volume_lots']} paper={r['is_paper']} "
              f"broker_pid={r['broker_position_id']}")
        print(f"    entry={r['entry_price']} exit={r['exit_price']} "
              f"pnl=${r['realized_pnl_usd']}")
        print(f"    opened={r['opened_at']}")
        print(f"    closed={r['closed_at']}")
        print(f"    open_reason: {(r['llm_reason'] or '')[:200]}")
        print(f"    close_reason: {(r['close_reason'] or '')[:200]}")

    print()
    print("=" * 72)
    print("ALL decisions (action summary) — newest first")
    print("=" * 72)
    cur = conn.execute(
        """SELECT id, cycle, cycle_type, ts, parsed_action, executed, error
           FROM decisions ORDER BY id DESC"""
    )
    for r in cur.fetchall():
        pa = (r["parsed_action"] or "")[:240]
        print(f"\n  dec_id={r['id']} cyc={r['cycle']} type={r['cycle_type']} "
              f"ts={r['ts']} exec={r['executed']} err={r['error']}")
        print(f"    action: {pa}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
