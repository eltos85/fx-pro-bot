"""Reconciliation между Bybit-state и нашей БД.

Содержит:

- ``reconcile_closed_positions`` — позиции, которые закрылись на бирже
  (SL / TP / manual / liquidation), но в БД ещё открыты. Подтягивает
  net PnL и avg exit price из ``client.get_closed_pnl``.
- ``reconcile_pending_pnl`` — позиции, ЗАКРЫТЫЕ ботом, но с PnL=NULL
  (биржа не успела зарегистрировать запись за 4 retry в момент
  ``_apply_close``). К следующему циклу запись точно появилась →
  добиваем PnL и обновляем ``daily_pnl``.

Этот модуль вынесен из ``app.main`` чтобы не тащить ``llm.client``
(и ``anthropic`` SDK) в unit-тестах reconcile-логики.
"""
from __future__ import annotations

import logging

from ai_arena.state.db import AiArenaStore
from ai_arena.telegram.bot import TelegramArenaBot
from ai_arena.trading.client import AiArenaBybitClient
from ai_arena.trading.executor import (
    _resolve_net_close,
    _resolve_pnl_from_balance_delta,
)

log = logging.getLogger("ai_arena")


def reconcile_closed_positions(
    client: AiArenaBybitClient,
    store: AiArenaStore,
    tg: TelegramArenaBot | None,
) -> None:
    """Если SL/TP/liquidation закрыли позицию на бирже — обновим БД + push.

    Защита от false-close при transient outage биржи: если
    ``get_positions(symbol)`` возвращает None — пропускаем символ
    целиком (не помечаем closed).

    PnL и exit_price берутся из Bybit ``get_closed_pnl`` (net после
    fees + funding) через ``_resolve_net_close`` — 1-в-1 с биржей.
    Локальный ``(exit-entry)*qty`` запрещён (BUILDLOG 2026-05-15).
    """
    open_db = store.get_open_positions()
    if not open_db:
        return

    api_positions_by_symbol: dict[str, list] = {}
    failed_symbols: set[str] = set()
    for sym in {p.symbol for p in open_db}:
        positions = client.get_positions(symbol=sym)
        if positions is None:
            failed_symbols.add(sym)
            log.warning(
                "RECONCILE skipped for %s: get_positions=None (API outage)", sym
            )
            continue
        api_positions_by_symbol[sym] = list(positions)

    for db_pos in open_db:
        if db_pos.symbol in failed_symbols:
            continue
        api_list = api_positions_by_symbol.get(db_pos.symbol, [])
        still_open = any(
            p.side == db_pos.side and abs(p.size - db_pos.qty) < 1e-6
            for p in api_list
        )
        if still_open:
            continue
        exit_price, pnl = _resolve_net_close(
            client=client,
            symbol=db_pos.symbol,
            opened_at_iso=db_pos.opened_at,
            opened_side=db_pos.side,
            qty=db_pos.qty,
            fallback_entry=db_pos.entry_price,
        )
        # pnl=None → биржа ещё не зарегистрировала запись; пишем
        # позицию с PnL=NULL, reconcile_pending_pnl на след. цикле
        # подберёт. Telegram уведомление помечается «pending».
        store.close_position(
            db_pos.id,
            exit_price=exit_price,
            realized_pnl_usd=pnl,
            close_reason="exchange_closed (SL/TP/manual)",
        )
        if pnl is None:
            msg = (
                f"id={db_pos.id} {db_pos.side} {db_pos.symbol} qty={db_pos.qty}\n"
                f"entry=${db_pos.entry_price:.6g} exit=${exit_price:.6g}\n"
                f"PnL: pending… (биржа задержала, добьём на след. цикле)\n"
                f"Reason: exchange_closed"
            )
        else:
            msg = (
                f"id={db_pos.id} {db_pos.side} {db_pos.symbol} qty={db_pos.qty}\n"
                f"entry=${db_pos.entry_price:.6g} exit=${exit_price:.6g}\n"
                f"PnL: ${pnl:+.2f} (net of fees)\nReason: exchange_closed"
            )
        log.info("RECONCILE closed: %s", msg.replace("\n", " | "))
        if tg:
            tg.notify_close(msg)


def reconcile_pending_pnl(
    client: AiArenaBybitClient,
    store: AiArenaStore,
    tg: TelegramArenaBot | None,
) -> None:
    """Добивает net PnL для позиций, закрытых ботом с PnL=NULL.

    Два пути добивания (в порядке приоритета):

    1. **Primary**: Bybit ``/v5/position/closed-pnl`` (3 retry, ≤6s).
       Если запись появилась — берём `closedPnl` + `avgExitPrice`.
    2. **Fallback**: balance delta — net_pnl = wallet_now - wallet_before
       (где `wallet_balance_before_close` сохранён в `positions` при
       `_apply_close`). Срабатывает только для позиций закрытых
       ботом (для exchange-инициированных closes wallet_before
       отсутствует — fallback недоступен, остаётся ждать closed-pnl).

    Bybit demo `/v5/position/closed-pnl` имеет наблюдаемую latency
    **до 5+ минут** (BUILDLOG 2026-05-15). Без balance-delta fallback
    pending-сообщения копились бы пока demo agg-job не сработает.
    """
    pending = store.get_pending_pnl_positions()
    if not pending:
        return
    for pos in pending:
        exit_price, pnl = _resolve_net_close(
            client=client,
            symbol=pos.symbol,
            opened_at_iso=pos.opened_at,
            opened_side=pos.side,
            qty=pos.qty,
            fallback_entry=pos.entry_price,
            max_retries=3,  # уже прошло >180s — короткий retry
            retry_backoff_sec=(1.0, 2.0, 3.0),
        )
        # Fallback: balance delta (только если бот закрыл и сохранил
        # wallet_before). Для exchange-инициированных closes wallet_before
        # = NULL → fallback пропускаем, ждём closed-pnl.
        used_fallback = False
        if pnl is None and pos.wallet_balance_before_close is not None:
            pnl = _resolve_pnl_from_balance_delta(
                client=client,
                wallet_before=pos.wallet_balance_before_close,
                position_id=pos.id,
                symbol=pos.symbol,
                settle_wait_sec=0.0,  # на этом цикле уже >180s, fill точно прошёл
            )
            used_fallback = pnl is not None
        if pnl is None:
            log.warning(
                "PENDING-PNL: id=%d %s %s ещё не виден в Bybit closed_pnl "
                "(wallet_before=%s) — повторим на след. цикле",
                pos.id, pos.side, pos.symbol,
                pos.wallet_balance_before_close,
            )
            continue
        store.finalize_pending_pnl(
            pos.id, exit_price=exit_price, realized_pnl_usd=pnl,
        )
        source = "balance-delta" if used_fallback else "closed-pnl"
        log.info(
            "PENDING-PNL resolved via %s: id=%d %s %s exit=$%.6g pnl=$%+.2f (net)",
            source, pos.id, pos.side, pos.symbol, exit_price, pnl,
        )
        if tg:
            tg.notify_close(
                f"PnL добит для id={pos.id} {pos.side} {pos.symbol}: "
                f"${pnl:+.2f} (net of fees, via {source})"
            )
