"""Полный разбор позиции id=3 (BRENT BUY, opened 15:20 UTC, stale в БД).

Делает:
1. Дамп row из positions (наша БД).
2. Дёргает get_deal_list через broker — ищет closing deal для
   broker_position_id=150428404 (реальный close cTrader-а).
3. Печатает response_raw для OPEN decision и ВСЕХ CLOSE-попыток
   (executed или err'нувших).
"""
from __future__ import annotations
import sqlite3
import sys
import time

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter
from fx_ai_trader.trading.executor import _calc_pnl_usd

TARGET_POS_ID = 3
TARGET_BROKER_PID = 150428404


def main() -> int:
    s = AiFxTraderSettings()
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row

    cur = conn.execute(
        "SELECT * FROM positions WHERE id = ?", (TARGET_POS_ID,)
    )
    pos = cur.fetchone()
    if pos is None:
        print(f"Позиция id={TARGET_POS_ID} не найдена")
        return 1
    print("=" * 72)
    print(f"POSITION (DB row, id={TARGET_POS_ID})")
    print("=" * 72)
    for k in pos.keys():
        print(f"  {k}: {pos[k]}")
    print()

    print("=" * 72)
    print("BROKER: closing deal для broker_pid={}".format(TARGET_BROKER_PID))
    print("=" * 72)
    adapter = CTraderFxAdapter(s)
    adapter.start(timeout=30.0)
    if not adapter.is_ready:
        print("Adapter not ready")
        return 1
    client = adapter._client  # noqa: SLF001
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - 24 * 3600 * 1000
    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=1000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []
    print(f"  total deals last 24h: {len(deals)}")
    found = []
    for d in deals:
        if d.HasField("closePositionDetail") and int(d.positionId) == TARGET_BROKER_PID:
            cpd = d.closePositionDetail
            md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
            div = 10 ** md
            found.append({
                "dealId": d.dealId,
                "symbolId": d.symbolId,
                "executionTimestamp": d.executionTimestamp,
                "filledVolume": d.filledVolume,
                "tradeSide": d.tradeSide,
                "executionPrice": float(getattr(d, "executionPrice", 0)),
                "entryPrice": float(getattr(cpd, "entryPrice", 0)),
                "grossProfit": cpd.grossProfit / div,
                "swap": cpd.swap / div,
                "commission": cpd.commission / div,
                "balance": cpd.balance / div,
            })
    if not found:
        print("  closing deal не найден за последние 24h")
    else:
        for f in found:
            print(f"  deal_id={f['dealId']} ts={f['executionTimestamp']} "
                  f"vol={f['filledVolume']} side={f['tradeSide']}")
            print(f"  entry={f['entryPrice']} exit={f['executionPrice']}  "
                  f"broker gross=${f['grossProfit']:.2f}  swap=${f['swap']:.2f}  "
                  f"comm=${f['commission']:.2f}  balance_after=${f['balance']:.2f}")
            our_calc = _calc_pnl_usd(
                side="BUY",
                entry=f["entryPrice"],
                exit_price=f["executionPrice"],
                volume_lots=pos["volume_lots"],
                symbol="BZ=F",
            )
            print(f"  our formula on broker data: ${our_calc:.2f}")
    print()
    adapter.stop()

    print("=" * 72)
    print("OPEN DECISION (full LLM response)")
    print("=" * 72)
    cur = conn.execute(
        """SELECT id, cycle, cycle_type, ts, parsed_action, response_raw,
                  executed, error
           FROM decisions
           WHERE parsed_action LIKE '%open%'
             AND parsed_action LIKE '%BZ=F%'
             AND parsed_action LIKE '%0.01%'
           ORDER BY id DESC LIMIT 1"""
    )
    od = cur.fetchone()
    if od is None:
        print("  не найден")
    else:
        print(f"  dec_id={od['id']} cycle={od['cycle']} type={od['cycle_type']} "
              f"ts={od['ts']} executed={od['executed']} error={od['error']}")
        print(f"  parsed_action: {od['parsed_action']}")
        print("  --- response_raw ---")
        print(od["response_raw"] or "<empty>")

    print()
    print("=" * 72)
    print("ALL CLOSE attempts (decisions targeting position_id=3)")
    print("=" * 72)
    cur = conn.execute(
        """SELECT id, cycle, cycle_type, ts, parsed_action, response_raw,
                  executed, error
           FROM decisions
           WHERE (parsed_action LIKE '%"position_id": 3%'
                  OR parsed_action LIKE '%"position_id":3%'
                  OR parsed_action LIKE '%position_id": 3,%')
           ORDER BY id ASC"""
    )
    close_attempts = list(cur.fetchall())
    print(f"  total close attempts: {len(close_attempts)}\n")
    for cd in close_attempts:
        print("-" * 72)
        print(f"dec_id={cd['id']} cycle={cd['cycle']} type={cd['cycle_type']} "
              f"ts={cd['ts']}")
        print(f"  executed={cd['executed']}  error={cd['error']}")
        print(f"  parsed_action: {cd['parsed_action']}")

    if close_attempts:
        print()
        print("=" * 72)
        print("LAST CLOSE attempt — full LLM response")
        print("=" * 72)
        last = close_attempts[-1]
        print(f"dec_id={last['id']}  cycle={last['cycle']}  ts={last['ts']}")
        print(f"executed={last['executed']}  error={last['error']}")
        print("--- response_raw ---")
        print(last["response_raw"] or "<empty>")

    return 0


if __name__ == "__main__":
    sys.exit(main())
