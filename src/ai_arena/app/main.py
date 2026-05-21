"""AI Arena main loop — Single 3-min cycle (Nof1 7-node loop).

Архитектура (см. правило `.cursor/rules/ai-arena-sources.mdc` и
AI_TRADER_PROPOSAL_ALPHA_ARENA.md §4.1):

1. Heartbeat trigger    — каждые `poll_interval_sec` (default 180 сек)
2. Account snapshot     — equity, available cash, total return, minutes_elapsed
3. Compute Sharpe       — rolling 14d из equity_snapshots
4. Reconcile positions  — закрытые на бирже (SL/TP/manual) → БД + push
5. Market data acquire  — per-symbol: 3m × 50, 4h × 60, OI 20×5min
6. Build user prompt    — Nof1 layout (per-symbol blocks + account state)
7. LLM call             — DeepSeek V4-Pro, reasoning_effort=off
8. Parse JSON action    — sanity-валидация Nof1 schema (типы + диапазоны)
9. Execute on Bybit    — set_leverage → place_order (или PAPER)
10. Persist decision    — БД с confidence/invalidation/risk_usd/sharpe
12. Telegram notify     — на open/close
13. Equity snapshot     — для следующего Sharpe

ЕДИНЫЙ ТАЙМЕР — никаких dual-cycle (full + review). Nof1 single-agent,
single-cycle.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime

from ai_arena.analysis.sharpe import cumulative_sharpe
from ai_arena.app.scaling import compute_scaled_account
from ai_arena.config.settings import AiArenaSettings
from ai_arena.llm.client import DeepSeekArenaClient
from ai_arena.llm.prompts import build_system_prompt, build_user_prompt
from ai_arena.state.db import AiArenaStore
from ai_arena.telegram.bot import (
    TelegramArenaBot,
    TelegramConfig,
    build_command_handlers,
)
from ai_arena.trading.client import AiArenaBybitClient
from ai_arena.trading.context import (
    collect_market_context,
    format_open_positions_block,
    format_per_symbol_blocks,
)
from ai_arena.trading.executor import apply_action, parse_action
from ai_arena.trading.reconcile import (
    reconcile_closed_positions,
    reconcile_pending_pnl,
)

log = logging.getLogger("ai_arena")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def run() -> None:
    settings = AiArenaSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("=" * 60)
    log.info("AI Arena v0.1 (Nof1 Alpha Arena clone) — DeepSeek %s", settings.deepseek_model)
    log.info(
        "Demo: %s | Symbols: %s | Cycle: %ds | Virtual cap: $%.2f | leverage cap: 1-%dx",
        settings.bybit_demo,
        ", ".join(settings.symbols),
        settings.poll_interval_sec,
        settings.virtual_capital_usd,
        settings.leverage_max,
    )
    log.info(
        "Equity model: offset-based (LLM видит $%.0f + реальный PnL с Bybit)",
        settings.virtual_capital_usd,
    )
    log.info("Mode: %s", "LIVE" if settings.trading_enabled else "PAPER (decisions only)")
    log.info(
        "Telegram: %s",
        "ON" if (settings.telegram_enabled and settings.telegram_bot_token) else "OFF",
    )
    log.info("=" * 60)

    if not settings.deepseek_api_key:
        log.error("DEEPSEEK_API_KEY не задан, выход")
        return
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        log.error(
            "AI_ARENA_BYBIT_API_KEY/SECRET не заданы, выход (ждём токен)"
        )
        return

    store = AiArenaStore(settings.db_path)
    bybit = AiArenaBybitClient(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        demo=settings.bybit_demo,
        category=settings.bybit_category,
    )
    llm = DeepSeekArenaClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=settings.deepseek_max_tokens,
        reasoning_effort=settings.deepseek_reasoning_effort,
    )

    tg: TelegramArenaBot | None = None
    if settings.telegram_enabled and settings.telegram_bot_token:
        tg_cfg = TelegramConfig(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            enabled=True,
        )
        tg = TelegramArenaBot(
            tg_cfg, store, build_command_handlers(store, settings)
        )
        tg.start()
        tg.send(
            "🚀 *AI Arena v0.1 started*\n\n"
            f"Mode: `{'LIVE' if settings.trading_enabled else 'PAPER'}`\n"
            f"Model: `{settings.deepseek_model}` (reasoning={settings.deepseek_reasoning_effort})\n"
            f"Symbols: {', '.join(settings.symbols)}\n"
            f"Cycle: {settings.poll_interval_sec}s\n\n"
            "Send /help to see commands."
        )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    last_cycle_ts = 0.0
    while not _shutdown:
        now_mono = time.monotonic()
        if last_cycle_ts == 0.0 or (now_mono - last_cycle_ts) >= settings.poll_interval_sec:
            cycle += 1
            try:
                _run_cycle(cycle, settings, store, bybit, llm, tg)
            except Exception as e:
                log.exception("Cycle %d crashed (продолжаю)", cycle)
                if tg:
                    tg.notify_error(f"cycle {cycle}", str(e))
            last_cycle_ts = time.monotonic()
        time.sleep(1)

    if tg:
        tg.stop()
    log.info("AI Arena остановлен")


def _run_cycle(
    cycle: int,
    settings: AiArenaSettings,
    store: AiArenaStore,
    bybit: AiArenaBybitClient,
    llm: DeepSeekArenaClient,
    tg: TelegramArenaBot | None,
) -> None:
    log.info("─── Cycle %d @ %s ───", cycle, datetime.now(tz=UTC).isoformat())

    started_ts = store.get_started_at_ts()
    minutes_elapsed = max(
        0, int((datetime.now(tz=UTC).timestamp() - started_ts) // 60)
    )

    reconcile_closed_positions(bybit, store, tg)
    reconcile_pending_pnl(bybit, store, tg)

    # Cumulative Sharpe с момента старта эксперимента (1-в-1 с Nof1
    # Season 1, который идёт cumulative с 17 окт 2025). nullable если
    # истории < 3 snapshot'ов.
    snapshots = store.get_all_equity_snapshots()
    sharpe = cumulative_sharpe(snapshots)

    # Market context
    ctx = collect_market_context(
        bybit, store, settings.symbols, settings.virtual_capital_usd
    )

    # Open positions block: подтягиваем current_price и liquidation_price
    cur_prices: dict[str, float] = {}
    liq_prices: dict[str, float] = {}
    notional: dict[str, float] = {}
    unrealized: dict[str, float] = {}
    api_positions = bybit.get_positions() or []
    for ap in api_positions:
        cur_prices[ap.symbol] = ap.entry_price  # будет перетёрто ниже
        liq_prices[ap.symbol] = ap.liquidation_price
        notional[ap.symbol] = ap.position_value
        unrealized[ap.symbol] = ap.unrealised_pnl
    for blk in ctx.blocks:
        if blk.ticker is not None:
            cur_prices[blk.symbol] = blk.ticker.last_price

    open_pos_block = format_open_positions_block(
        ctx.open_positions,
        current_prices=cur_prices,
        liquidation_prices=liq_prices,
        notional_by_symbol=notional,
        unrealized_by_symbol=unrealized,
    )

    # Offset-based scaling (sandbox $1000 + реальный PnL):
    #   scaled_equity = virtual_capital + (real_now - real_at_start)
    #   scaled_cash   = real_avail - (real_at_start - virtual_capital)
    #
    # `real_at_start` — anchor реального Bybit equity на момент первого
    # цикла, сохраняется в kv_state. Anchor НЕ пересчитывается рестартом
    # контейнера (см. правило deploy-vps.mdc: ai_arena_data сохраняется).
    #
    # Семантика: quantities в LLM-вселенной = quantities на Bybit
    # (исполняются как есть). Реальный PnL в $$ прибавляется к sandbox
    # 1-в-1 (а не делится на divisor — это давало некорректное
    # «+$0.15 за реальную +$7.32 прибыль»).
    real_at_start = _get_or_init_real_anchor(store, ctx.real_equity_usd)
    scaled_equity, scaled_cash, total_return_pct = compute_scaled_account(
        real_equity_now=ctx.real_equity_usd,
        real_at_start=real_at_start,
        real_available_cash=ctx.available_cash_usd,
        virtual_capital_usd=settings.virtual_capital_usd,
    )

    system_prompt = build_system_prompt(settings)
    user_prompt = build_user_prompt(
        minutes_elapsed=minutes_elapsed,
        per_symbol_blocks=format_per_symbol_blocks(ctx),
        total_return_pct=total_return_pct,
        sharpe=sharpe,
        cash=scaled_cash,
        equity=scaled_equity,
        open_positions_block=open_pos_block,
    )

    log.info(
        "LLM call: positions=%d real=$%.2f anchor=$%.2f → sandbox=$%.2f "
        "(PnL %+.2f, %+.2f%%) sharpe=%s minutes=%d",
        len(ctx.open_positions),
        ctx.real_equity_usd,
        real_at_start,
        scaled_equity,
        ctx.real_equity_usd - real_at_start,
        total_return_pct,
        f"{sharpe:.3f}" if sharpe is not None else "n/a",
        minutes_elapsed,
    )
    resp = llm.ask(system_prompt, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle,
            minutes_elapsed=minutes_elapsed,
            sharpe_at_decision=sharpe,
            prompt_system=system_prompt,
            prompt_user=user_prompt,
            response_raw=None,
            parsed_action=None,
            signal=None,
            confidence=None,
            invalidation_condition=None,
            risk_usd=None,
            executed=False,
            error=f"llm_error: {resp.error}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            tokens_cache_hit=resp.tokens_cache_hit,
            tokens_cache_miss=resp.tokens_cache_miss,
            cost_usd=resp.cost_usd,
        )
        log.error("LLM error: %s", resp.error)
        if tg:
            tg.notify_error("LLM", resp.error)
        _save_equity_snapshot(store, scaled_equity=scaled_equity, scaled_cash=scaled_cash, cycle=cycle, sharpe=sharpe, total_return_pct=total_return_pct)
        return

    log.info(
        "LLM tokens: in=%d (hit=%d miss=%d, %.1f%% cache) out=%d cost=$%.5f",
        resp.tokens_input,
        resp.tokens_cache_hit,
        resp.tokens_cache_miss,
        resp.cache_hit_rate * 100,
        resp.tokens_output,
        resp.cost_usd,
    )
    log.info("LLM response (first 300): %s", resp.text[:300].replace("\n", " "))

    parsed = parse_action(resp.text, settings.symbols)
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle,
            minutes_elapsed=minutes_elapsed,
            sharpe_at_decision=sharpe,
            prompt_system=system_prompt,
            prompt_user=user_prompt,
            response_raw=resp.text,
            parsed_action=None,
            signal=None,
            confidence=None,
            invalidation_condition=None,
            risk_usd=None,
            executed=False,
            error=f"parse_error: {parsed}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            tokens_cache_hit=resp.tokens_cache_hit,
            tokens_cache_miss=resp.tokens_cache_miss,
            cost_usd=resp.cost_usd,
        )
        log.error("Parse error: %s", parsed)
        _save_equity_snapshot(store, scaled_equity=scaled_equity, scaled_cash=scaled_cash, cycle=cycle, sharpe=sharpe, total_return_pct=total_return_pct)
        return

    apply = apply_action(
        parsed,
        client=bybit,
        store=store,
        settings=settings,
    )
    store.log_decision(
        cycle=cycle,
        minutes_elapsed=minutes_elapsed,
        sharpe_at_decision=sharpe,
        prompt_system=system_prompt,
        prompt_user=user_prompt,
        response_raw=resp.text,
        parsed_action=parsed.raw,
        signal=parsed.signal,
        confidence=parsed.raw.get("confidence"),
        invalidation_condition=parsed.raw.get("invalidation_condition"),
        risk_usd=parsed.raw.get("risk_usd"),
        executed=apply.executed,
        error=apply.error,
        tokens_input=resp.tokens_input,
        tokens_output=resp.tokens_output,
        tokens_cache_hit=resp.tokens_cache_hit,
        tokens_cache_miss=resp.tokens_cache_miss,
        cost_usd=resp.cost_usd,
    )
    if apply.error:
        log.error("Apply error: %s", apply.error)
    elif apply.summary:
        log.info("APPLY: %s", apply.summary)
        if tg and apply.executed:
            if parsed.signal in {"buy_to_enter", "sell_to_enter"}:
                tg.notify_open(apply.summary)
            elif parsed.signal == "close":
                tg.notify_close(apply.summary)

    _save_equity_snapshot(store, scaled_equity=scaled_equity, scaled_cash=scaled_cash, cycle=cycle, sharpe=sharpe, total_return_pct=total_return_pct)


def _save_equity_snapshot(
    store: AiArenaStore,
    *,
    scaled_equity: float,
    scaled_cash: float,
    cycle: int,
    sharpe: float | None,
    total_return_pct: float,
) -> None:
    """Шаг 13 — equity snapshot **scaled-значений** для cumulative Sharpe.

    Сохраняем sandbox-equity (то что LLM видит), не real Bybit equity.
    Тогда Sharpe и total_return считаются в LLM-вселенной — согласовано
    с тем что модель наблюдает в каждом prompt'е.
    """
    try:
        store.add_equity_snapshot(
            total_equity_usd=scaled_equity,
            available_cash_usd=scaled_cash,
            total_return_pct=total_return_pct,
            sharpe_rolling_14d=sharpe,
            cycle_no=cycle,
        )
    except Exception:
        log.exception("equity snapshot save failed (cycle %d)", cycle)


_REAL_ANCHOR_KEY = "real_equity_at_start_usd"


def _get_or_init_real_anchor(store: AiArenaStore, current_real_equity: float) -> float:
    """Возвращает anchor `real_equity_at_start` из kv_state.

    При первом цикле (anchor отсутствует) — сохраняет current_real_equity
    как anchor. Дальше anchor неизменен (не сбрасывается рестартом
    контейнера, т.к. ai_arena_data volume переживает recreate).

    Это **единственная инфраструктурная адаптация** в формуле equity:
    у source (Hyperliquid) изначальный capital задан декларативно
    ($10k бюджет на model), у нас он анкерится автоматически на
    первом цикле — иначе divisor пришлось бы захардкодить.
    """
    raw = store.kv_get(_REAL_ANCHOR_KEY)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    store.kv_set(_REAL_ANCHOR_KEY, str(current_real_equity))
    log.info(
        "Real-equity anchor зафиксирован: $%.2f (на этой точке LLM видит "
        "Starting Capital в SYSTEM_PROMPT)", current_real_equity,
    )
    return current_real_equity


if __name__ == "__main__":
    run()
