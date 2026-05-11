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
) -> ParsedAction | str:
    """Возвращает ParsedAction или строку с описанием ошибки.

    v0.3 (AUDIT_2026.md): промпт теперь требует commentary + JSON, поэтому
    парсер ищет **последний** balanced JSON-блок в тексте, а не первый
    встреченный ``{``. Это устойчиво к фигурным скобкам в commentary.

    v0.10 (2026-05-10): review-cycle support. Если ``review_mode=True`` —
    action ``"open"`` отвергается явной ошибкой (review-промпт явно
    запрещает open, но дополнительный hard-guard защищает от случаев
    когда LLM проигнорировал инструкцию).
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
        # v0.11.1 (2026-05-11): compliance — обязательный sub-object для open.
        # Структура: sl_atr_ratio (float), rr_net_fee (float),
        # counter_trend (bool), confirmations (list of >=2 strings).
        comp = obj.get("compliance")
        if not isinstance(comp, dict):
            return "missing or non-object 'compliance' (required for open)"
        sl_ratio = comp.get("sl_atr_ratio")
        if not isinstance(sl_ratio, (int, float)) or sl_ratio <= 0:
            return f"invalid compliance.sl_atr_ratio: {sl_ratio!r}"
        rr_net = comp.get("rr_net_fee")
        if not isinstance(rr_net, (int, float)) or rr_net <= 0:
            return f"invalid compliance.rr_net_fee: {rr_net!r}"
        if not isinstance(comp.get("counter_trend"), bool):
            return f"invalid compliance.counter_trend: {comp.get('counter_trend')!r}"
        confirms = comp.get("confirmations")
        if not isinstance(confirms, list) or len(confirms) < 2:
            return (
                f"compliance.confirmations must be a list of >=2 strings, "
                f"got {confirms!r}"
            )
        if not all(isinstance(c, str) and c.strip() for c in confirms):
            return "compliance.confirmations must be non-empty strings"

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
    atr_by_symbol: dict[str, float] | None = None,
    regime_by_symbol: dict[str, dict[str, float | None]] | None = None,
    reference_price_by_symbol: dict[str, float] | None = None,
) -> ApplyResult:
    """Применить распарсенное действие LLM.

    `atr_by_symbol` — опциональная мапа {symbol: ATR(1H)} для compliance-check
    SL distance >= 1.5x ATR (см. STOP-LOSS DISCIPLINE в промпте, v0.11).
    Если передана и LLM нарушил правило — это **только** логируется как
    WARNING + помечается в summary (`sl_atr=X.XX`). Сделка НЕ блокируется
    (soft enforcement). Если соберём ≥10 нарушений — переходим к hard-block
    (см. BUILDLOG_AI_TRADER.md).

    `regime_by_symbol` (v0.12) — мапа {symbol: {adx14, plus_di14, minus_di14}}
    с 1H ADX/DI для regime-filter. Если ADX>=25 и направление сделки
    противоположно направлению тренда (counter-trend) — сделка
    БЛОКИРУЕТСЯ (hard enforcement). Research: Connors/Raschke 1995,
    botversusbot 2026 — mean-reversion в strong trend = suicide.

    `reference_price_by_symbol` (v0.13) — мапа {symbol: last_price} в
    момент сбора context (то есть «цена, которую видел LLM при принятии
    решения»). _apply_open сравнивает её с live ticker'ом в момент
    place_order; при drift > settings.price_drift_threshold_pct сделка
    отменяется (price_drift_too_large). Это устраняет ситуацию когда
    LLM выбрал SL/TP по «старой» цене, а к моменту fill цена уже ушла.
    """
    if action.action == "hold":
        reason = action.raw.get("reason", "")
        return ApplyResult(executed=False, summary=f"HOLD: {reason}")

    if action.action == "close":
        return _apply_close(action, client=client, store=store)

    if action.action == "open":
        return _apply_open(
            action,
            client=client,
            store=store,
            settings=settings,
            killswitch=killswitch,
            atr_by_symbol=atr_by_symbol,
            regime_by_symbol=regime_by_symbol,
            reference_price_by_symbol=reference_price_by_symbol,
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
    atr_by_symbol: dict[str, float] | None = None,
    regime_by_symbol: dict[str, dict[str, float | None]] | None = None,
    reference_price_by_symbol: dict[str, float] | None = None,
) -> ApplyResult:
    raw = action.raw
    symbol = raw["symbol"]
    side = raw["side"]
    leverage = int(raw["leverage"])
    notional_usd = float(raw["position_size_usd"])
    sl_price = float(raw["stop_loss"])
    tp_price = float(raw["take_profit"])
    reason = str(raw.get("reason", ""))[:200]

    check = killswitch.check_can_open_position(leverage)
    if not check.allowed:
        return ApplyResult(executed=False, summary="", error=f"killswitch: {check.reason}")

    # v0.12: SL cooldown gate. Если по паре (symbol, side) недавно был SL —
    # пара в cooldown'е, открывать новый трейд запрещено. Длительность
    # cooldown растёт по Fibonacci при повторяющихся SL подряд (см. db.py).
    cooldown_left = store.get_cooldown_remaining_minutes(symbol, side)
    if cooldown_left > 0:
        log.info(
            "COOLDOWN_BLOCK %s %s: %d min remaining (recent SL)",
            symbol, side, cooldown_left,
        )
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"cooldown_active: {symbol} {side} blocked for {cooldown_left} more "
                f"min after recent stop-loss (Fibonacci scheme, v0.12)"
            ),
        )

    # v0.12: ADX-based regime gate. Counter-trend mean-reversion в strong
    # trend (ADX>=25) — статистически проигрышная стратегия (Connors/Raschke
    # 1995, botversusbot 2026). Блокируем такие входы.
    # Threshold: settings.adx_regime_threshold (default 25, Wilder 1978).
    if regime_by_symbol is not None:
        reg = regime_by_symbol.get(symbol)
        if reg is not None:
            adx_v = reg.get("adx14")
            pdi = reg.get("plus_di14")
            mdi = reg.get("minus_di14")
            if (
                isinstance(adx_v, (int, float)) and adx_v >= settings.adx_regime_threshold
                and isinstance(pdi, (int, float)) and isinstance(mdi, (int, float))
            ):
                trending_up = pdi > mdi
                # counter-trend сделка: Sell в uptrend ИЛИ Buy в downtrend.
                counter_trend = (side == "Sell" and trending_up) or (
                    side == "Buy" and not trending_up
                )
                if counter_trend:
                    direction = "uptrend" if trending_up else "downtrend"
                    log.info(
                        "REGIME_BLOCK %s %s: ADX=%.1f +DI=%.1f -DI=%.1f (%s) — "
                        "counter-trend mean-reversion forbidden in trending regime",
                        symbol, side, adx_v, pdi, mdi, direction,
                    )
                    return ApplyResult(
                        executed=False, summary="",
                        error=(
                            f"regime_block: {symbol} {side} forbidden — "
                            f"ADX={adx_v:.1f} {direction} "
                            f"(+DI={pdi:.1f} -DI={mdi:.1f}); "
                            f"counter-trend mean-reversion in strong trend "
                            f"is statistically unprofitable (v0.12)"
                        ),
                    )

    ticker = client.get_ticker(symbol)
    if ticker is None or ticker.last_price <= 0:
        return ApplyResult(executed=False, summary="", error=f"ticker unavailable for {symbol}")
    price = ticker.last_price

    # v0.13: price-drift guard. Между сбором context (где LLM «увидел»
    # цену = ticker.last_price на тот момент) и текущим place_order
    # прошло 30-60 сек (LLM thinking + I/O). Если цена ушла >threshold,
    # SL/TP, рассчитанные по «той» цене, попадут не туда — отменяем.
    if reference_price_by_symbol is not None:
        ref_price = reference_price_by_symbol.get(symbol)
        if ref_price is not None and ref_price > 0:
            drift_pct = abs(price - ref_price) / ref_price * 100
            if drift_pct > settings.price_drift_threshold_pct:
                log.info(
                    "PRICE_DRIFT_BLOCK %s %s: reference=%.6g current=%.6g "
                    "drift=%.3f%% > threshold=%.2f%% — order cancelled",
                    symbol, side, ref_price, price, drift_pct,
                    settings.price_drift_threshold_pct,
                )
                return ApplyResult(
                    executed=False, summary="",
                    error=(
                        f"price_drift_too_large: {symbol} reference=${ref_price:.6g} "
                        f"current=${price:.6g} drift={drift_pct:.3f}% > "
                        f"{settings.price_drift_threshold_pct:.2f}% — SL/TP from "
                        f"LLM are stale; waiting for next cycle (v0.13)"
                    ),
                )

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
                f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x — {reason}"
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

    sl_dist = abs(price - sl_price)
    sl_atr_ratio: float | None = None
    sl_compliance_tag = ""
    if atr_by_symbol is not None:
        atr = atr_by_symbol.get(symbol)
        if atr is not None and atr > 0:
            sl_atr_ratio = sl_dist / atr
            if sl_atr_ratio < 1.5:
                log.warning(
                    "SL_DISCIPLINE_VIOLATION %s %s entry=%.6g SL=%.6g "
                    "sl_dist=%.6g ATR(1H)=%.6g ratio=%.2f (required >=1.50) — "
                    "trade allowed (soft enforcement); see prompt v0.11 STOP-LOSS DISCIPLINE",
                    symbol,
                    side,
                    price,
                    sl_price,
                    sl_dist,
                    atr,
                    sl_atr_ratio,
                )
                sl_compliance_tag = f" [sl_atr={sl_atr_ratio:.2f}!]"
            else:
                sl_compliance_tag = f" [sl_atr={sl_atr_ratio:.2f}]"

            # v0.11.1: cross-check заявленного LLM compliance.sl_atr_ratio vs
            # фактического (наш расчёт). Расхождение > 10% = модель неточно
            # отчиталась — логируем для аудита (не блокирует сделку).
            comp = raw.get("compliance")
            if isinstance(comp, dict):
                claimed = comp.get("sl_atr_ratio")
                if isinstance(claimed, (int, float)) and claimed > 0:
                    drift = abs(claimed - sl_atr_ratio) / sl_atr_ratio
                    if drift > 0.10:
                        log.warning(
                            "MODEL_MISREPORT %s sl_atr_ratio claimed=%.2f "
                            "actual=%.2f drift=%.0f%% — LLM reported a "
                            "compliance value that does not match the math",
                            symbol,
                            claimed,
                            sl_atr_ratio,
                            drift * 100,
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
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"OPEN {side} {symbol} qty={qty} @ ${price:.6g} "
            f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x"
            f"{sl_compliance_tag} — {reason}"
        ),
    )
