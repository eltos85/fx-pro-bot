"""Pure-функция offset-based scaling для ai-arena.

Вынесено из ``main.py`` в отдельный модуль чтобы:

1. Тестировать без зависимости от ``anthropic`` (main.py подтягивает
   DeepSeekArenaClient → anthropic, что не нужно для математики).
2. Зафиксировать каноничную формулу (правило ai-arena-sources.mdc
   § «Equity scaling — offset-based») в одном месте — single source
   of truth.

Подробности в BUILDLOG_AI_ARENA.md (v2.x bug-fix: scaled_cash clamp removal).
"""
from __future__ import annotations


def compute_scaled_account(
    *,
    real_equity_now: float,
    real_at_start: float,
    real_available_cash: float,
    virtual_capital_usd: float,
) -> tuple[float, float, float]:
    """Offset-based scaling Bybit demo→sandbox (Nof1 Hyperliquid analog).

    Формула 1-в-1 с правилом ``ai-arena-sources.mdc`` § «Equity scaling —
    offset-based» (L130-133)::

        scaled_equity = virtual_capital_usd + (real_equity_now − real_at_start)
        scaled_cash   = real_available_cash − (real_at_start − virtual_capital_usd)

    Возвращает ``(scaled_equity, scaled_cash, total_return_pct)``.

    **NO CLAMP**: ``scaled_cash`` может быть отрицательным когда margin
    использован > virtual_capital. Это **намеренная семантика** —
    отрицательный cash = «нет свободного margin под новые позиции», что
    LLM должен интерпретировать как ограничение sizing. Раньше у нас был
    clamp ``< 0 → 0`` (наша отсебятина, не из правила) — он ломал
    канон-формулу ``Position Size = Cash × Leverage × Allocation%``
    в overleveraged state. См. BUILDLOG_AI_ARENA.md (v2.x bug-fix).

    ``total_return_pct = cumulative_real_pnl / virtual_capital_usd × 100``,
    тоже в полном соответствии с правилом (cumulative с момента старта).
    Если ``virtual_capital_usd == 0`` — ``total_return_pct = 0`` (защита
    от div/0; в проде virtual_capital всегда > 0).
    """
    cumulative_real_pnl = real_equity_now - real_at_start
    scaled_equity = virtual_capital_usd + cumulative_real_pnl
    scaled_cash = real_available_cash - (real_at_start - virtual_capital_usd)
    total_return_pct = (
        (cumulative_real_pnl / virtual_capital_usd) * 100.0
        if virtual_capital_usd > 0
        else 0.0
    )
    return scaled_equity, scaled_cash, total_return_pct
