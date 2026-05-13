"""Парсинг ответа LLM (Pydantic-schema) + исполнение действий.

Цикл:
1. ``parse_action(text)`` → Pydantic-валидация JSON-блока. Pydantic
   schemas защищают от структурных hallucination'ов на agent boundary
   (стандартная практика для LLM-agents, см. TauricResearch
   TradingAgents).
2. ``apply_action(...)`` → KillSwitch broker-safety check → клиент
   cTrader (или paper-mode) → запись в БД.
3. Все ошибки → возврат ApplyResult с error, никаких exception наружу.

v1.0 (12-May-2026): сняты hard caps R:R ≥ 1.5 и risk_per_trade ≤ $25.
LLM получает свободу профессионального discretionary trader. Остались
только broker-input validations:
- SL/TP в правильную сторону (broker отвергнет иначе).
- volume > 0 после rounding к step.
- max_lot_size clamp (catastrophic broker margin safety).
- aggregate_uncertainty > 0.7 → reject (anti-hallucination gate).
- KillSwitch: max_open_positions, daily/total loss cap.
См. docstring в prompts.py для полного research-basis.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.safety.killswitch import KillSwitch
from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

log = logging.getLogger(__name__)


# ─── Pydantic schemas ────────────────────────────────────────────────────


# ─── Defensive coercion helpers (research-backed) ────────────────────────
#
# Подход — Pydantic annotated pattern с BeforeValidator + Field constraint.
# Это **рекомендованный** способ для борьбы с LLM out-of-range / hallucination
# по официальным источникам:
#   - Pydantic ofic docs «Validators»
#     (https://docs.pydantic.dev/latest/concepts/validators)
#   - Pydantic blog «Minimize LLM Hallucinations with Pydantic Validators»
#     (https://blog.pydantic.dev/blog/2024/01/18/llm-validation/)
#   - Instructor «Validation & Retry» best-practices
#     (https://python.useinstructor.com/learning/validation/)
#   - tianpan.co «Structured Outputs Not Solved Problem», 2026
#     (https://tianpan.co/blog/2026-04-18-structured-output-json-mode-failure-modes)
#
# Bug-fix 13-May-2026: LLM прислал forwardness=-0.3 (путает с polarity ∈ [-1, 1]).
# Раньше parse_action отвергал _всё_ решение из-за одного кривого значения в
# audit-блоке. Теперь clamp вместо reject — sentiment остаётся информативным
# для aggregate_uncertainty gate, но не блокирует core decision (open/close/hold).


def _coerce_unit(value: Any) -> float:
    """Clamp произвольного input'а к [0.0, 1.0].

    Терпим к None / NaN / inf / нечисловым типам (defensive):
    LLM может пропустить поле или прислать строку «N/A». Возвращаем 0.0
    как safe default — это самое нейтральное значение для sentiment
    (низкая relevance / intensity = «новость не важна»).
    """
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return max(0.0, min(1.0, v))


def _coerce_signed_unit(value: Any) -> float:
    """Clamp к [-1.0, 1.0] — для polarity (единственное signed-измерение)."""
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return max(-1.0, min(1.0, v))


# Annotated type aliases — переиспользуемые «strict + safe» типы.
# Field(ge/le) остаётся как формальная схема (для документации, OpenAPI
# export, IDE-hint), а BeforeValidator делает clamp ДО проверки constraint,
# так что constraint фактически всегда проходит.
UnitFloat = Annotated[float, BeforeValidator(_coerce_unit), Field(ge=0.0, le=1.0)]
SignedUnitFloat = Annotated[
    float, BeforeValidator(_coerce_signed_unit), Field(ge=-1.0, le=1.0)
]


class SentimentItem(BaseModel):
    """Multi-dim sentiment per news. Все dimensions clamp'ятся защитно
    (см. блок _coerce_* выше). Out-of-range от LLM (например forwardness=-0.3)
    не отвергает решение, а заменяется ближайшей границей."""

    title_snippet: str = Field(default="", max_length=200)
    relevance: UnitFloat
    polarity: SignedUnitFloat
    intensity: UnitFloat
    uncertainty: UnitFloat
    forwardness: UnitFloat


class SentimentBlock(BaseModel):
    """Sentiment audit-block. ``aggregate_uncertainty`` используется в
    parse_action как anti-hallucination gate (>0.7 → reject open).
    """

    aggregate_uncertainty: UnitFloat
    items: list[SentimentItem] = Field(default_factory=list)


class OpenAction(BaseModel):
    action: Literal["open"]
    symbol: str
    side: Literal["BUY", "SELL"]
    volume_lots: float = Field(gt=0.0, le=10.0)
    stop_loss: float = Field(gt=0.0)
    take_profit: float = Field(gt=0.0)
    reason: str = Field(default="", max_length=300)
    sentiment: Optional[SentimentBlock] = None


class CloseAction(BaseModel):
    action: Literal["close"]
    position_id: int = Field(gt=0)
    reason: str = Field(default="", max_length=300)


class HoldAction(BaseModel):
    action: Literal["hold"]
    reason: str = Field(default="", max_length=300)
    sentiment: Optional[SentimentBlock] = None


@dataclass
class ParsedAction:
    """Внутренний контейнер: тип + сам Pydantic-объект + raw-словарь."""
    action_type: str  # "open" / "close" / "hold"
    model: OpenAction | CloseAction | HoldAction
    raw: dict[str, Any]


@dataclass
class ApplyResult:
    executed: bool
    summary: str
    error: str | None = None


# ─── Schema parsing ──────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_last_json_object(text: str) -> dict[str, Any] | str:
    """Возвращает последний balanced JSON-объект из текста или строку-ошибку.

    Логика как у ai_trader.executor: терпим markdown-фенсы, ищем JSON
    с конца через скобочный счётчик. Берём первый успешно парсящийся
    объект с ключом ``action`` — это decision-блок (sentiment может
    встречаться отдельно как inline-блок в commentary, но он не
    decision-level).
    """
    if not text:
        return "empty response"
    cleaned = text.strip()
    fence = _FENCE_RE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    end = len(cleaned)
    last_err: Exception | None = None
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
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
            last_err = ValueError(
                f"not a decision dict (missing 'action'): {type(parsed).__name__}"
            )
        except json.JSONDecodeError as e:
            last_err = e
        end = start_brace

    if last_err is not None:
        return f"JSON parse error: {last_err}"
    return f"no JSON object with 'action' found: {cleaned[:120]}"


def parse_action(
    text: str,
    allowed_symbols: tuple[str, ...],
    *,
    review_mode: bool = False,
    max_uncertainty: float = 0.7,
) -> ParsedAction | str:
    """Возвращает ParsedAction или строку с описанием ошибки.

    Schema-validation через Pydantic — структурные ошибки не доходят до
    apply-стадии (Risk 1 mitigation: research TauricResearch #458).

    review_mode: если True, action="open" отвергается (review-промпт явно
    запрещает open; hard-guard на случай если LLM проигнорировал
    инструкцию).

    max_uncertainty: если open-decision имеет aggregate_uncertainty выше
    порога → reject. Это anti-hallucination gate: LLM сам должен был
    вернуть "hold" при высокой uncertainty (так указано в промпте),
    но если он этого не сделал — режем здесь.
    """
    obj = _extract_last_json_object(text)
    if isinstance(obj, str):
        return obj

    action_type = obj.get("action")
    if action_type not in ("open", "close", "hold"):
        return f"invalid action: {action_type!r}"

    if review_mode and action_type == "open":
        return "review_mode: 'open' action is forbidden in review cycle"

    try:
        if action_type == "open":
            model = OpenAction.model_validate(obj)
            if model.symbol not in allowed_symbols:
                return f"symbol {model.symbol!r} not in allowed list {list(allowed_symbols)}"
            # Anti-hallucination gate: LLM сам должен был вернуть hold
            # при высокой uncertainty (так указано в SYSTEM_PROMPT),
            # но если попытался open — режем.
            if (
                model.sentiment is not None
                and model.sentiment.aggregate_uncertainty > max_uncertainty
            ):
                return (
                    f"high aggregate_uncertainty "
                    f"({model.sentiment.aggregate_uncertainty:.2f} > "
                    f"{max_uncertainty}) — open blocked by anti-hallucination "
                    f"gate; LLM должен был вернуть hold"
                )
        elif action_type == "close":
            model = CloseAction.model_validate(obj)
        else:
            model = HoldAction.model_validate(obj)
    except ValidationError as e:
        return f"schema validation error: {e.errors(include_url=False)}"

    return ParsedAction(action_type=action_type, model=model, raw=obj)


# ─── Apply ───────────────────────────────────────────────────────────────


def apply_action(
    action: ParsedAction,
    *,
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
    settings: AiFxTraderSettings,
    killswitch: KillSwitch,
) -> ApplyResult:
    if action.action_type == "hold":
        reason = action.raw.get("reason", "")
        return ApplyResult(executed=False, summary=f"HOLD: {reason}")

    if action.action_type == "close":
        return _apply_close(action, adapter=adapter, store=store, settings=settings)

    if action.action_type == "open":
        return _apply_open(
            action, adapter=adapter, store=store,
            settings=settings, killswitch=killswitch,
        )

    return ApplyResult(executed=False, summary="unknown action", error="impossible branch")


def _apply_close(
    action: ParsedAction,
    *,
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
    settings: AiFxTraderSettings,
) -> ApplyResult:
    assert isinstance(action.model, CloseAction)
    pos_id = action.model.position_id
    db_positions = store.get_open_positions()
    pos = next((p for p in db_positions if p.id == pos_id), None)
    if pos is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"position id={pos_id} not found among open positions",
        )

    # Текущая цена — нужна для расчёта realized P&L (и в paper, и в live).
    current_price = adapter.get_current_price(pos.symbol)
    if current_price is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"current price unavailable for {pos.symbol}",
        )
    pnl_usd = _calc_pnl_usd(
        side=pos.side,
        entry=pos.entry_price,
        exit_price=current_price,
        volume_lots=pos.volume_lots,
        symbol=pos.symbol,
    )

    # LIVE-режим: дёргаем broker.
    if (
        not pos.is_paper
        and settings.trading_enabled
        and pos.broker_position_id is not None
    ):
        info = adapter.get_symbol_info(pos.symbol)
        contract_size = info.contract_size if info else 100_000
        volume_int = int(round(pos.volume_lots * contract_size))
        res = adapter.close_position(pos.broker_position_id, volume_int)
        if not res.success:
            return ApplyResult(
                executed=False, summary="",
                error=f"broker close_failed: {res.error}",
            )

    store.close_position(
        pos.id,
        exit_price=current_price,
        realized_pnl_usd=pnl_usd,
        close_reason=action.raw.get("reason", "llm_close"),
    )
    mode = "PAPER" if pos.is_paper else "LIVE"
    return ApplyResult(
        executed=True,
        summary=(
            f"[{mode}] CLOSE id={pos.id} {pos.side} {pos.symbol} "
            f"lots={pos.volume_lots} entry=${pos.entry_price:.6g} "
            f"exit=${current_price:.6g} pnl=${pnl_usd:+.2f}"
        ),
    )


def _apply_open(
    action: ParsedAction,
    *,
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
    settings: AiFxTraderSettings,
    killswitch: KillSwitch,
) -> ApplyResult:
    assert isinstance(action.model, OpenAction)
    m = action.model
    reason = m.reason[:300]

    ks = killswitch.check_can_open_position(symbol=m.symbol, side=m.side)
    if not ks.allowed:
        return ApplyResult(executed=False, summary="", error=f"killswitch: {ks.reason}")

    current_price = adapter.get_current_price(m.symbol)
    if current_price is None or current_price <= 0:
        return ApplyResult(
            executed=False, summary="",
            error=f"current price unavailable for {m.symbol}",
        )

    # Sanity: SL/TP в правильную сторону.
    if m.side == "BUY":
        if not (m.stop_loss < current_price < m.take_profit):
            return ApplyResult(
                executed=False, summary="",
                error=(
                    f"BUY direction: need SL<price<TP, got "
                    f"SL={m.stop_loss} price={current_price} TP={m.take_profit}"
                ),
            )
    else:
        if not (m.stop_loss > current_price > m.take_profit):
            return ApplyResult(
                executed=False, summary="",
                error=(
                    f"SELL direction: need SL>price>TP, got "
                    f"SL={m.stop_loss} price={current_price} TP={m.take_profit}"
                ),
            )

    # R:R больше НЕ hard-cap'нут (v1.0). LLM сам решает R:R по setup'у:
    # scalp может 1.2, swing 3.0+. Считаем для лога/учёта.
    risk_distance = abs(current_price - m.stop_loss)
    reward_distance = abs(m.take_profit - current_price)
    if risk_distance <= 0:
        return ApplyResult(executed=False, summary="", error="risk distance == 0")
    r_r = reward_distance / risk_distance

    # size_multiplier в v1.0 всегда 1.0 (correlation haircut снят); поле
    # сохранено для API stability.
    volume_lots = m.volume_lots * ks.size_multiplier

    info = adapter.get_symbol_info(m.symbol)
    if info is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"symbol info unavailable for {m.symbol}",
        )

    # Round lot к step и clamp к max_lot_size + min_volume.
    step_lots = info.step_volume / info.contract_size if info.contract_size else 0.01
    volume_lots = _round_to_step(volume_lots, step_lots)
    if volume_lots > settings.max_lot_size:
        log.info(
            "FX-AI clamp: volume_lots %.4f → MAX_LOT_SIZE %.2f",
            volume_lots, settings.max_lot_size,
        )
        volume_lots = settings.max_lot_size
        volume_lots = _round_to_step(volume_lots, step_lots)
    if volume_lots <= 0:
        return ApplyResult(executed=False, summary="", error="volume_lots <= 0 после rounding")

    # v1.0: hard cap по risk-per-trade USD снят. LLM сам решает risk
    # size по Van Tharp R-multiple (см. SYSTEM_PROMPT, "Position size"
    # секция). Catastrophic floor — max_lot_size clamp выше + KillSwitch
    # daily/total loss caps снизу. Считаем risk_usd для audit-логов.
    pip_size = _pip_size_for(m.symbol)
    sl_pips = risk_distance / pip_size if pip_size > 0 else 0
    risk_usd = sl_pips * volume_lots * _pip_value_per_std_lot(m.symbol)

    # ─── PAPER MODE ──────────────────────────────────────────────────────
    if not settings.trading_enabled:
        pos_id = store.open_position(
            symbol=m.symbol, side=m.side,
            volume_lots=volume_lots, entry_price=current_price,
            sl_price=m.stop_loss, tp_price=m.take_profit,
            broker_position_id=None,
            broker_order_label=settings.order_label,
            llm_reason=reason, is_paper=True,
        )
        return ApplyResult(
            executed=True,
            summary=(
                f"[PAPER] OPEN id={pos_id} {m.side} {m.symbol} lots={volume_lots} "
                f"@ ${current_price:.6g} SL=${m.stop_loss:.6g} TP=${m.take_profit:.6g} "
                f"R:R={r_r:.2f} risk=${risk_usd:.2f} — {reason}"
            ),
        )

    # ─── LIVE MODE ───────────────────────────────────────────────────────
    res = adapter.place_market_order(
        internal_symbol=m.symbol, side=m.side,
        volume_lots=volume_lots,
        sl_price=m.stop_loss, tp_price=m.take_profit,
        comment=f"ai-fx-trader:{uuid.uuid4().hex[:8]}",
    )
    if not res.success:
        return ApplyResult(
            executed=False, summary="",
            error=f"broker open_failed: {res.error}",
        )
    pos_id = store.open_position(
        symbol=m.symbol, side=m.side,
        volume_lots=res.volume_lots, entry_price=res.fill_price or current_price,
        sl_price=m.stop_loss, tp_price=m.take_profit,
        broker_position_id=res.broker_position_id,
        broker_order_label=settings.order_label,
        llm_reason=reason, is_paper=False,
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"[LIVE] OPEN id={pos_id} broker={res.broker_position_id} "
            f"{m.side} {m.symbol} lots={res.volume_lots} "
            f"fill=${res.fill_price:.6g} SL=${m.stop_loss:.6g} TP=${m.take_profit:.6g} "
            f"R:R={r_r:.2f} — {reason}"
        ),
    )


# ─── Helpers ─────────────────────────────────────────────────────────────


def _round_to_step(value: float, step: float) -> float:
    """Round-DOWN к ближайшему step."""
    if step <= 0:
        return round(value, 6)
    n = int(value / step)
    return round(n * step, 6)


def _pip_size_for(symbol: str) -> float:
    """Pip size в АБСОЛЮТНЫХ единицах цены для XAUUSD / BZ=F.

    На FxPro обычно: XAUUSD digits=2, BRENT digits=2. 1 pip = 0.01.
    """
    if symbol in ("XAUUSD", "BZ=F"):
        return 0.01
    return 0.0001


def _pip_value_per_std_lot(symbol: str) -> float:
    """USD-стоимость 1 pip за 1 standard lot.

    Для XAUUSD: 1 std lot = 100 oz × $0.01 = $1.0 per pip.
    Для BRENT (1 lot = 100 barrels на FxPro): 100 × $0.01 = $1.0 per pip.
    На demo может отличаться (FxPro contract specs), уточняется при
    paper-observation. На Phase 1 принимаем $1.0/pip/lot как baseline.
    """
    return 1.0


def _calc_pnl_usd(
    *,
    side: str,
    entry: float,
    exit_price: float,
    volume_lots: float,
    symbol: str,
) -> float:
    """USD P&L через pip-distance × volume × pip_value."""
    pip_size = _pip_size_for(symbol)
    pip_value = _pip_value_per_std_lot(symbol)
    if side.upper() == "BUY":
        pip_diff = (exit_price - entry) / pip_size
    else:
        pip_diff = (entry - exit_price) / pip_size
    return pip_diff * volume_lots * pip_value
