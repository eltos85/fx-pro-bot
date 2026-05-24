"""Парсер Nof1-style ответа LLM и исполнение действий.

Output schema (см. правило `ai-arena-sources.mdc`, gist nof1-prompt.md
line 165-178):

    {
      "signal": "buy_to_enter" | "sell_to_enter" | "hold" | "close",
      "coin":   "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP",
      "quantity": <float>,
      "leverage": <integer 1-20>,
      "stop_loss":     <float>,
      "profit_target": <float>,
      "invalidation_condition": "<string>",
      "confidence": <float 0-1>,
      "risk_usd":   <float>,
      "justification": "<string ≤ 500 chars>"
    }

`coin` — голые тикеры **без USDT-суффикса** (1-в-1 с source). При
исполнении на Bybit маппятся через `arena_to_bybit` (`BTC` → `BTCUSDT`).
В БД храним Bybit-формат (`BTCUSDT`), для prompt'а конвертируем
обратно. См. `trading/symbols.py`.

ВАЖНО — никаких server-side capital safety hard-checks (нет в источнике
Nof1). Bot выполняет только:

1. Sanity-парсинг: signal ∈ allowed, coin ∈ whitelist (Nof1-format),
   типы полей, confidence ∈ [0,1], leverage ≥ 1, quantity > 0
   (для entries).
2. Direction sanity: LONG  → SL < price < TP;  SHORT → TP < price < SL
   (gist § OUTPUT VALIDATION RULES line 183-184). Формальное требование
   source, не риск-фильтр.
3. Bybit-rounding: qty под `lotSizeFilter.qtyStep`, SL/TP под
   `priceFilter.tickSize` (Bybit V5 требование, не Nof1).
4. Реальный fill price: после `place_order` читаем `position.avgPrice`
   из Bybit и сохраняем в БД (вместо нашего ticker.last_price ДО
   ордера, который игнорировал slippage). Реальный exit и net PnL —
   из `get_closed_pnl` (Bybit `closedPnl` уже после fees + funding).

Risk management полностью на стороне LLM (формула risk_usd, Sharpe
feedback, invalidation_condition, conviction-mapping leverage). См.
gist § "RISK MANAGEMENT PROTOCOL (MANDATORY)" — это инструкции LLM,
не серверный код.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ai_arena.config.settings import AiArenaSettings
from ai_arena.state.db import AiArenaStore
from ai_arena.trading.client import AiArenaBybitClient
from ai_arena.trading.notional_cap import apply_notional_cap, format_rescale_notice
from ai_arena.trading.symbols import arena_to_bybit, arena_symbols

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

    ``allowed_symbols`` — кортеж Bybit-формата (``BTCUSDT``, ``ETHUSDT``,
    …). Внутри parser whitelist получается через ``arena_symbols(...)``
    → проверяем coin LLM-ответа против Nof1-формата (``BTC``, ``ETH``,
    …). Это даёт LLM source-faithful enum, а Bybit-вызовы остаются
    с USDT-суффиксом.

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

    nof1_whitelist = arena_symbols(allowed_symbols)
    coin = obj.get("coin")
    if coin not in nof1_whitelist:
        return f"coin {coin!r} not in allowed list {nof1_whitelist}"

    if signal == "close":
        return ParsedAction(signal="close", raw=obj)

    # buy_to_enter / sell_to_enter — sanity-валидация типов и диапазонов.
    # Никаких "капитальных" cap'ов (max_risk, max_lev, min_RR) — они не
    # описаны в Nof1 источниках. Risk management на стороне LLM.
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
) -> ApplyResult:
    """Применяет распарсенное LLM-решение.

    Никаких killswitch / capital-safety hard-checks. Source Nof1 их не
    имеет — risk management на стороне LLM (через required JSON-поля
    confidence / invalidation_condition / risk_usd / stop_loss /
    profit_target и Sharpe feedback). См. gist § "RISK MANAGEMENT
    PROTOCOL (MANDATORY)".

    Серверная валидация ограничена sanity-парсингом и Bybit-rounding'ом
    (qty_step, tick_size — это требования Bybit API, не Nof1).
    """
    if action.signal == "hold":
        just = action.raw.get("justification", "")
        return ApplyResult(executed=False, summary=f"HOLD: {just[:200]}")

    if action.signal == "close":
        return _apply_close(action, client=client, store=store)

    if action.signal in {"buy_to_enter", "sell_to_enter"}:
        return _apply_open(
            action, client=client, store=store, settings=settings,
        )

    return ApplyResult(
        executed=False, summary="", error=f"unknown signal: {action.signal}"
    )


def _apply_close(
    action: ParsedAction, *, client: AiArenaBybitClient, store: AiArenaStore
) -> ApplyResult:
    coin = action.raw["coin"]
    bybit_symbol = arena_to_bybit(coin)
    pos = next(
        (p for p in store.get_open_positions() if p.symbol == bybit_symbol),
        None,
    )
    if pos is None:
        return ApplyResult(
            executed=False, summary="",
            error=f"close: no open position for {coin} ({bybit_symbol})",
        )

    # Снимок wallet balance USDT РОВНО перед close-ордером —
    # для fallback'а _resolve_pnl_from_balance_delta когда Bybit
    # closed-pnl endpoint молчит (наблюдено demo latency 5+ минут,
    # BUILDLOG 2026-05-15). Bybit обновляет walletBalance мгновенно
    # при executed close (списывает PnL+fees сразу). None если
    # запрос упал — fallback просто не сработает, останется текущее
    # поведение (closed-pnl + reconcile_pending_pnl).
    wallet_before = client.get_wallet_balance_usdt()
    if wallet_before is not None:
        store.set_wallet_before_close(pos.id, wallet_before)

    link_id = f"arena_close_{uuid.uuid4().hex[:10]}"
    resp = client.close_position(pos.symbol, pos.side, pos.qty, link_id)
    if not resp or not resp.get("ok"):
        err_msg = (resp or {}).get("error", "close_position returned empty")
        return ApplyResult(executed=False, summary="", error=f"close_failed: {err_msg}")

    # Primary: Bybit `closed-pnl` (net PnL + avgExitPrice от биржи 1-в-1).
    # Локальный `(exit-entry)*qty` запрещён — игнорирует fees и
    # funding (BUILDLOG 2026-05-15).
    exit_price, pnl = _resolve_net_close(
        client=client,
        symbol=pos.symbol,
        opened_at_iso=pos.opened_at,
        opened_side=pos.side,
        qty=pos.qty,
        fallback_entry=pos.entry_price,
    )

    # Fallback: balance delta (только если closed-pnl молчит И мы
    # сохранили wallet_before выше). Net PnL = wallet_after - wallet_before
    # — это и есть net of fees + funding (Bybit списывает их в
    # walletBalance моментально). Источник правды: Bybit V5 wallet-balance
    # docs (https://bybit-exchange.github.io/docs/v5/account/wallet-balance).
    if pnl is None and wallet_before is not None:
        pnl = _resolve_pnl_from_balance_delta(
            client=client, wallet_before=wallet_before,
            position_id=pos.id, symbol=pos.symbol,
        )

    store.close_position(
        pos.id,
        exit_price=exit_price,
        realized_pnl_usd=pnl,  # None допустимо → reconcile_pending_pnl добьёт
        close_reason=action.raw.get("justification", "llm_close")[:200],
    )
    if pnl is None:
        summary = (
            f"CLOSE id={pos.id} {pos.side} {pos.symbol} "
            f"exit=${exit_price:.6g} pnl=pending… (биржа ещё не зарегистрировала, "
            f"добьём на след. цикле)"
        )
    else:
        summary = (
            f"CLOSE id={pos.id} {pos.side} {pos.symbol} "
            f"exit=${exit_price:.6g} pnl=${pnl:+.2f} (net of fees)"
        )
    return ApplyResult(executed=True, summary=summary)


def _resolve_pnl_from_balance_delta(
    *,
    client: AiArenaBybitClient,
    wallet_before: float,
    position_id: int,
    symbol: str,
    settle_wait_sec: float = 1.5,
) -> float | None:
    """Считает net PnL как ``walletBalance_after - wallet_before``.

    Используется как fallback в ``_apply_close`` и
    ``reconcile_pending_pnl`` когда Bybit ``/v5/position/closed-pnl``
    endpoint молчит (наблюдено demo latency 5+ минут эмпирически
    2026-05-15). На mainnet обычно <10s — fallback редко срабатывает.

    Bybit V5 ``walletBalance`` (UNIFIED account, USDT coin) обновляется
    мгновенно при executed close: биржа списывает realized PnL и fees
    в walletBalance сразу, **без задержки агрегации** в closed-pnl
    endpoint. ``unrealisedPnl`` по другим открытым позициям сюда **не
    попадает** (это в ``equity = walletBalance + unrealisedPnL``).

    Edge cases:
    - Funding payment в окне ``settle_wait_sec`` (раз в 8ч в 00/08/16
      UTC): попадёт в дельту. Окно 1.5s vs ~28800s между funding —
      крайне маловероятное пересечение, игнорируем.
    - Deposit/transfer в окне: то же — игнорируем (1.5s vs минуты-часы
      между такими событиями).
    - ``get_wallet_balance_usdt`` упал → возвращаем None, caller
      пишет ``realized_pnl_usd=NULL`` → ``reconcile_pending_pnl``
      попробует на следующем цикле.

    Bybit docs: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
    """
    # Маленькая задержка чтобы Bybit зарегистрировал fill в walletBalance.
    # Эмпирически достаточно ~1s на mainnet, 1.5s даёт запас на demo.
    time.sleep(settle_wait_sec)

    wallet_after = client.get_wallet_balance_usdt()
    if wallet_after is None:
        log.warning(
            "balance-delta fallback: get_wallet_balance_usdt=None for id=%d %s",
            position_id, symbol,
        )
        return None

    delta = wallet_after - wallet_before
    log.info(
        "balance-delta resolved net PnL for id=%d %s: "
        "wallet_before=%.6f wallet_after=%.6f → pnl=$%+.4f",
        position_id, symbol, wallet_before, wallet_after, delta,
    )
    return delta


def _resolve_net_close(
    *,
    client: AiArenaBybitClient,
    symbol: str,
    opened_at_iso: str,
    opened_side: str,
    qty: float,
    fallback_entry: float,
    max_retries: int = 6,
    retry_backoff_sec: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0),
) -> tuple[float, float | None]:
    """Возвращает ``(avg_exit_price, net_pnl)`` из Bybit `get_closed_pnl`.

    Bybit demo `/v5/position/closed-pnl` имеет наблюдаемую latency
    регистрации запис: 1-10 сек на mainnet, **до 5+ минут на demo**
    (эмпирически 2026-05-15). Поэтому делаем `max_retries` попыток с
    backoff `retry_backoff_sec` (всего ≤ 30 секунд ожидания) перед
    сдачей.

    **Не передаём** `start_time_ms` в Bybit запрос: на demo endpoint
    глючит и возвращает 0 записей даже когда `record.updated_time >
    start_time` (наблюдено 2026-05-15: с фильтром n=0, без фильтра
    n=20). Полагаемся на дефолтное 7-day окно + `symbol`-фильтр +
    `qty/side/exec_type` матч.

    Также фильтруем `exec_type ∈ {Trade, BustTrade}` — исключаем
    Settle / SessionSettlePnL / MovePosition записи (Bybit V5 docs).

    Если по истечении всех попыток матч не найден — возвращаем
    `(ticker.last_price, None)`. **None** сигнализирует «PnL ещё не
    знаем»: caller сохранит `None` в БД, а `reconcile_pending_pnl` на
    следующем цикле подберёт net PnL через тот же endpoint (на demo
    Bybit может задержать на >5 минут, добивание идёт несколько
    циклов). Это лучше чем `0.0` (он ломал UX и врал про net PnL).
    """
    closing_side = "Sell" if opened_side == "Buy" else "Buy"
    try:
        opened_ts_ms = _iso_to_ms(opened_at_iso)
    except ValueError:
        opened_ts_ms = int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000

    qty_tolerance = max(qty * 1e-4, 1e-8)

    for attempt in range(max_retries):
        # ВАЖНО: не передаём start_time_ms — Bybit demo фильтрует не
        # по тому полю и режет валидные записи (BUILDLOG 2026-05-15).
        records = client.get_closed_pnl(symbol=symbol, limit=100)
        if records is None:
            log.warning(
                "get_closed_pnl=None for %s (attempt %d/%d) — retry",
                symbol, attempt + 1, max_retries,
            )
        else:
            candidates = [
                r for r in records
                if r.symbol == symbol
                and r.side == closing_side
                and abs(r.qty - qty) <= qty_tolerance
                and r.exec_type in ("Trade", "BustTrade")
                and r.updated_time_ms >= opened_ts_ms
            ]
            if candidates:
                rec = candidates[-1]  # самая поздняя по времени
                if attempt > 0:
                    log.info(
                        "closed_pnl matched for %s on attempt %d/%d",
                        symbol, attempt + 1, max_retries,
                    )
                return rec.avg_exit_price, rec.closed_pnl

        if attempt < max_retries - 1:
            wait = retry_backoff_sec[min(attempt, len(retry_backoff_sec) - 1)]
            time.sleep(wait)

    log.warning(
        "no closed_pnl match for %s side=%s qty=%s after %d attempts — "
        "defer net PnL to next cycle reconcile_pending_pnl",
        symbol, closing_side, qty, max_retries,
    )
    return _ticker_fallback_exit(client, symbol, fallback_entry), None


def _ticker_fallback_exit(
    client: AiArenaBybitClient, symbol: str, fallback_entry: float,
) -> float:
    t = client.get_ticker(symbol)
    return t.last_price if t and t.last_price > 0 else fallback_entry


def _iso_to_ms(iso: str) -> int:
    """ISO-8601 (UTC) → unix ms. Допускает суффиксы `Z` и `+00:00`."""
    from datetime import datetime as _dt

    s = iso.replace("Z", "+00:00")
    return int(_dt.fromisoformat(s).timestamp() * 1000)


def _apply_open(
    action: ParsedAction,
    *,
    client: AiArenaBybitClient,
    store: AiArenaStore,
    settings: AiArenaSettings,
) -> ApplyResult:
    raw = action.raw
    coin = raw["coin"]
    bybit_symbol = arena_to_bybit(coin)  # `BTC` → `BTCUSDT` для Bybit V5
    side = "Buy" if action.signal == "buy_to_enter" else "Sell"
    qty_req = float(raw["quantity"])
    leverage = int(raw["leverage"])
    sl_price = float(raw["stop_loss"])
    tp_price = float(raw["profit_target"])
    confidence = float(raw.get("confidence", 0))
    risk_usd_claimed = float(raw.get("risk_usd", 0))
    invalidation = str(raw.get("invalidation_condition", ""))[:500]
    justification = str(raw.get("justification", ""))[:500]

    # 1) Уже есть открытая по этой coin? Source: «one position per coin
    # maximum» (gist: "NO pyramiding"). Это формальное правило source.
    if any(p.symbol == bybit_symbol for p in store.get_open_positions()):
        return ApplyResult(
            executed=False, summary="",
            error=f"already have open position for {coin} (no pyramiding)",
        )

    # 2) Текущая цена + instrument-info (Bybit требует qty_step / tick_size)
    ticker = client.get_ticker(bybit_symbol)
    if ticker is None or ticker.last_price <= 0:
        return ApplyResult(executed=False, summary="", error=f"ticker unavailable for {coin}")
    price = ticker.last_price

    info = client.get_instrument_info(bybit_symbol)
    if info is None:
        return ApplyResult(executed=False, summary="", error=f"instrument-info unavailable for {coin}")

    sl_price = _round_to_step(sl_price, info.tick_size)
    tp_price = _round_to_step(tp_price, info.tick_size)

    # 3) Direction sanity (формальное требование source: gist § OUTPUT
    # VALIDATION RULES — "stop_loss must be below entry price for longs,
    # above for shorts; profit_target must be above entry price for
    # longs, below for shorts").
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

    # 4) Округляем qty под qty_step (Bybit lotSizeFilter)
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

    # 4a) v2.z3 user-approved exception #4 (2026-05-22): server-side
    # notional cap. Если notional (`qty × price`) > 30% × virtual_capital
    # (default $3000) — silent rescale до cap, факт фиксируется в
    # kv_state и показывается LLM в следующем prompt'е блоком
    # «System notice». См. .cursor/rules/ai-arena-sources.mdc § «Допустимые
    # исключения» исключение #4.
    cap_result = apply_notional_cap(
        requested_qty=qty,
        price=price,
        qty_step=info.qty_step,
        min_order_qty=info.min_order_qty,
        virtual_capital_usd=settings.virtual_capital_usd,
        max_allocation_pct=settings.max_allocation_pct,
    )
    if cap_result.rescaled or cap_result.rejected:
        notice = format_rescale_notice(
            coin=coin,
            side=side,
            cap=cap_result,
            leverage=leverage,
            virtual_capital_usd=settings.virtual_capital_usd,
            max_allocation_pct=settings.max_allocation_pct,
        )
        store.kv_set("pending_rescale_notice", notice)
        log.warning(
            "notional cap: %s qty %g→%g ($%.2f→$%.2f, max=$%.2f, rejected=%s)",
            coin, cap_result.original_qty, cap_result.capped_qty,
            cap_result.original_notional, cap_result.capped_notional,
            cap_result.max_notional, cap_result.rejected,
        )
    if cap_result.rejected:
        return ApplyResult(
            executed=False, summary="",
            error=(
                f"notional cap rejected: {coin} requested notional "
                f"${cap_result.original_notional:,.2f} > cap "
                f"${cap_result.max_notional:,.2f}, rescaled qty "
                f"{cap_result.capped_qty:g} < min_order_qty "
                f"{info.min_order_qty}"
            ),
        )
    qty = cap_result.capped_qty  # либо без изменений, либо после rescale

    # risk_usd_actual — для логирования и записи в БД (для аналитики).
    # Никакого hard-cap'а: source формула `|entry - stop_loss| × quantity`
    # инструктирует LLM, не серверный код.
    risk_dist = abs(price - sl_price)
    reward_dist = abs(tp_price - price)
    risk_usd_actual = risk_dist * qty
    rr = (reward_dist / risk_dist) if risk_dist > 0 else 0.0
    notional = qty * price

    if not settings.trading_enabled:
        return ApplyResult(
            executed=False,
            summary=(
                f"[PAPER] {action.signal.upper()} {coin} qty={qty} @ ${price:.6g} "
                f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x conf={confidence:.2f} "
                f"R:R={rr:.2f} risk=${risk_usd_actual:.2f} (claimed ${risk_usd_claimed:.2f}) "
                f"notional=${notional:.2f} — {justification[:150]}"
            ),
        )

    # 5) Live: set_leverage → place_order
    if not client.set_leverage(bybit_symbol, leverage):
        log.warning(
            "set_leverage %s %dx failed before place_order — продолжаем",
            bybit_symbol, leverage,
        )
    link_id = f"arena_{uuid.uuid4().hex[:12]}"
    resp = client.place_order(
        symbol=bybit_symbol,
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
                f"(symbol={bybit_symbol} side={side} qty={qty} lev={leverage}x)"
            ),
        )

    # 6) Реальный entry_price из Bybit (после fill, с учётом slippage).
    # Source предполагает actual fill price; локальный `ticker.last_price`
    # ДО ордера — это slippage-смещённый эстимат, который ломал
    # PnL-расчёты (см. BUILDLOG 2026-05-15 «net PnL alignment»).
    # Если получить avgPrice не удалось (API outage / latency) — fallback
    # на ticker price (с warning в лог).
    real_entry, real_qty = _resolve_real_open(client, bybit_symbol, side, qty)
    if real_entry is None:
        log.warning(
            "could not fetch position.avgPrice for %s after open — "
            "falling back to ticker.last_price (entry will be slightly off)",
            bybit_symbol,
        )
        real_entry = price
    final_qty = real_qty if real_qty is not None else qty
    real_risk_usd = abs(real_entry - sl_price) * final_qty
    real_notional = final_qty * real_entry

    store.open_position(
        symbol=bybit_symbol,
        side=side,
        qty=final_qty,
        entry_price=real_entry,
        sl_price=sl_price,
        tp_price=tp_price,
        leverage=leverage,
        order_link_id=link_id,
        llm_justification=justification,
        confidence=confidence,
        invalidation_condition=invalidation,
        risk_usd=real_risk_usd,
    )
    real_rr = (
        abs(tp_price - real_entry) / abs(real_entry - sl_price)
        if abs(real_entry - sl_price) > 0
        else 0.0
    )
    return ApplyResult(
        executed=True,
        summary=(
            f"OPEN {action.signal.upper()} {coin} qty={final_qty} @ ${real_entry:.6g} "
            f"SL=${sl_price:.6g} TP=${tp_price:.6g} lev={leverage}x "
            f"conf={confidence:.2f} R:R={real_rr:.2f} risk=${real_risk_usd:.2f} "
            f"notional=${real_notional:.2f}"
        ),
    )


def _resolve_real_open(
    client: AiArenaBybitClient,
    bybit_symbol: str,
    side: str,
    requested_qty: float,
    *,
    attempts: int = 3,
    delay_sec: float = 0.4,
) -> tuple[float | None, float | None]:
    """Запрашивает Bybit `get_positions(symbol)` и возвращает
    ``(avg_entry_price, real_qty)`` для только что открытой позиции.

    Bybit V5 fill пишется в `position.avgPrice` асинхронно (обычно
    в течение 100-300 мс после market order). Делаем 3 попытки с
    400 мс паузой; если за ~1.2 сек не появилось — возвращаем
    ``(None, None)`` (caller fallback'ится на ticker).
    """
    for _ in range(max(1, attempts)):
        positions = client.get_positions(symbol=bybit_symbol)
        if positions:
            for p in positions:
                if (
                    p.side == side
                    and p.size > 0
                    and abs(p.size - requested_qty) <= max(requested_qty * 1e-3, 1e-8)
                    and p.entry_price > 0
                ):
                    return p.entry_price, p.size
        time.sleep(delay_sec)
    return None, None
