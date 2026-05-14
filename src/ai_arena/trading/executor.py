"""Парсер Nof1-style ответа LLM и исполнение действий.

Output schema (см. правило `ai-arena-sources.mdc`, gist nof1-prompt.md):

    {
      "signal": "buy_to_enter" | "sell_to_enter" | "hold" | "close",
      "coin":   "BTCUSDT" | "ETHUSDT" | …,
      "quantity": <float>,
      "leverage": <integer 1-5>,
      "stop_loss":     <float>,
      "profit_target": <float>,
      "invalidation_condition": "<string>",
      "confidence": <float 0-1>,
      "risk_usd":   <float ≤ 10>,
      "justification": "<string ≤ 500 chars>"
    }

Валидация:
1. signal ∈ allowed
2. coin ∈ whitelist
3. signal=hold: остальные поля игнорируются
4. signal=close: нужен match по open position (по coin)
5. signal=buy/sell:
   - quantity > 0 и кратна qty_step
   - leverage ∈ [1, max_leverage]
   - LONG:  stop_loss < current_price < profit_target
     SHORT: profit_target < current_price < stop_loss
   - R:R = |profit_target - current_price| / |current_price - stop_loss| ≥ 1.5
   - risk_usd = |current_price - stop_loss| * quantity ≤ max_risk_per_trade_usd
   - confidence ∈ [0, 1]
6. Killswitch: max_open_positions, max_leverage, daily/total loss

При нарушении любого пункта — `signal` интерпретируется как **HOLD**
с записью error в БД.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from ai_arena.config.settings import AiArenaSettings
from ai_arena.safety.killswitch import KillSwitch
from ai_arena.state.db import AiArenaStore
from ai_arena.trading.client import AiArenaBybitClient

log = logging.getLogger(__name__)

ALLOWED_SIGNALS = {"buy_to_enter", "sell_to_enter", "hold", "close"}


@dataclass
class ParsedAction:
    signal: str  # buy_to_enter / sell_to_enter / hold / close
    raw: dict[str, Any]


@dataclass
class ApplyResult:
    executed: bool
    summary: str
    error: str | None = None


def _decimals_for_step(step: float) -> int:
    if step <= 0 or step >= 1:
        return 0
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    return len(s.split(".", 1)[1]) if "." in s else 0


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = math.floor(value / step)
    return round(n * step, _decimals_for_step(step))


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = round(value / step)
    return round(n * step, _decimals_for_step(step))


# ─── Parser ──────────────────────────────────────────────────────────────


def parse_action(text: str, allowed_symbols: tuple[str, ...]) -> ParsedAction | str:
    """Возвращает ``ParsedAction`` или строку с описанием ошибки.

    Ищет последний balanced JSON-блок в тексте — это устойчиво к фигурным
    скобкам в commentary (LLM часто сначала пишет анализ текстом, потом
    JSON в конце).
    """
    if not text:
        return "empty response"

    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    obj = None
    last_err: Exception | None = None
    end = len(cleaned)
    while True:
        end_brace = cleaned.rfind("}", 0, end)
        if end_brace == -1:
            break
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
            if isinstance(parsed, dict) and "signal" in parsed:
                obj = parsed
                break
            last_err = ValueError(f"not a decision dict: {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            last_err = e
        end = start_brace

    if obj is None:
        if last_err is not None:
            return f"JSON parse error: {last_err}"
        return f"no JSON object with 'signal' found: {cleaned[:120]}"

    if not isinstance(obj, dict):
        return f"expected JSON object, got {type(obj).__name__}"

    signal = obj.get("signal")
    if signal not in ALLOWED_SIGNALS:
        return f"invalid signal: {signal!r} (allowed: {sorted(ALLOWED_SIGNALS)})"

    if signal == "hold":
        return ParsedAction(signal="hold", raw=obj)

    coin = obj.get("coin")
    if coin not in allowed_symbols:
        return f"coin {coin!r} not in allowed list {allowed_symbols}"

    if signal == "close":
        return ParsedAction(signal="close", raw=obj)

    # buy_to_enter / sell_to_enter — полная валидация
    qty = obj.get("quantity")
    leverage = obj.get("leverage")
    sl = obj.get("stop_loss")
    tp = obj.get("profit_target")
    confidence = obj.get("confidence")
    risk_usd = obj.get("risk_usd")

    for key, v in [("quantity", qty), ("leverage", leverage), ("stop_loss", sl),
                   ("profit_target", tp), ("confidence", confidence),
                   ("risk_usd", risk_usd)]:
        if not isinstance(v, (int, float)):
            return f"invalid {key}: {v!r} (expected number)"
        if v < 0:
            return f"{key} must be ≥ 0, got {v}"

    if not 0 <= confidence <= 1:
        return f"confidence out of [0,1]: {confidence}"

    if leverage < 1:
        return f"leverage must be ≥ 1, got {leverage}"

    if qty <= 0:
        return f"quantity must be > 0 for entries, got {qty}"

    return ParsedAction(signal=signal, raw=obj)


# ─── Apply ───────────────────────────────────────────────────────────────


def apply_action(
    action: ParsedAction,
    *,
    client: AiArenaBybitClient,
    store: AiArenaStore,
    settings: AiArenaSettings,
    killswitch: KillSwitch,
    notional_cap_base_usd: float | None = None,
) -> ApplyResult:
    """Применяет распарсенное LLM-решение.

    notional_cap_base_usd: база для notional-cap'а (max_notional = base × leverage).
    Если None — fallback на settings.virtual_capital_usd (для unit-тестов и
    обратной совместимости). В live-цикле передаётся scaled_equity =
    real_bybit_equity / equity_scale_divisor, чтобы лимит рос/падал вместе
    с реальным P&L (compounding).
    """
    if action.signal == "hold":
        just = action.raw.get("justification", "")
        return ApplyResult(executed=False, summary=f"HOLD: {just[:200]}")

    if action.signal == "close":
        return _apply_close(action, client=client, store=store)

    if action.signal in {"buy_to_enter", "sell_to_enter"}:
        return _apply_open(
            action,
            client=client,
            store=store,
            settings=settings,
            killswitch=killswitch,
            notional_cap_base_usd=notional_cap_base_usd,
        )

    return ApplyResult(
        executed=False, summary="", error=f"unknown signal: {action.signal}"
    )


def _apply_close(
    action: ParsedAction, *, client: AiArenaBybitClient, store: AiArenaStore
) -> ApplyResult:
    coin = action.raw["coin"]
    pos = next((p for p in store.get_open_positions() if p.symbol == coin), None)
    if pos is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"close: no open position for {coin}",
        )

    link_id = f"arena_close_{uuid.uuid4().hex[:10]}"
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
        close_reason=action.raw.get("justification", "llm_close")[:200],
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"CLOSE id={pos.id} {pos.side} {pos.symbol} "
            f"exit=${exit_price:.6g} pnl=${pnl:+.2f}"
        ),
    )


def _apply_open(
    action: ParsedAction,
    *,
    client: AiArenaBybitClient,
    store: AiArenaStore,
    settings: AiArenaSettings,
    killswitch: KillSwitch,
    notional_cap_base_usd: float | None = None,
) -> ApplyResult:
    raw = action.raw
    coin = raw["coin"]
    side = "Buy" if action.signal == "buy_to_enter" else "Sell"
    qty_req = float(raw["quantity"])
    leverage = int(raw["leverage"])
    sl_price = float(raw["stop_loss"])
    tp_price = float(raw["profit_target"])
    confidence = float(raw.get("confidence", 0))
    risk_usd_claimed = float(raw.get("risk_usd", 0))
    invalidation = str(raw.get("invalidation_condition", ""))[:500]
    justification = str(raw.get("justification", ""))[:500]

    # 1) Killswitch (positions count + leverage cap)
    check = killswitch.check_can_open_position(leverage)
    if not check.allowed:
        return ApplyResult(executed=False, summary="", error=f"killswitch: {check.reason}")

    # 2) Уже есть открытая по этой coin? Nof1: «one position per coin»
    if any(p.symbol == coin for p in store.get_open_positions()):
        return ApplyResult(
            executed=False, summary="",
            error=f"already have open position for {coin} (no pyramiding)",
        )

    # 3) Текущая цена + instrument-info
    ticker = client.get_ticker(coin)
    if ticker is None or ticker.last_price <= 0:
        return ApplyResult(executed=False, summary="", error=f"ticker unavailable for {coin}")
    price = ticker.last_price

    info = client.get_instrument_info(coin)
    if info is None:
        return ApplyResult(executed=False, summary="", error=f"instrument-info unavailable for {coin}")

    sl_price = _round_to_step(sl_price, info.tick_size)
    tp_price = _round_to_step(tp_price, info.tick_size)

    # 4) Direction sanity (LONG/SHORT)
    if side == "Buy":
        if not (sl_price < price < tp_price):
            return ApplyResult(
                executed=False, summary="",
                error=f"LONG: need SL<price<TP, got SL={sl_price} price={price} TP={tp_price}",
            )
    else:
        if not (tp_price < price < sl_price):
            return ApplyResult(
                executed=False, summary="",
                error=f"SHORT: need TP<price<SL, got TP={tp_price} price={price} SL={sl_price}",
            )

    # 5) R:R hard-check
    risk_dist = abs(price - sl_price)
    reward_dist = abs(tp_price - price)
    if risk_dist <= 0:
        return ApplyResult(executed=False, summary="", error="risk_dist == 0 (SL==price)")
    rr = reward_dist / risk_dist
    if rr < settings.min_risk_reward_ratio:
        return ApplyResult(
            executed=False, summary="",
            error=f"R:R {rr:.2f} < min {settings.min_risk_reward_ratio} — return HOLD",
        )

    # 6) Округляем qty под qty_step
    qty = _floor_to_step(qty_req, info.qty_step)
    if qty < info.min_order_qty:
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"qty {qty} < min_order_qty {info.min_order_qty} for {coin} "
                f"(requested {qty_req}, step {info.qty_step})"
            ),
        )
    if qty > info.max_order_qty:
        qty = _floor_to_step(info.max_order_qty, info.qty_step)
    if qty <= 0:
        return ApplyResult(executed=False, summary="", error="qty<=0 after rounding")

    # 7) risk_usd hard-check (КАНОНИЧНАЯ Nof1 формула: БЕЗ leverage)
    risk_usd_actual = risk_dist * qty
    if risk_usd_actual > settings.max_risk_per_trade_usd:
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"risk_usd {risk_usd_actual:.2f} > max {settings.max_risk_per_trade_usd:.2f} "
                f"(claimed {risk_usd_claimed:.2f})"
            ),
        )

    # 8) Notional cap: base × leverage. base = scaled_equity (real Bybit
    # equity / divisor) если передан notional_cap_base_usd, иначе fallback на
    # virtual_capital_usd. Compounding-логика: cap растёт вместе с равити.
    notional = qty * price
    cap_base = (
        notional_cap_base_usd
        if notional_cap_base_usd is not None and notional_cap_base_usd > 0
        else settings.virtual_capital_usd
    )
    max_notional = cap_base * leverage
    if notional > max_notional:
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"notional ${notional:.2f} > cap ${max_notional:.2f} "
                f"(virtual_cap×leverage)"
            ),
        )

    if not settings.trading_enabled:
        return ApplyResult(
            executed=False,
            summary=(
                f"[PAPER] {action.signal.upper()} {coin} qty={qty} @ ${price:.6g} "
                f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x conf={confidence:.2f} "
                f"R:R={rr:.2f} risk=${risk_usd_actual:.2f} — {justification[:150]}"
            ),
        )

    # 9) Live: set_leverage → place_order
    if not client.set_leverage(coin, leverage):
        log.warning(
            "set_leverage %s %dx failed before place_order — продолжаем",
            coin, leverage,
        )
    link_id = f"arena_{uuid.uuid4().hex[:12]}"
    resp = client.place_order(
        symbol=coin,
        side=side,
        qty=qty,
        order_link_id=link_id,
        sl_price=sl_price,
        tp_price=tp_price,
    )
    if not resp or not resp.get("ok"):
        err_msg = (resp or {}).get("error", "place_order returned empty")
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"open_failed: {err_msg} "
                f"(symbol={coin} side={side} qty={qty} lev={leverage}x)"
            ),
        )

    store.open_position(
        symbol=coin,
        side=side,
        qty=qty,
        entry_price=price,
        sl_price=sl_price,
        tp_price=tp_price,
        leverage=leverage,
        order_link_id=link_id,
        llm_justification=justification,
        confidence=confidence,
        invalidation_condition=invalidation,
        risk_usd=risk_usd_actual,
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"OPEN {action.signal.upper()} {coin} qty={qty} @ ${price:.6g} "
            f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
            f"conf={confidence:.2f} R:R={rr:.2f} risk=${risk_usd_actual:.2f}"
        ),
    )
