"""Прямая broker-side verification PnL формулы fx-ai-trader.

Запускать read-only внутри fx-ai-trader контейнера:

    docker exec fx-pro-bot-fx-ai-trader-1 \
        python scripts/fx_ai_verify_pnl_from_history.py

Что делает:
1. Подключается через ``CTraderFxAdapter`` (с auth refresh).
2. Запрашивает ``ProtoOADealListReq`` за последние 48 часов.
3. Для каждого закрытого XAUUSD / BRENT deal:
   - Берёт broker-side ``grossProfit`` из ``ProtoOAClosePositionDetail``
     (это **ground truth** от cTrader-бэкенда, рассчитанный их движком).
   - Параллельно считает наш PnL через ``_calc_pnl_usd`` с теми же
     entry / exit / volume.
   - Печатает обе цифры и delta.

Если delta < $1 для каждой сделки — формула fx-ai-trader верна.

Никаких побочных эффектов: не открывает и не закрывает позиций, не
пишет в БД.

Источники:
- cTrader Open API ProtoOADealListReq + ProtoOAClosePositionDetail —
  поля ``grossProfit``, ``swap``, ``commission``, ``entryPrice`` приходят
  как integer × 10^moneyDigits (см. compare_stats.py:50-56 и
  src/fx_pro_bot/trading/executor.py:599-627).
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter
from fx_ai_trader.trading.executor import (
    _calc_pnl_usd,
    _pip_size_for,
    _pip_value_per_std_lot,
)


def _scale_price(raw: int | float) -> float:
    """cTrader цены в proto обычно scaled на 10^5. Detect by magnitude.

    Если raw > 1_000_000 → scaled, делим на 100000.
    Иначе — уже real.
    """
    if isinstance(raw, (int, float)) and abs(raw) > 1_000_000:
        return float(raw) / 100_000.0
    return float(raw)


def main() -> int:
    settings = AiFxTraderSettings()
    adapter = CTraderFxAdapter(settings)
    print(f"=== Connecting cTrader (account {settings.ctrader_account_id}) ===")
    adapter.start(timeout=30.0)
    if not adapter.is_ready:
        print("Adapter not ready, abort.")
        return 1

    client = adapter._client  # noqa: SLF001 — diag-only
    if client is None:
        print("client is None, abort.")
        return 1

    # Resolve XAUUSD / BRENT symbolIds через catalog
    xau_info = adapter.get_symbol_info("XAUUSD")
    brent_info = adapter.get_symbol_info("BZ=F")
    target_ids: dict[int, tuple[str, int]] = {}
    if xau_info is not None:
        target_ids[xau_info.symbol_id] = ("XAUUSD", xau_info.contract_size)
    if brent_info is not None:
        target_ids[brent_info.symbol_id] = ("BZ=F", brent_info.contract_size)
    print(f"=== Target symbols (id → internal, contract_size): {target_ids} ===\n")

    # 48h окно
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - 48 * 3600 * 1000
    print(f"=== Querying deals from {from_ms} to {now_ms} (last 48h) ===\n")
    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=1000)

    deals = list(resp.deal) if hasattr(resp, "deal") else []
    print(f"=== Total deals returned: {len(deals)} ===\n")

    closing_target_deals = []
    for d in deals:
        if d.symbolId not in target_ids:
            continue
        if not d.HasField("closePositionDetail"):
            continue
        closing_target_deals.append(d)

    if not closing_target_deals:
        print("Нет закрытых XAUUSD / BRENT сделок за 48h. Расширь окно или подожди real-сделок.")
        adapter.stop()
        return 0

    print(f"=== Closing XAUUSD/BRENT deals: {len(closing_target_deals)} ===\n")

    total_abs_delta = 0.0
    total_broker_gross = 0.0
    total_our_calc = 0.0

    for d in closing_target_deals:
        internal, contract_size = target_ids[d.symbolId]
        cpd = d.closePositionDetail
        md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
        money_div = 10 ** md

        broker_gross = cpd.grossProfit / money_div
        broker_swap = cpd.swap / money_div
        broker_comm = cpd.commission / money_div

        entry = _scale_price(cpd.entryPrice)
        exit_p = _scale_price(getattr(d, "executionPrice", 0))
        volume_units = int(getattr(d, "filledVolume", 0))
        # cTrader broker volume → lots: divide by contract_size (cTrader lotSize)
        volume_lots = volume_units / contract_size if contract_size > 0 else 0.0

        # tradeSide closing deal — противоположен открытию. У cTrader
        # ProtoOADeal.tradeSide = side ЭТОГО исполнения. Для closing
        # deal: closingSide = opposite(openingSide), значит для расчёта
        # PnL открытой позиции мы должны взять "opening side":
        # если closing side = SELL → original BUY (long), и наоборот.
        closing_side_raw = int(getattr(d, "tradeSide", 0))
        # ProtoOATradeSide: 1 = BUY, 2 = SELL
        closing_side = "BUY" if closing_side_raw == 1 else "SELL"
        opening_side = "SELL" if closing_side == "BUY" else "BUY"

        our_calc = _calc_pnl_usd(
            side=opening_side,
            entry=entry,
            exit_price=exit_p,
            volume_lots=volume_lots,
            symbol=internal,
        )

        delta = our_calc - broker_gross
        total_abs_delta += abs(delta)
        total_broker_gross += broker_gross
        total_our_calc += our_calc

        print(
            f"deal_id={d.dealId} pos_id={d.positionId} {internal} "
            f"({opening_side}, vol_units={volume_units}, lots={volume_lots:.4f})\n"
            f"  entry={entry:.5f}  exit={exit_p:.5f}  "
            f"price_move={exit_p - entry:+.5f}\n"
            f"  broker:   gross=${broker_gross:+.2f}  swap=${broker_swap:+.2f}  comm=${broker_comm:+.2f}\n"
            f"  fx-ai-trader formula: ${our_calc:+.2f}  →  delta vs broker = ${delta:+.4f}\n"
            f"  pip_size={_pip_size_for(internal)}  pip_value=${_pip_value_per_std_lot(internal):.2f}/pip/lot\n"
        )

    print("=" * 70)
    print(
        f"SUMMARY ({len(closing_target_deals)} deals):\n"
        f"  Sum broker gross:    ${total_broker_gross:+.2f}\n"
        f"  Sum our calc:        ${total_our_calc:+.2f}\n"
        f"  Sum |delta|:         ${total_abs_delta:.4f}\n"
        f"  Avg |delta|/deal:    ${total_abs_delta / len(closing_target_deals):.4f}\n"
    )
    if total_abs_delta / len(closing_target_deals) < 1.0:
        print("✓ Formula matches broker within $1/deal — OK.")
    else:
        print("✗ Significant delta — formula likely incorrect. Review pip_value/pip_size.")
    print("=" * 70)

    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
