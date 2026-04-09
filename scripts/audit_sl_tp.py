"""Аудит: вывести все открытые позиции с их SL/TP статусом."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fx_pro_bot.trading.client import CTraderClient

tokens_path = os.environ.get("CTRADER_TOKEN_PATH", "/data/ctrader_tokens.json")
if not os.path.exists(tokens_path):
    tokens_path = os.path.join(os.path.dirname(__file__), "..", "data", "ctrader_tokens.json")
tokens = json.loads(open(tokens_path).read())

client = CTraderClient(
    client_id=os.environ["CTRADER_CLIENT_ID"],
    client_secret=os.environ["CTRADER_CLIENT_SECRET"],
    access_token=tokens["access_token"],
    account_id=int(os.environ["CTRADER_ACCOUNT_ID"]),
    host_type="demo",
)

client.start(timeout=30, retries=2)

sym_resp = client.get_symbols()
sym_map = {s.symbolId: getattr(s, "symbolName", str(s.symbolId)) for s in sym_resp.symbol}

resp = client.reconcile()
positions = list(resp.position)
print(f"Total open: {len(positions)}\n")

header = f"{'POS_ID':<14} {'SYMBOL':<12} {'SIDE':<5} {'ENTRY':>12} {'SL':>12} {'TP':>12}  FLAGS"
print(header)
print("-" * 80)

no_sl = 0
no_tp = 0
for p in positions:
    td = p.tradeData
    name = sym_map.get(td.symbolId, "?")
    side = "BUY" if td.tradeSide == 1 else "SELL"
    has_sl = p.HasField("stopLoss")
    has_tp = p.HasField("takeProfit")
    sl_s = f"{p.stopLoss:.5f}" if has_sl else "---"
    tp_s = f"{p.takeProfit:.5f}" if has_tp else "---"
    flags = ""
    if not has_sl:
        no_sl += 1
        flags += " NO_SL"
    if not has_tp:
        no_tp += 1
        flags += " NO_TP"
    print(f"{p.positionId:<14} {name:<12} {side:<5} {p.price:>12.5f} {sl_s:>12} {tp_s:>12}  {flags}")

print(f"\nБез SL: {no_sl}/{len(positions)}")
print(f"Без TP: {no_tp}/{len(positions)}")
client.stop()
