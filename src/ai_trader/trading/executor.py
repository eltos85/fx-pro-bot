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
    # v0.30 audit-trail для последующей записи в `decisions` через
    # ``store.update_decision_thesis`` / ``store.update_decision_sentiment``
    # после того, как main.py получит decision_id из ``log_decision``.
    # Все поля опциональные — None означает «не применимо для этого
    # action» (например, thesis_status — только при close).
    thesis_status: str | None = None
    thesis_invalidator: str | None = None
    aggregate_uncertainty: float | None = None
    sentiment_items_json: str | None = None
    # v0.31 (2026-05-28, aggressive mandate cost-awareness): optional
    # `cost_estimate_usd` поле в open-JSON, заполняется LLM как сумма
    # fee_RT (round-trip taker fee на ожидаемом notional) + funding cost
    # to next 8h settlement. Soft enforcement: executor НЕ блокирует если
    # поле отсутствует или вне диапазона, только логирует и пишет в БД.
    # Цель: data trail для оценки accuracy LLM в pre-trade cost-thinking.
    cost_estimate_usd: float | None = None


# v0.30 thesis_status enum для close-action audit.
_ALLOWED_THESIS_STATUS = {"broken", "intact", "partial"}


def parse_action(
    text: str,
    allowed_symbols: tuple[str, ...],
    *,
    review_mode: bool = False,
    risk_usd_cap: float = 10.0,
    strict_v030_schema: bool = False,
    position_size_cap_usd: float = 500.0,
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

    v0.30 (2026-05-28): институциональная схема (port FX-trader patterns).
    Управляется флагом ``strict_v030_schema`` (default False для
    backward-compat с существующими тестами):

    - ``action=open`` требует ДОПОЛНИТЕЛЬНО:
      * ``macro_thesis`` (string, ≥50 ≤500 chars) — PRICE-ACTION
        trade-thesis (MFP-сетап + конкретный price level + macro
        regime). v0.40: ``sentiment`` блок и uncertainty-gate УДАЛЕНЫ
        (нет news-фида).
    - ``action=close`` требует ДОПОЛНИТЕЛЬНО:
      * ``thesis_status`` ∈ {"broken", "intact", "partial"} —
        классификация что произошло с тезисом (THESIS DISCIPLINE).
      * ``thesis_invalidator`` (string non-empty) — что именно сломало
        или подтвердило тезис.

    Backward-compat: с ``strict_v030_schema=False`` (default) все эти
    поля **опциональны** — старые тесты v0.2-v0.21 продолжают работать.
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

        # v0.31 (2026-05-28, aggressive mandate): position_size_usd cap.
        # Default 500.0 = legacy behavior (вся virtual capital). main.py
        # передаёт явный `settings.max_position_size_usd` (default $100
        # для aggressive mandate) — это binding constraint при leverage до
        # 5x, чтобы notional не превышал весь капитал. См. settings.py.
        pos_size = float(obj["position_size_usd"])
        if pos_size > position_size_cap_usd:
            return (
                f"position_size_usd {pos_size:g} > cap {position_size_cap_usd:g}. "
                f"v0.31 lot cap = ${position_size_cap_usd:g} (notional with "
                f"leverage up to 5x = ${position_size_cap_usd * 5:g} = full capital)."
            )

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

        # v0.40 strict schema: macro_thesis (reframed as PRICE-ACTION
        # trade-thesis) — sentiment block + uncertainty gate REMOVED
        # (no news feed). macro_thesis stays mandatory (≥50 ≤500 chars).
        if strict_v030_schema:
            mth = obj.get("macro_thesis")
            if not isinstance(mth, str):
                return (
                    f"macro_thesis required (string), got {type(mth).__name__}. "
                    "State the price-action setup + a specific level + regime."
                )
            mth_stripped = mth.strip()
            if len(mth_stripped) < 50:
                return (
                    f"macro_thesis too short (min 50 chars, got "
                    f"{len(mth_stripped)}). State the MFP setup + a specific "
                    "price level + macro regime context (e.g. \"1H broke 24h "
                    "high $79.8k on ATR% expansion, 4H trend up, risk-on "
                    "regime; SL below the broken high\")."
                )
            if len(mth_stripped) > 500:
                return f"macro_thesis too long (max 500 chars): got {len(mth_stripped)}"

    if action == "close":
        if not isinstance(obj.get("position_id"), int):
            return f"close requires int position_id, got {obj.get('position_id')!r}"

        if strict_v030_schema:
            ts = obj.get("thesis_status")
            if ts not in _ALLOWED_THESIS_STATUS:
                return (
                    f"thesis_status required (one of {sorted(_ALLOWED_THESIS_STATUS)}), "
                    f"got {ts!r}. broken=thesis invalidated, intact=thesis "
                    "still valid (closing for other reason), partial=thesis "
                    "partially playing out."
                )
            inv = obj.get("thesis_invalidator")
            if not isinstance(inv, str) or not inv.strip():
                return (
                    f"thesis_invalidator required (non-empty string), got "
                    f"{inv!r}. Specify what broke / confirmed the thesis "
                    "(price level / trend flip / regime shift / indicator)."
                )
            if len(inv) > 500:
                return f"thesis_invalidator too long (max 500 chars): got {len(inv)}"

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
    # v0.30 audit-trail (опциональные, пробрасываем дальше в ApplyResult).
    thesis_status = action.raw.get("thesis_status")
    if not isinstance(thesis_status, str):
        thesis_status = None
    thesis_invalidator = action.raw.get("thesis_invalidator")
    if not isinstance(thesis_invalidator, str):
        thesis_invalidator = None

    pos = None
    for p in store.get_open_positions():
        if p.id == pos_id:
            pos = p
            break
    if pos is None:
        return ApplyResult(
            executed=False, summary="", error=f"position id={pos_id} not found among open positions",
            thesis_status=thesis_status,
            thesis_invalidator=thesis_invalidator,
        )

    link_id = f"ai_close_{uuid.uuid4().hex[:10]}"
    resp = client.close_position(pos.symbol, pos.side, pos.qty, link_id)
    if not resp or not resp.get("ok"):
        err_msg = (resp or {}).get("error", "close_position returned empty")
        return ApplyResult(
            executed=False, summary="", error=f"close_failed: {err_msg}",
            thesis_status=thesis_status,
            thesis_invalidator=thesis_invalidator,
        )

    # Сначала считаем gross — на случай если get_closed_pnl недоступен
    # или Bybit ещё не успел записать closed-pnl (это случается через
    # 1-2 секунды после close). Если получили net — перезапишем ниже.
    ticker = client.get_ticker(pos.symbol)
    exit_price = ticker.last_price if ticker else pos.entry_price
    if pos.side == "Buy":
        pnl = (exit_price - pos.entry_price) * pos.qty
    else:
        pnl = (pos.entry_price - exit_price) * pos.qty
    pnl_source = "gross"

    # v0.18: попытка немедленного matching с Bybit closedPnl (net,
    # с учётом fee, но БЕЗ funding — см. v0.21). Если API дал ответ —
    # используем net. Иначе оставляем gross + догоним в
    # _reconcile_pnl_to_net на следующем full-cycle (см. main.py).
    from ai_trader.trading.pnl_reconcile import fetch_net_pnl
    net = fetch_net_pnl(client, pos)
    if net is not None:
        pnl, exit_price = net
        pnl_source = "net"

    store.close_position(
        pos.id,
        exit_price=exit_price,
        realized_pnl_usd=pnl,
        close_reason=action.raw.get("reason", "llm_close"),
        pnl_source=pnl_source,
    )

    # v0.21: попытка немедленной записи funding_usd. Если позиция
    # пересекала funding settlement (00/08/16 UTC) — он там был.
    # Bybit transaction-log обычно отдаёт SETTLEMENT в течение 1–2
    # минут после fact, в момент close скорее всего уже доступен для
    # «старых» settlement'ов (тех, что были давно за время удержания).
    # Если API упал / запись ещё не появилась — funding_usd=NULL,
    # _reconcile_funding догонит на следующем full-cycle.
    funding_suffix = ""
    try:
        from ai_trader.trading.funding_reconcile import fetch_position_funding

        closed_pos = store.get_position_by_link_id(pos.order_link_id)
        if closed_pos is not None and closed_pos.closed_at:
            funding = fetch_position_funding(client, closed_pos)
            if funding is not None:
                store.update_funding(closed_pos.id, funding_usd=funding)
                if abs(funding) >= 0.005:
                    net_total = pnl + funding
                    funding_suffix = (
                        f" funding=${funding:+.2f} net_total=${net_total:+.2f}"
                    )
    except Exception:
        log.exception("immediate funding fetch failed for id=%d", pos.id)

    return ApplyResult(
        executed=True,
        summary=(
            f"CLOSE id={pos.id} {pos.side} {pos.symbol} exit=${exit_price:.6g} "
            f"pnl=${pnl:+.2f} ({pnl_source}){funding_suffix}"
        ),
        thesis_status=thesis_status,
        thesis_invalidator=thesis_invalidator,
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
    # v0.30: macro_thesis (опционально на парсер-уровне для backward-compat
    # с paper/test, но обязательно в production через strict_v030_schema).
    macro_thesis_raw = raw.get("macro_thesis")
    macro_thesis = (
        str(macro_thesis_raw).strip()[:500]
        if isinstance(macro_thesis_raw, str) and macro_thesis_raw.strip()
        else None
    )
    # v0.40: 5-dim news sentiment УДАЛЁН (нет news-фида). Поля оставлены
    # в ApplyResult/БД для backward-compat со старыми decision-строками,
    # но всегда None — sentiment больше не парсится.
    aggregate_uncertainty: float | None = None
    sentiment_items_json: str | None = None

    # v0.31 (aggressive mandate): cost_estimate_usd — optional audit поле.
    # LLM ожидает посчитать fee_RT + funding-to-settlement и сравнить с
    # ожидаемой прибылью. Soft enforcement: невалидное значение → None
    # (не блокируем сделку, только не пишем в audit).
    cost_estimate_usd: float | None = None
    ce_raw = raw.get("cost_estimate_usd")
    if isinstance(ce_raw, (int, float)) and not isinstance(ce_raw, bool):
        ce_val = float(ce_raw)
        if 0.0 <= ce_val <= 50.0:  # реальный диапазон при $500 capital
            cost_estimate_usd = ce_val

    def _result(executed: bool, summary: str, error: str | None = None) -> ApplyResult:
        """v0.30 helper: каждый ApplyResult из _apply_open пробрасывает
        audit-trail (aggregate_uncertainty / sentiment_items_json) — это
        не зависит от того, executed True или False. main.py запишет в
        ``decisions`` через update_decision_sentiment.
        """
        return ApplyResult(
            executed=executed,
            summary=summary,
            error=error,
            aggregate_uncertainty=aggregate_uncertainty,
            sentiment_items_json=sentiment_items_json,
            cost_estimate_usd=cost_estimate_usd,
        )

    check = killswitch.check_can_open_position(leverage)
    if not check.allowed:
        return _result(False, "", error=f"killswitch: {check.reason}")

    ticker = client.get_ticker(symbol)
    if ticker is None or ticker.last_price <= 0:
        return _result(False, "", error=f"ticker unavailable for {symbol}")
    price = ticker.last_price

    # instruments-info — для round'инга qty/SL/TP под Bybit фильтры.
    info = client.get_instrument_info(symbol)
    if info is None:
        return _result(False, "", error=f"instruments-info unavailable for {symbol}")

    # Округляем SL/TP под tick_size ДО sanity-check'а — чтобы не падать
    # из-за плавающей точки LLM (1.38531 при tickSize 0.0001).
    sl_price = _round_to_step(sl_price, info.tick_size)
    tp_price = _round_to_step(tp_price, info.tick_size)

    # Sanity check на направление SL/TP
    if side == "Buy":
        if not (sl_price < price < tp_price):
            return _result(
                False, "",
                error=f"Buy: need SL<price<TP, got SL={sl_price} price={price} TP={tp_price}",
            )
    else:
        if not (sl_price > price > tp_price):
            return _result(
                False, "",
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
        return _result(
            False, "",
            error=(
                f"qty {qty} < min_order_qty {info.min_order_qty} for {symbol} "
                f"(notional ${notional_usd:.2f} / price {price} / step {info.qty_step})"
            ),
        )
    if qty > info.max_order_qty:
        qty = _floor_to_step(info.max_order_qty, info.qty_step)
    if qty <= 0:
        return _result(False, "", error="qty<=0 after rounding")

    # v0.20 (2026-05-28): hard fee-aware валидация. До v0.20 R:R и
    # risk_usd считались чисто по ценам — реальный убыток на SL =
    # (entry-SL)*qty + round-trip fees превышал заявленный cap, а
    # effective_R:R после fees мог быть < 1.0 при price R:R = 1.5.
    # Считаем оценку round-trip fee (используем текущий price для
    # обеих сторон — приближение, разница ~1% между entry и exit).
    fee_rate = max(0.0, float(getattr(settings, "taker_fee_pct", 0.0)))
    if fee_rate > 0:
        fee_RT_usd = price * qty * fee_rate * 2
        risk_usd_cap = settings.virtual_capital_usd * settings.risk_per_trade_pct
        # 1) net-risk cap: реальный убыток при SL hit = gross + fee_RT.
        #    Должен влезать в per-trade cap.
        net_risk_usd = risk_usd_declared + fee_RT_usd
        if net_risk_usd > risk_usd_cap:
            return _result(
                False, "",
                error=(
                    f"net_risk_exceeds_cap: declared risk_usd={risk_usd_declared:.2f} "
                    f"+ est. fee_RT={fee_RT_usd:.2f} = {net_risk_usd:.2f} > "
                    f"cap=${risk_usd_cap:.2f}. Reduce position_size_usd or widen SL."
                ),
            )
        # 2) effective R:R после fees.
        #    eff_reward = |TP-entry|*qty - fee_RT
        #    eff_risk   = |entry-SL|*qty + fee_RT
        reward_dist = abs(tp_price - price)
        risk_dist = abs(price - sl_price)
        eff_reward_usd = reward_dist * qty - fee_RT_usd
        eff_risk_usd = risk_dist * qty + fee_RT_usd
        if eff_risk_usd <= 0 or eff_reward_usd <= 0:
            return _result(
                False, "",
                error=(
                    f"eff_rr_non_positive: reward_dist={reward_dist} "
                    f"risk_dist={risk_dist} qty={qty} fee_RT={fee_RT_usd:.2f}"
                ),
            )
        eff_rr = eff_reward_usd / eff_risk_usd
        if eff_rr < 1.5:
            return _result(
                False, "",
                error=(
                    f"eff_rr_below_1.5: after fees "
                    f"(eff_reward=${eff_reward_usd:.2f} / "
                    f"eff_risk=${eff_risk_usd:.2f}) = {eff_rr:.2f}. "
                    f"Price-only R:R {reward_dist / risk_dist:.2f} doesn't "
                    f"survive round-trip fee ${fee_RT_usd:.2f}. "
                    f"Widen TP or pick larger-edge setup."
                ),
            )

    if not settings.trading_enabled:
        # PAPER MODE: не вызываем биржу, только пишем decision-only
        mth_suffix = (
            f" mth=\"{macro_thesis[:80]}\"" if macro_thesis else ""
        )
        return _result(
            False,
            (
                f"[PAPER] OPEN {side} {symbol} qty={qty} @ ${price:.6g} "
                f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
                f"conf={confidence:.2f} risk_decl=${risk_usd_declared:.2f} "
                f"inv=\"{invalidation_condition[:80]}\"{mth_suffix} — {reason}"
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
        return _result(
            False, "",
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
        macro_thesis=macro_thesis,
    )
    mth_suffix = f" mth=\"{macro_thesis[:80]}\"" if macro_thesis else ""
    return _result(
        True,
        (
            f"OPEN {side} {symbol} qty={qty} @ ${price:.6g} "
            f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
            f"conf={confidence:.2f} risk_decl=${risk_usd_declared:.2f} "
            f"inv=\"{invalidation_condition[:80]}\"{mth_suffix} — {reason}"
        ),
    )
