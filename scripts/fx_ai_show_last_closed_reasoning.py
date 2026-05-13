"""Показать LLM thinking для последней закрытой позиции."""
from __future__ import annotations
import sqlite3
import sys

from fx_ai_trader.config.settings import AiFxTraderSettings


def main() -> int:
    s = AiFxTraderSettings()
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row

    cur = conn.execute(
        """SELECT id, symbol, side, volume_lots, entry_price, exit_price,
                  realized_pnl_usd, opened_at, closed_at, close_reason,
                  llm_reason, broker_position_id, is_paper
           FROM positions
           WHERE closed_at IS NOT NULL
           ORDER BY closed_at DESC
           LIMIT 1"""
    )
    p = cur.fetchone()
    if p is None:
        print("Нет закрытых позиций")
        return 0

    print("=" * 72)
    print(f"LAST CLOSED POSITION (id={p['id']})")
    print("=" * 72)
    print(f"  symbol/side/lots   {p['symbol']} {p['side']} {p['volume_lots']}")
    print(f"  entry → exit       {p['entry_price']} → {p['exit_price']}")
    print(f"  PnL (idealized)    ${p['realized_pnl_usd']:.2f}")
    print(f"  opened/closed      {p['opened_at']}  →  {p['closed_at']}")
    print(f"  broker_pos_id      {p['broker_position_id']}  is_paper={p['is_paper']}")
    print(f"  close_reason       {p['close_reason']}")
    print(f"  llm_reason (open)  {p['llm_reason']}")
    print()

    cur = conn.execute(
        """SELECT id, cycle, cycle_type, ts, parsed_action, response_raw,
                  executed, error
           FROM decisions
           ORDER BY id DESC
           LIMIT 80"""
    )
    rows = list(cur.fetchall())

    open_dec = None
    close_dec = None
    pid = p["id"]
    for r in rows:
        pa = r["parsed_action"] or ""
        if "CLOSE" in pa and (f'"position_id":{pid}' in pa.replace(" ", "") or
                              f'"id":{pid}' in pa.replace(" ", "")):
            if close_dec is None:
                close_dec = r
        if "OPEN" in pa and ("BZ=F" in pa or "BRENT" in pa):
            if open_dec is None:
                open_dec = r

    if open_dec is None:
        for r in rows:
            pa = r["parsed_action"] or ""
            if '"action":"OPEN"' in pa.replace(" ", "") and (
                "BZ=F" in pa or "BRENT" in pa
            ):
                open_dec = r
                break

    print("=" * 72)
    print("OPEN DECISION (full LLM response)")
    print("=" * 72)
    if open_dec:
        print(f"cycle={open_dec['cycle']}  ts={open_dec['ts']}  "
              f"executed={open_dec['executed']}  error={open_dec['error']}")
        print(f"parsed_action: {open_dec['parsed_action']}")
        print()
        print("--- response_raw ---")
        print(open_dec["response_raw"] or "<empty>")
    else:
        print("не нашёл OPEN-decision для BRENT в последних 80 циклах")

    print()
    print("=" * 72)
    print("CLOSE DECISION (full LLM response)")
    print("=" * 72)
    if close_dec:
        print(f"cycle={close_dec['cycle']}  ts={close_dec['ts']}  "
              f"executed={close_dec['executed']}  error={close_dec['error']}")
        print(f"parsed_action: {close_dec['parsed_action']}")
        print()
        print("--- response_raw ---")
        print(close_dec["response_raw"] or "<empty>")
    else:
        print("не нашёл CLOSE-decision для последней позиции")

    return 0


if __name__ == "__main__":
    sys.exit(main())
