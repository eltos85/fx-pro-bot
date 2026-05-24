"""v2.z3 user-approved exception #4 (2026-05-22): server-side notional cap.

Pure-функция для вычисления rescale-операций над qty, изолированная от
Bybit-client / БД для unit-тестов. Интеграция в `executor._apply_open`
вызывает `apply_notional_cap(...)` единожды между rounding qty и
``place_order``.

См. правило ``.cursor/rules/ai-arena-sources.mdc`` § «Допустимые
исключения по решению пользователя» (исключение #4) и
BUILDLOG_AI_ARENA.md v2.z3 entry.

Поведение: silent rescale + лог факта (через event-объект). Если
после rescale qty < min_order_qty биржи — возвращается reject с тем же
event для уведомления LLM.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class CapResult:
    """Итог применения notional cap к одной открывающей сделке.

    Семантика:
    - ``original_qty`` / ``original_notional`` — что LLM запросил после
      Bybit-rounding'а (qty_step), но до cap.
    - ``capped_qty`` / ``capped_notional`` — после cap (или равны
      original'ам если cap не сработал).
    - ``rescaled``: True если qty был уменьшен. False если LLM
      изначально вписался в cap (или cap отключён `max_allocation_pct≥1.0`).
    - ``rejected``: True если после cap qty < min_order_qty биржи.
      В таком случае позиция **не открывается** (LLM получит rescale-notice
      в следующем prompt'е с пометкой «cap_too_small_to_open»).
    - ``max_notional`` — фактический cap в долларах
      (`max_allocation_pct × virtual_capital`).
    """

    original_qty: float
    original_notional: float
    capped_qty: float
    capped_notional: float
    max_notional: float
    rescaled: bool
    rejected: bool
    reject_reason: str | None = None


# Защита от деградации float (очень-очень маленькие отклонения).
_EPS: Final[float] = 1e-9


def _floor_to_step(value: float, step: float) -> float:
    """Округление вниз до кратного шагу (Bybit lotSizeFilter qty_step)."""
    if step <= 0:
        return value
    return math.floor((value + _EPS) / step) * step


def apply_notional_cap(
    *,
    requested_qty: float,
    price: float,
    qty_step: float,
    min_order_qty: float,
    virtual_capital_usd: float,
    max_allocation_pct: float,
) -> CapResult:
    """Применяет notional cap к запрошенному qty.

    Notional определяется как ``qty × price`` (в USDT). Leverage **не
    участвует** в формуле — leverage влияет на margin требование,
    но не на размер позиции (notional). Это намеренно: даже при
    `leverage=1x` позиция в $47k на $10k капитал → 4.7× exposure
    относительно equity.

    ``max_notional = virtual_capital_usd × max_allocation_pct``.
    Например: $10000 × 0.30 = $3000 максимум на позицию.

    Шаги:
    1. Вычислить ``original_notional = requested_qty × price``.
    2. Если ``original_notional ≤ max_notional`` (с малой EPS-погрешностью)
       → возвращаем как есть, ``rescaled=False``, ``rejected=False``.
    3. Иначе считаем ``capped_qty = floor((max_notional / price), step)``.
    4. Если ``capped_qty < min_order_qty`` (биржа не примет такой
       размер) → ``rejected=True`` + reason.
    5. Иначе → ``rescaled=True`` с детальной статистикой.

    Edge cases:
    - ``max_allocation_pct ≥ 1.0`` (или ``virtual_capital_usd × pct``
      ≥ original_notional) → cap не сработает, return as-is.
      Эффективно отключает cap.
    - ``price ≤ 0`` или ``virtual_capital_usd ≤ 0`` → возвращаем
      reject с причиной (edge-case защита; в проде не должно).
    - ``requested_qty ≤ 0`` → reject с причиной.
    """
    original_notional = requested_qty * price
    max_notional = virtual_capital_usd * max_allocation_pct

    if price <= 0:
        return CapResult(
            original_qty=requested_qty,
            original_notional=0.0,
            capped_qty=0.0,
            capped_notional=0.0,
            max_notional=max_notional,
            rescaled=False,
            rejected=True,
            reject_reason="invalid_price",
        )
    if virtual_capital_usd <= 0 or max_allocation_pct <= 0:
        return CapResult(
            original_qty=requested_qty,
            original_notional=original_notional,
            capped_qty=0.0,
            capped_notional=0.0,
            max_notional=0.0,
            rescaled=False,
            rejected=True,
            reject_reason="invalid_cap_config",
        )
    if requested_qty <= 0:
        return CapResult(
            original_qty=requested_qty,
            original_notional=original_notional,
            capped_qty=0.0,
            capped_notional=0.0,
            max_notional=max_notional,
            rescaled=False,
            rejected=True,
            reject_reason="non_positive_qty",
        )

    if original_notional <= max_notional + _EPS:
        return CapResult(
            original_qty=requested_qty,
            original_notional=original_notional,
            capped_qty=requested_qty,
            capped_notional=original_notional,
            max_notional=max_notional,
            rescaled=False,
            rejected=False,
        )

    capped_qty = _floor_to_step(max_notional / price, qty_step)
    capped_notional = capped_qty * price

    if capped_qty < min_order_qty:
        return CapResult(
            original_qty=requested_qty,
            original_notional=original_notional,
            capped_qty=capped_qty,
            capped_notional=capped_notional,
            max_notional=max_notional,
            rescaled=False,
            rejected=True,
            reject_reason="cap_too_small_to_open",
        )

    return CapResult(
        original_qty=requested_qty,
        original_notional=original_notional,
        capped_qty=capped_qty,
        capped_notional=capped_notional,
        max_notional=max_notional,
        rescaled=True,
        rejected=False,
    )


def format_rescale_notice(
    *,
    coin: str,
    side: str,
    cap: CapResult,
    leverage: int,
    virtual_capital_usd: float,
    max_allocation_pct: float,
) -> str:
    """Сериализует ``CapResult`` в текст блок для USER_PROMPT.

    Возвращает текст «System notice» который вставляется в начало
    следующего USER_PROMPT после ``CRITICAL: ALL OF THE PRICE…`` и
    перед ``## CURRENT MARKET STATE FOR ALL COINS``.

    Формат подстраивается под `cap.rejected` vs `cap.rescaled`:
    - rescaled (исполнено): «Your `quantity` was rescaled X→Y …»
    - rejected (не исполнено): «Your `quantity` was rejected (cap too
      small for min_order_qty) — no position opened.»
    """
    pct_label = f"{max_allocation_pct * 100:.0f}%"
    cap_dollars = f"${cap.max_notional:,.2f}".replace(",", ",")
    capital_dollars = f"${virtual_capital_usd:,.2f}".replace(",", ",")

    if cap.rejected:
        return (
            f"⚠️ **System notice (previous cycle):** Your `quantity` for "
            f"{coin} {side.upper()} (lev={leverage}x) was rejected — "
            f"requested notional ${cap.original_notional:,.2f} exceeded "
            f"server-side max allocation ({pct_label} × {capital_dollars} "
            f"= {cap_dollars}), and the rescaled qty fell below "
            f"Bybit min_order_qty. **No position was opened.** Reason: "
            f"`{cap.reject_reason}`. **Future tip:** keep `quantity × "
            f"current_price` ≤ {cap_dollars} per opening order."
        )
    return (
        f"⚠️ **System notice (previous cycle):** Your `quantity` for "
        f"{coin} {side.upper()} (lev={leverage}x) was rescaled "
        f"{cap.original_qty:g} → {cap.capped_qty:g} (notional "
        f"${cap.original_notional:,.2f} → ${cap.capped_notional:,.2f}) "
        f"to respect server-side max allocation of {pct_label} × "
        f"{capital_dollars} = {cap_dollars}. Position was opened with "
        f"the rescaled qty. **Future tip:** keep `quantity × "
        f"current_price` ≤ {cap_dollars} per opening order to avoid "
        f"silent rescale; this leaves room for up to "
        f"{int(round(1 / max_allocation_pct))} simultaneous positions."
    )
