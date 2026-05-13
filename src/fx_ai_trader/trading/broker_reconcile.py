"""Broker-side reconcile для FX AI Trader.

Закрывает в БД live-позиции, которые **broker уже закрыл** (SL/TP сработали
на стороне cTrader), но локальная БД об этом не знает.

Симптом без reconcile (наблюдалось 2026-05-13 на pos id=3, BRENT BUY):
- LLM открыла позицию с SL=104.7, broker исполнил.
- Цена пробила SL → cTrader auto-close → broker_pid больше не активен.
- Бот опросил БД, увидел ``closed_at IS NULL`` и считал позицию открытой.
- В каждом review-цикле LLM решала CLOSE → ``close_position()`` →
  ``POSITION_NOT_FOUND`` → бот не записывал close в БД → бесконечный
  цикл фантомных попыток (9 циклов подряд в нашей продакшен-БД).

Финансовое последствие: ``realized_pnl_usd`` минусовых broker-closed
позиций НЕ попадает в ``daily_pnl`` → KillSwitch ``max_daily_loss_usd``
видит ноль вместо реальных потерь. На live — реальные деньги.

Решение (Two-Phase Commit alternative — async reconcile pattern):

1. Один раз за цикл (full + review) запрашиваем у broker'а активные
   position IDs (``client.reconcile()``).
2. Для каждой live-позиции в локальной БД, у которой broker_pid отсутствует
   в активном set'е → дёргаем ``get_deal_list`` чтобы найти closing deal с
   broker-точным ``grossProfit``, ``swap``, ``commission``.
3. Закрываем в БД с broker-true ``exit_price`` / ``realized_pnl_usd`` /
   ``close_reason='broker_auto'``.

Альтернатива (real-time ProtoOAExecutionEvent listener) — в backlog,
требует event-stream pipeline. Polling-подход прост, надёжен и достаточен
на dual-timer 5/15-минутном цикле.
"""
from __future__ import annotations

import logging

from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter
from fx_ai_trader.trading.executor import _calc_pnl_usd

log = logging.getLogger(__name__)


def reconcile_broker_positions(
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
) -> int:
    """Синхронизирует live-позиции в БД с активными у broker'а.

    Returns количество позиций, помеченных как closed по факту broker-close.
    Безопасно при недоступности broker'а: ``None`` от
    ``get_active_broker_position_ids`` → no-op (НЕ закрываем фантомно).
    """
    db_open = [
        p for p in store.get_open_positions()
        if not p.is_paper and p.broker_position_id is not None
    ]
    if not db_open:
        return 0

    active = adapter.get_active_broker_position_ids()
    if active is None:
        log.warning(
            "broker reconcile: get_active_broker_position_ids() вернул None "
            "(API недоступно) — пропускаю sync, не закрываю фантомно"
        )
        return 0

    closed_count = 0
    for pos in db_open:
        if pos.broker_position_id in active:
            continue
        log.info(
            "broker reconcile: позиция id=%d (broker_pid=%d, %s %s lots=%s) "
            "закрыта broker'ом сам — ищу closing deal",
            pos.id, pos.broker_position_id, pos.side, pos.symbol,
            pos.volume_lots,
        )
        deal = adapter.get_closing_deal_for_position(
            pos.broker_position_id, lookback_hours=48,
        )
        if deal is None:
            log.warning(
                "broker reconcile: closing deal для broker_pid=%d не "
                "найден за 48h — оставляю позицию open в БД (manual review)",
                pos.broker_position_id,
            )
            continue
        broker_gross = deal["gross_pnl_usd"]
        broker_swap = deal["swap_usd"]
        broker_comm = deal["commission_usd"]
        broker_net = broker_gross + broker_swap + broker_comm
        exit_price = deal["exit_price"]
        our_calc = _calc_pnl_usd(
            side=pos.side, entry=pos.entry_price,
            exit_price=exit_price, volume_lots=pos.volume_lots,
            symbol=pos.symbol,
        )
        log.info(
            "broker reconcile: id=%d closing_deal_id=%d exit=$%.5f "
            "broker_gross=$%+.2f swap=$%+.2f comm=$%+.2f net=$%+.2f "
            "(our_formula=$%+.2f, delta=$%+.4f)",
            pos.id, deal["deal_id"], exit_price,
            broker_gross, broker_swap, broker_comm, broker_net,
            our_calc, our_calc - broker_gross,
        )
        store.close_position(
            pos.id,
            exit_price=exit_price,
            realized_pnl_usd=broker_net,
            close_reason="broker_auto",
        )
        closed_count += 1

    if closed_count > 0:
        log.info("broker reconcile: закрыто %d позиций по broker-true PnL", closed_count)
    return closed_count
