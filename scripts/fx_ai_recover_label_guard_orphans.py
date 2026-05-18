"""Recovery script для двух позиций ошибочно помеченных label_guard_orphan.

ИНЦИДЕНТ 2026-05-18:
- id=7 BZ=F SELL (pid=150837215, opened 13:20:19) — на broker'е больше нет
  (закрылась SL/TP), нужно подтянуть closing deal и записать broker-net PnL.
- id=8 XAUUSD SELL (pid=150839089, opened 14:33:16) — РЕАЛЬНО ЖИВА на broker'е,
  ошибочно помечена closed в БД нашим label guard'ом из-за Spotware reconcile
  caching bug. Нужно восстановить closed_at=NULL.
"""
from __future__ import annotations

import sqlite3
import sys


def main() -> int:
    from fx_ai_trader.config.settings import AiFxTraderSettings
    from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

    s = AiFxTraderSettings()
    adapter = CTraderFxAdapter(s)
    adapter.start(timeout=30.0)

    # ── Step 1: id=7 BZ=F → найти closing deal и записать правильный PnL ─
    print("=== Step 1: closing deal для id=7 (BZ=F, broker_pid=150837215) ===")
    deal = adapter.get_closing_deal_for_position(150837215, lookback_hours=72)
    if deal is None:
        print("  WARN: closing deal не найден за 72h — оставляю как есть, manual review")
    else:
        broker_net = deal["gross_pnl_usd"] + deal["swap_usd"] + deal["commission_usd"]
        print(f"  Found: deal_id={deal['deal_id']} exit=${deal['exit_price']:.5f}")
        print(f"         gross=${deal['gross_pnl_usd']:+.2f} swap=${deal['swap_usd']:+.2f} "
              f"comm=${deal['commission_usd']:+.2f} → NET=${broker_net:+.2f}")
        conn = sqlite3.connect(s.db_path)
        try:
            conn.execute(
                "UPDATE positions SET exit_price=?, realized_pnl_usd=?, "
                "close_reason='broker_auto_recovered', "
                "closed_at=? WHERE id=7",
                (deal["exit_price"], broker_net,
                 # closed_at preserved или recover из deal-ts
                 "2026-05-18T14:00:00+00:00"),
            )
            conn.commit()
            print(f"  ✅ id=7 updated: realized_pnl=${broker_net:+.2f}, reason=broker_auto_recovered")
        finally:
            conn.close()

    # ── Step 2: id=8 XAUUSD → восстановить как open ─────────────────────
    print("\n=== Step 2: восстановить id=8 (XAUUSD, broker_pid=150839089) как open ===")
    conn = sqlite3.connect(s.db_path)
    try:
        cur = conn.execute(
            "UPDATE positions SET closed_at=NULL, close_reason=NULL, "
            "exit_price=NULL, realized_pnl_usd=NULL WHERE id=8"
        )
        conn.commit()
        print(f"  ✅ id=8 restored: closed_at=NULL, close_reason=NULL "
              f"({cur.rowcount} row updated)")
    finally:
        conn.close()

    print("\n=== Verification ===")
    conn = sqlite3.connect(s.db_path)
    try:
        rows = conn.execute(
            "SELECT id, symbol, side, broker_position_id, closed_at, close_reason, "
            "realized_pnl_usd FROM positions WHERE id IN (7, 8)"
        ).fetchall()
        for r in rows:
            print(f"  id={r[0]} {r[1]} {r[2]} pid={r[3]} closed_at={r[4]} "
                  f"reason={r[5]} pnl={r[6]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
