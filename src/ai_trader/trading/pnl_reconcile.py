"""v0.18 (2026-05-25): синхронизация realized_pnl_usd с биржевым net.

Bot до v0.18 писал в БД ``realized_pnl_usd = (exit - entry) * qty`` —
это **gross** PnL без trading fee и без funding settlement. На сегодня
25/05 расхождение с фактическим биржевым PnL составило 41% (БД -2.38,
биржа net -4.40 за день из 5 закрытий).

После v0.18 поверх gross-расчёта мы дёргаем Bybit
``/v5/position/closed-pnl`` и заменяем на точное ``closedPnl`` (net).

Helper здесь shared между:
- ``executor._apply_close`` (моментальный fetch сразу после close)
- ``main._reconcile_closed_positions`` (после exchange-close через SL/TP)
- ``main._reconcile_pnl_to_net`` (догон для gross-записей если API
  падал в момент close)

Стратегия матчинга:
1. По ``orderLinkId``: каждая позиция бота имеет уникальный
   ``ai_open_*`` link_id (он же сохраняется как ``orderLinkId`` на
   первом execution). Bybit ставит этот же link_id на closed-pnl
   запись для **первого** исполнения позиции. Это самый надёжный путь.
2. Fallback по ``closedSize ≈ qty`` AND ``createdTime ≥ opened_at``:
   если link_id в API не нашёлся (бывает при reconcile через
   exchange-close, у которого свой close-link_id).

Возвращает (closed_pnl, avg_exit_price) или None.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_trader.state.db import AiPosition
    from ai_trader.trading.client import AiBybitClient, ClosedPnl

log = logging.getLogger(__name__)


def _opened_at_ms(opened_at_iso: str) -> int:
    """ISO timestamp из БД → ms для startTime фильтра API."""
    try:
        dt = datetime.fromisoformat(opened_at_iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def fetch_net_pnl(
    client: "AiBybitClient",
    position: "AiPosition",
    *,
    qty_tolerance: float = 1e-6,
) -> tuple[float, float] | None:
    """Возвращает ``(closed_pnl_net, avg_exit_price)`` для закрытой позиции.

    None если:
    - API упал (transient outage) — caller должен оставить gross.
    - Запись в Bybit ещё не появилась (биржа может задержать на 1-2с
      после close) — caller fallback на gross + догонит позже через
      ``_reconcile_pnl_to_net``.
    - Запись найдена, но не матчится (что подозрительно) — пропуск.

    qty_tolerance — для float-сравнения closedSize с qty в БД.
    """
    opened_ms = _opened_at_ms(position.opened_at)
    # 60s slack назад от opened_at — на случай если closed-pnl запись
    # имеет createdTime близкое к opened_at и попадает на границу.
    start_ms = max(opened_ms - 60_000, 0) if opened_ms else None
    items = client.get_closed_pnl(
        position.symbol, start_ms=start_ms, limit=50
    )
    if items is None:
        log.warning(
            "fetch_net_pnl: API failure for %s id=%d (gross fallback)",
            position.symbol, position.id,
        )
        return None
    if not items:
        log.info(
            "fetch_net_pnl: no closed-pnl yet for %s id=%d "
            "(биржа ещё не обновила, попробуем в reconcile)",
            position.symbol, position.id,
        )
        return None

    # Пытаемся через orderLinkId (наш ai_open_…). Это самый надёжный путь,
    # потому что Bybit ставит этот link_id на первой записи позиции.
    candidates: list["ClosedPnl"] = []
    for it in items:
        if it.order_link_id == position.order_link_id:
            candidates.append(it)
    if not candidates:
        # Fallback: матчинг по closedSize + side + createdTime после opened_at.
        # Bybit side в closed-pnl — это сторона **закрывающего** ордера
        # (для Buy-позиции close сделан Sell-ордером). Поэтому
        # invert: closed-pnl side != position.side.
        invert = {"Buy": "Sell", "Sell": "Buy"}.get(position.side)
        for it in items:
            if it.side != invert:
                continue
            if abs(it.closed_size - position.qty) > qty_tolerance:
                continue
            if opened_ms and it.created_time_ms < opened_ms - 60_000:
                continue
            candidates.append(it)
    if not candidates:
        log.warning(
            "fetch_net_pnl: no matching closed-pnl for %s id=%d link=%s "
            "(returned %d items, none matched) — gross fallback",
            position.symbol, position.id, position.order_link_id, len(items),
        )
        return None
    # Если несколько кандидатов — берём с наибольшим updatedTime
    # (последнее обновление = финальная запись после всех partial-fills).
    chosen = max(candidates, key=lambda c: c.updated_time_ms)
    return chosen.closed_pnl, chosen.avg_exit_price
