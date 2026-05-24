"""Парсинг ответа LLM и исполнение действий.

Цикл:
1. parse_action(text) → строгая валидация JSON по схеме
2. apply_action(...) → вызов клиента Bybit + запись в БД
3. Все ошибки → возврат ApplyResult с error, никаких exception наружу
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from ai_trader.config.settings import AiTraderSettings
from ai_trader.safety.killswitch import KillSwitch
from ai_trader.state.db import AiTraderStore
from ai_trader.trading.client import AiBybitClient, InstrumentInfo


def _decimals_for_step(step: float) -> int:
    """Сколько знаков после запятой нужно для строкового представления step."""
    if step <= 0 or step >= 1:
        return 0
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def _floor_to_step(value: float, step: float) -> float:
    """Округление вниз до ближайшего step (для qty: чтобы не превысить notional)."""
    if step <= 0:
        return value
    n = math.floor(value / step)
    return round(n * step, _decimals_for_step(step))


def _round_to_step(value: float, step: float) -> float:
    """Округление к ближайшему step (для цен SL/TP)."""
    if step <= 0:
        return value
    n = round(value / step)
    return round(n * step, _decimals_for_step(step))

log = logging.getLogger(__name__)


ALLOWED_SIDES = {"Buy", "Sell"}


@dataclass
class ParsedAction:
    action: str  # "open" / "close" / "hold"
    raw: dict[str, Any]


@dataclass
class ApplyResult:
    executed: bool
    summary: str
    error: str | None = None


def parse_action(
    text: str,
    allowed_symbols: tuple[str, ...],
    *,
    review_mode: bool = False,
    risk_usd_cap: float = 10.0,
) -> ParsedAction | str:
    """Возвращает ParsedAction или строку с описанием ошибки.

    v0.3 (AUDIT_2026.md): промпт теперь требует commentary + JSON, поэтому
    парсер ищет **последний** balanced JSON-блок в тексте, а не первый
    встреченный ``{``. Это устойчиво к фигурным скобкам в commentary.

    v0.10 (2026-05-10): review-cycle support. Если ``review_mode=True`` —
    action ``"open"`` отвергается явной ошибкой (review-промпт явно
    запрещает open, но дополнительный hard-guard защищает от случаев
    когда LLM проигнорировал инструкцию).

    v0.15 (2026-05-24, refactor): ``risk_usd_cap`` — per-trade cap в USD
    (= ``settings.virtual_capital_usd × settings.risk_per_trade_pct``).
    Default ``10.0`` соответствует default-settings ($500 capital × 2%) —
    backward-compat для существующих тестов. В production (main.py)
    передаётся явно из settings, чтобы переменная ``AI_TRADER_RISK_PER_TRADE``
    в ``.env`` была единой точкой истины (промпт + парсер).
    """
    if not text:
        return "empty response"

    cleaned = text.strip()
    # Терпим случай если LLM всё-таки обернул в ```json … ```
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    # Ищем последний JSON-объект в тексте: идём от конца, находим '}',
    # затем balanced '{' слева. Если не парсится — пробуем следующий '}'.
    obj = None
    last_err: Exception | None = None
    end = len(cleaned)
    while True:
        end_brace = cleaned.rfind("}", 0, end)
        if end_brace == -1:
            break
        # Найдём balanced '{' слева через скобочный счётчик
        depth = 0
        start_brace = -1
        for i in range(end_brace, -1, -1):
            ch = cleaned[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start_brace = i
                    break
        if start_brace == -1:
            break
        candidate = cleaned[start_brace : end_brace + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "action" in parsed:
                obj = parsed
                break
            last_err = ValueError(f"not a decision dict: {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            last_err = e
        # Не подошло — попробуем JSON-блок раньше в тексте
        end = start_brace

    if obj is None:
        if last_err is not None:
            return f"JSON parse error: {last_err}"
        return f"no JSON object found in response: {cleaned[:120]}"

    if not isinstance(obj, dict):
        return f"expected JSON object, got {type(obj).__name__}"

    action = obj.get("action")
    if action not in {"open", "close", "hold"}:
        return f"invalid action: {action!r}"

    if review_mode and action == "open":
        return "review_mode: 'open' action is forbidden in review cycle"

    if action == "open":
        sym = obj.get("symbol")
        if sym not in allowed_symbols:
            return f"symbol {sym!r} not in allowed list"
        if obj.get("side") not in ALLOWED_SIDES:
            return f"invalid side: {obj.get('side')!r}"
        for key in ("leverage", "position_size_usd", "stop_loss", "take_profit"):
            v = obj.get(key)
            if not isinstance(v, (int, float)) or v <= 0:
                return f"invalid {key}: {v!r}"

        # v0.13 (2026-05-18) — meta-cognition поля Nof1-style.
        # Эти три поля обязательны для action=open и принуждают LLM
        # явно посчитать (а не «прикинуть») уверенность, риск и заранее
        # сформулировать observable условие, при котором тезис неверен.
        # См. AI_TRADER_PROPOSAL_ALPHA_ARENA.md § Output Schema.
        conf = obj.get("confidence")
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            return f"confidence required (number 0.0-1.0), got {conf!r}"
        if not (0.0 <= float(conf) <= 1.0):
            return f"confidence out of range [0.0, 1.0]: {conf!r}"

        inv = obj.get("invalidation_condition")
        if not isinstance(inv, str):
            return f"invalidation_condition required (string), got {type(inv).__name__}"
        inv_stripped = inv.strip()
        if not inv_stripped:
            return "invalidation_condition required (non-empty string)"
        if len(inv_stripped) > 500:
            return f"invalidation_condition too long (max 500 chars): got {len(inv_stripped)}"

        risk_decl = obj.get("risk_usd")
        if not isinstance(risk_decl, (int, float)) or isinstance(risk_decl, bool):
            return f"risk_usd required (number), got {risk_decl!r}"
        if float(risk_decl) <= 0 or float(risk_decl) > risk_usd_cap:
            return (
                f"risk_usd out of range (must be 0 < x <= {risk_usd_cap:g}): {risk_decl!r}. "
                f"Per-trade cap = ${risk_usd_cap:g}."
            )

    if action == "close":
        if not isinstance(obj.get("position_id"), int):
            return f"close requires int position_id, got {obj.get('position_id')!r}"

    return ParsedAction(action=action, raw=obj)


def apply_action(
    action: ParsedAction,
    *,
    client: AiBybitClient,
    store: AiTraderStore,
    settings: AiTraderSettings,
    killswitch: KillSwitch,
) -> ApplyResult:
    if action.action == "hold":
        reason = action.raw.get("reason", "")
        return ApplyResult(executed=False, summary=f"HOLD: {reason}")

    if action.action == "close":
        return _apply_close(action, client=client, store=store)

    if action.action == "open":
        return _apply_open(
            action, client=client, store=store, settings=settings, killswitch=killswitch
        )

    return ApplyResult(executed=False, summary="unknown action", error="impossible branch")


def _apply_close(
    action: ParsedAction, *, client: AiBybitClient, store: AiTraderStore
) -> ApplyResult:
    pos_id = int(action.raw["position_id"])
    pos = None
    for p in store.get_open_positions():
        if p.id == pos_id:
            pos = p
            break
    if pos is None:
        return ApplyResult(
            executed=False, summary="", error=f"position id={pos_id} not found among open positions"
        )

    link_id = f"ai_close_{uuid.uuid4().hex[:10]}"
    resp = client.close_position(pos.symbol, pos.side, pos.qty, link_id)
    if not resp or not resp.get("ok"):
        err_msg = (resp or {}).get("error", "close_position returned empty")
        return ApplyResult(executed=False, summary="", error=f"close_failed: {err_msg}")

    ticker = client.get_ticker(pos.symbol)
    exit_price = ticker.last_price if ticker else pos.entry_price
    if pos.side == "Buy":
        pnl = (exit_price - pos.entry_price) * pos.qty
    else:
        pnl = (pos.entry_price - exit_price) * pos.qty
    store.close_position(
        pos.id,
        exit_price=exit_price,
        realized_pnl_usd=pnl,
        close_reason=action.raw.get("reason", "llm_close"),
    )
    return ApplyResult(
        executed=True,
        summary=f"CLOSE id={pos.id} {pos.side} {pos.symbol} exit=${exit_price:.6g} pnl=${pnl:+.2f}",
    )


def _apply_open(
    action: ParsedAction,
    *,
    client: AiBybitClient,
    store: AiTraderStore,
    settings: AiTraderSettings,
    killswitch: KillSwitch,
) -> ApplyResult:
    raw = action.raw
    symbol = raw["symbol"]
    side = raw["side"]
    leverage = int(raw["leverage"])
    notional_usd = float(raw["position_size_usd"])
    sl_price = float(raw["stop_loss"])
    tp_price = float(raw["take_profit"])
    reason = str(raw.get("reason", ""))[:200]
    # v0.13: meta-cognition поля. Парсер их уже отвалидировал; здесь
    # просто извлекаем для проброса в БД и summary.
    confidence = float(raw["confidence"])
    invalidation_condition = str(raw["invalidation_condition"]).strip()[:500]
    risk_usd_declared = float(raw["risk_usd"])

    check = killswitch.check_can_open_position(leverage)
    if not check.allowed:
        return ApplyResult(executed=False, summary="", error=f"killswitch: {check.reason}")

    ticker = client.get_ticker(symbol)
    if ticker is None or ticker.last_price <= 0:
        return ApplyResult(executed=False, summary="", error=f"ticker unavailable for {symbol}")
    price = ticker.last_price

    # instruments-info — для round'инга qty/SL/TP под Bybit фильтры.
    info = client.get_instrument_info(symbol)
    if info is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"instruments-info unavailable for {symbol}",
        )

    # Округляем SL/TP под tick_size ДО sanity-check'а — чтобы не падать
    # из-за плавающей точки LLM (1.38531 при tickSize 0.0001).
    sl_price = _round_to_step(sl_price, info.tick_size)
    tp_price = _round_to_step(tp_price, info.tick_size)

    # Sanity check на направление SL/TP
    if side == "Buy":
        if not (sl_price < price < tp_price):
            return ApplyResult(
                executed=False,
                summary="",
                error=f"Buy: need SL<price<TP, got SL={sl_price} price={price} TP={tp_price}",
            )
    else:
        if not (sl_price > price > tp_price):
            return ApplyResult(
                executed=False,
                summary="",
                error=f"Sell: need SL>price>TP, got SL={sl_price} price={price} TP={tp_price}",
            )

    # Cap notional к виртуальному капиталу × leverage
    max_notional = settings.virtual_capital_usd * leverage
    if notional_usd > max_notional:
        notional_usd = max_notional
    # Округляем qty ВНИЗ под qtyStep — чтобы notional не превысил таргет
    # и Bybit принял ордер. Без этого: XRPUSDT (qtyStep=1) получает
    # qty=341.0343 → ErrCode 10001 «Qty invalid».
    qty_raw = notional_usd / price
    qty = _floor_to_step(qty_raw, info.qty_step)
    if qty < info.min_order_qty:
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"qty {qty} < min_order_qty {info.min_order_qty} for {symbol} "
                f"(notional ${notional_usd:.2f} / price {price} / step {info.qty_step})"
            ),
        )
    if qty > info.max_order_qty:
        qty = _floor_to_step(info.max_order_qty, info.qty_step)
    if qty <= 0:
        return ApplyResult(executed=False, summary="", error="qty<=0 after rounding")

    if not settings.trading_enabled:
        # PAPER MODE: не вызываем биржу, только пишем decision-only
        return ApplyResult(
            executed=False,
            summary=(
                f"[PAPER] OPEN {side} {symbol} qty={qty} @ ${price:.6g} "
                f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
                f"conf={confidence:.2f} risk_decl=${risk_usd_declared:.2f} "
                f"inv=\"{invalidation_condition[:80]}\" — {reason}"
            ),
        )

    if not client.set_leverage(symbol, leverage):
        log.warning(
            "set_leverage %s %dx failed before place_order — продолжаем, могут быть отказы биржи",
            symbol,
            leverage,
        )
    link_id = f"ai_{uuid.uuid4().hex[:12]}"
    resp = client.place_order(
        symbol=symbol,
        side=side,
        qty=qty,
        order_link_id=link_id,
        sl_price=sl_price,
        tp_price=tp_price,
    )
    if not resp or not resp.get("ok"):
        err_msg = (resp or {}).get("error", "place_order returned empty")
        return ApplyResult(
            executed=False,
            summary="",
            error=(
                f"open_failed: {err_msg} "
                f"(symbol={symbol} side={side} qty={qty} lev={leverage}x)"
            ),
        )

    store.open_position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=price,
        sl_price=sl_price,
        tp_price=tp_price,
        leverage=leverage,
        order_link_id=link_id,
        llm_reason=reason,
        confidence=confidence,
        invalidation_condition=invalidation_condition,
        risk_usd_declared=risk_usd_declared,
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"OPEN {side} {symbol} qty={qty} @ ${price:.6g} "
            f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
            f"conf={confidence:.2f} risk_decl=${risk_usd_declared:.2f} "
            f"inv=\"{invalidation_condition[:80]}\" — {reason}"
        ),
    )
