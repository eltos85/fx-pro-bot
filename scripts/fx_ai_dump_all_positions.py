"""Diagnostic: dump ALL open positions on cTrader account (без label-фильтра).

Используется когда подозреваем discrepancy между нашей БД и broker'ом —
например пользователь видит позицию в cTrader Web, а наш бот говорит
"нет позиций". Этот скрипт показывает СЫРОЙ ProtoOAReconcileRes:
все открытые позиции с любым label.

Использует CTraderFxAdapter (тот же путь что fx-ai-trader/fx-ai-trend
в продакшене → через ctrader-token-service для токена).

Запуск (внутри docker контейнера fx-ai-trader):
  docker exec fx-pro-bot-fx-ai-trader-1 python /tmp/fx_ai_dump_all_positions.py
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    from fx_ai_trader.config.settings import AiFxTraderSettings
    from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

    s = AiFxTraderSettings()
    adapter = CTraderFxAdapter(s)
    adapter.start(timeout=30.0)

    client = adapter._client
    if client is None:
        print("ERROR: adapter._client is None after start()", file=sys.stderr)
        return 1
    resp = client.reconcile()
    positions = list(resp.position)
    orders = list(resp.order)
    print(f"=== ProtoOAReconcileRes: {len(positions)} open positions, {len(orders)} pending orders ===")
    if not positions:
        print("(no open positions at all on this account)")
    for i, p in enumerate(positions):
        # Defensive: dump ALL fields через protobuf DESCRIPTOR, без
        # предположений о структуре (некоторые поля могут быть nested,
        # например tradeData.tradeSide).
        print(f"\n[{i}] === ProtoOAPosition ===")
        for field, value in p.ListFields():
            print(f"    {field.name} = {value!r}")
    print("\n=== Pending orders ===")
    if not orders:
        print("(no pending orders)")
    for o in orders:
        print(repr(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())
