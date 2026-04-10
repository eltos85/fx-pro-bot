"""Сравнение cTrader deal history с DB за последние 2 часа."""
import sys
import sqlite3
import time

sys.path.insert(0, "/app/src")

from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.auth import TokenStore
from fx_pro_bot.config.settings import Settings
from pathlib import Path

settings = Settings()
ts = TokenStore(Path("/data/ctrader_tokens.json"))
tokens = ts.load()
client = CTraderClient(
    client_id=settings.ctrader_client_id,
    client_secret=settings.ctrader_client_secret,
    access_token=tokens.access_token,
    account_id=settings.ctrader_account_id,
)
client.start()
time.sleep(8)

now_ms = int(time.time() * 1000)
two_h_ago = now_ms - 2 * 3600 * 1000

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOADealListReq,
    ProtoOADealListRes,
)

req = ProtoOADealListReq()
req.ctidTraderAccountId = settings.ctrader_account_id
req.fromTimestamp = two_h_ago
req.toTimestamp = now_ms
req.maxRows = 500
resp = client._send_and_wait(req, ProtoOADealListRes().payloadType, timeout=30)

deals = list(resp.deal)
print(f"=== cTrader deals за 2 часа: {len(deals)} ===")

sym_resp = client.get_symbols()
sym_map = {}
for s in sym_resp.symbol:
    sym_map[s.symbolId] = s.symbolName if hasattr(s, "symbolName") else str(s.symbolId)

close_deals = []
for d in deals:
    cpd = d.closePositionDetail if d.HasField("closePositionDetail") else None
    if cpd is None:
        continue
    md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
    div = 10**md
    gross = cpd.grossProfit / div
    comm = cpd.commission / div
    swap = cpd.swap / div
    net = gross + comm + swap
    sym_name = sym_map.get(d.symbolId, f"id#{d.symbolId}")
    side = "BUY" if d.tradeSide == 1 else "SELL"
    close_deals.append(
        (d.positionId, sym_name, side, gross, comm, swap, net, d.executionTimestamp)
    )

total_gross = 0
total_net = 0
print()
print("Закрытия (cTrader):")
for pid, sym, side, gross, comm, swap, net, ts_ms in sorted(
    close_deals, key=lambda x: x[7]
):
    total_gross += gross
    total_net += net
    t = time.strftime("%H:%M", time.gmtime(ts_ms / 1000))
    print(
        f"  {t} #{pid} {sym:<10} {side} gross={gross:>+6.2f} comm={comm:>+5.2f} net={net:>+6.2f}"
    )

print()
print(
    f"cTrader итого: gross={total_gross:+.2f} net={total_net:+.2f} ({len(close_deals)} закрытий)"
)

# DB comparison
db = sqlite3.connect("/data/advisor_stats.sqlite")
db_closed = db.execute(
    "SELECT broker_position_id, strategy, instrument, direction, profit_pips, exit_reason "
    "FROM positions WHERE status = 'closed' AND closed_at >= datetime('now', '-2 hours') "
    "ORDER BY closed_at DESC"
).fetchall()

print()
print(f"=== DB закрытые за 2 часа: {len(db_closed)} ===")
for r in db_closed:
    pips = r[4] or 0
    print(f"  broker#{r[0]} {r[1]:<15} {r[2]:<12} {r[3]:<5} {pips:>+7.1f} pips  [{r[5]}]")

broker_ids_ct = {d[0] for d in close_deals}
broker_ids_db = {r[0] for r in db_closed if r[0]}
missing_in_db = broker_ids_ct - broker_ids_db
missing_in_ct = broker_ids_db - broker_ids_ct
print()
print(f"В cTrader но не в DB: {len(missing_in_db)} — {missing_in_db or 'нет'}")
print(f"В DB но не в cTrader: {len(missing_in_ct)} — {missing_in_ct or 'нет'}")

# Open comparison
broker_open = client.reconcile()
broker_pos = list(broker_open.position) if hasattr(broker_open, "position") else []
broker_open_ids = {p.positionId for p in broker_pos}
db_open = db.execute(
    "SELECT broker_position_id FROM positions WHERE status = 'open' AND broker_position_id > 0"
).fetchall()
db_open_ids = {r[0] for r in db_open}

print()
print(f"Открытые: broker={len(broker_open_ids)}, DB={len(db_open_ids)}")
orphan_broker = broker_open_ids - db_open_ids
orphan_db = db_open_ids - broker_open_ids
if orphan_broker:
    print(f"  На брокере но не в DB: {orphan_broker}")
if orphan_db:
    print(f"  В DB но не на брокере: {orphan_db}")
if not orphan_broker and not orphan_db:
    print("  Синхронизация ОК")

# SL/TP check on open
no_sl = 0
no_tp = 0
for p in broker_pos:
    has_sl = hasattr(p, "stopLoss") and p.HasField("stopLoss")
    has_tp = hasattr(p, "takeProfit") and p.HasField("takeProfit")
    if not has_sl:
        no_sl += 1
    if not has_tp:
        no_tp += 1
print(f"  SL/TP: без SL={no_sl}, без TP={no_tp}")

client.stop()
