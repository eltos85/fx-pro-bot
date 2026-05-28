"""v0.21 (2026-05-28): сборка funding settlements для закрытых позиций.

Bybit `closedPnl` (используется в `pnl_reconcile.py`) НЕ включает
funding settlements — отдельный механизм perpetual futures, который
каждые 8ч (00:00 / 08:00 / 16:00 UTC) пересчитывает премию между
long-ами и short-ами.

В transaction-log (`/v5/account/transaction-log`, `type=SETTLEMENT`)
эти записи живут отдельно. Если позиция пересекла хотя бы один
settlement timestamp — у неё есть funding. Иначе ноль.

Этот модуль:
1. Знает как из (opened_at, closed_at) сформировать [start_ms, end_ms]
   окно для transaction-log (со slack ±2 мин чтобы поймать запись на
   границе settlement timestamp ≈ closed_at).
2. Суммирует все SETTLEMENT.funding_usd по symbol+side в этом окне.
3. Возвращает суммарный funding_usd (signed: − если бот заплатил,
   + если получил, 0 если settlement'ов не было).

Используется в:
- ``main._reconcile_funding`` (после ``_reconcile_pnl_to_net``) для
  догонной записи funding в БД.
- В будущем (v0.21+) — в ``executor._apply_close`` для немедленной
  записи если позиция держалась дольше 8ч.

Доверяем источнику с приоритетом transaction-log > computed estimate,
правило `stats-collection.mdc`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_trader.state.db import AiPosition
    from ai_trader.trading.client import AiBybitClient

log = logging.getLogger(__name__)


def _iso_to_ms(iso_str: str) -> int:
    """ISO-8601 → unix ms. 0 если parse не удался."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def fetch_position_funding(
    client: "AiBybitClient",
    position: "AiPosition",
    *,
    slack_ms: int = 120_000,
) -> float | None:
    """Возвращает суммарный funding_usd за время жизни ``position``.

    ``None`` если API упал (caller оставит ``funding_usd=NULL``, попробует
    позже). ``0.0`` если запрос ОК но settlement'ов не было.

    ``slack_ms`` (default 2 мин) — расширение окна с обеих сторон. На
    границе settlement timestamp transaction-log может записать
    funding на 1-2 секунды позже closed_at (особенно если позиция
    была закрыта ровно на 00:00/08:00/16:00 UTC). 2 мин буфера достаточно
    и не риск перехлёста с другой позицией того же symbol+side
    (минимальный интервал между нашими позициями обычно > 5 мин).
    """
    opened_ms = _iso_to_ms(position.opened_at) if position.opened_at else 0
    closed_ms = (
        _iso_to_ms(position.closed_at) if position.closed_at else 0
    )
    if opened_ms == 0 or closed_ms == 0:
        log.warning(
            "fetch_position_funding: bad timestamps for id=%d "
            "opened_at=%s closed_at=%s",
            position.id, position.opened_at, position.closed_at,
        )
        return None

    start = max(opened_ms - slack_ms, 0)
    end = closed_ms + slack_ms

    events = client.get_funding_for_position(
        position.symbol,
        start_ms=start,
        end_ms=end,
        side=position.side,
    )
    if events is None:
        log.info(
            "fetch_position_funding: API failure for %s id=%d "
            "(deferring, will retry next cycle)",
            position.symbol, position.id,
        )
        return None

    if not events:
        return 0.0

    # Дополнительный sanity: settlement timestamp должен быть строго
    # внутри [opened_ms, closed_ms] (без slack). Slack нужен только
    # для transaction-log запроса, сам settlement происходит ровно на
    # 00/08/16 UTC. Settlement раньше opened_ms = относится к предыдущей
    # позиции по тому же symbol+side; не наш.
    total = 0.0
    n_kept = 0
    for ev in events:
        if not (opened_ms <= ev.transaction_time_ms <= closed_ms):
            continue
        total += ev.funding_usd
        n_kept += 1

    log.info(
        "FUNDING-RECONCILE id=%d %s %s: %d settlement(s) in [%d..%d], "
        "total funding=$%+.4f",
        position.id, position.side, position.symbol, n_kept,
        opened_ms, closed_ms, total,
    )
    return total
