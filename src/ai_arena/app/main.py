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
from ai_arena.trading.executor import (
    _resolve_net_close,
    apply_action,
    parse_action,
)

log = logging.getLogger("ai_arena")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _reconcile_closed_positions(
    client: AiArenaBybitClient,
    store: AiArenaStore,
    tg: TelegramArenaBot | None,
) -> None:
    """Если SL/TP закрыли позицию на бирже — обновим БД + push.

    Защита от false-close при transient outage биржи: если
    `get_positions(symbol)` возвращает None — пропускаем символ
    целиком (не помечаем closed).

    PnL и exit_price берутся из Bybit `get_closed_pnl` (net после
    fees + funding) через ``_resolve_net_close`` — 1-в-1 с биржей.
    Локальный `(exit-entry)*qty` запрещён (см. BUILDLOG 2026-05-15).
    """
    open_db = store.get_open_positions()
    if not open_db:
        return

    api_positions_by_symbol: dict[str, list] = {}
    failed_symbols: set[str] = set()
    for sym in {p.symbol for p in open_db}:
        positions = client.get_positions(symbol=sym)
        if positions is None:
            failed_symbols.add(sym)
            log.warning(
                "RECONCILE skipped for %s: get_positions=None (API outage)", sym
            )
            continue
        api_positions_by_symbol[sym] = list(positions)

    for db_pos in open_db:
        if db_pos.symbol in failed_symbols:
            continue
        api_list = api_positions_by_symbol.get(db_pos.symbol, [])
        still_open = any(
            p.side == db_pos.side and abs(p.size - db_pos.qty) < 1e-6 for p in api_list
        )
        if still_open:
            continue
        exit_price, pnl = _resolve_net_close(
            client=client,
            symbol=db_pos.symbol,
            opened_at_iso=db_pos.opened_at,
            opened_side=db_pos.side,
            qty=db_pos.qty,
            fallback_entry=db_pos.entry_price,
        )
        if pnl == 0.0 and exit_price == db_pos.entry_price:
            log.warning(
                "RECONCILE deferred for id=%d %s %s: closed_pnl unavailable",
                db_pos.id, db_pos.side, db_pos.symbol,
            )
            continue
        store.close_position(
            db_pos.id,
            exit_price=exit_price,
            realized_pnl_usd=pnl,
            close_reason="exchange_closed (SL/TP/manual)",
        )
        msg = (
            f"id={db_pos.id} {db_pos.side} {db_pos.symbol} qty={db_pos.qty}\n"
            f"entry=${db_pos.entry_price:.6g} exit=${exit_price:.6g}\n"
            f"PnL: ${pnl:+.2f} (net of fees)\nReason: exchange_closed"
        )
        log.info("RECONCILE closed: %s", msg.replace("\n", " | "))
        if tg:
            tg.notify_close(msg)


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
        "Equity scale divisor: %.1f (Bybit demo equity → LLM-видимый equity)",
        settings.equity_scale_divisor,
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

    _reconcile_closed_positions(bybit, store, tg)

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

    # Scaling Bybit equity вниз для LLM (demo $50k → /50 → $1000 sandbox).
    # Делитель — `equity_scale_divisor` из settings (см. settings.py).
    # Это единственное обоснованное отклонение от source: Hyperliquid
    # позволяет дать модели $10k бюджет, Bybit demo выдаёт фиксированный
    # $50k — масштабируем чтобы LLM работал в $1000 окне.
    divisor = max(1.0, settings.equity_scale_divisor)
    scaled_equity = ctx.real_equity_usd / divisor
    scaled_cash = ctx.available_cash_usd / divisor

    # total_return_pct — % изменения equity от **самого первого**
    # snapshot'а (с момента старта эксперимента), как `Current Total
    # Return` у Nof1 Season 1. Раньше брали первый snapshot за 14 дней
    # (rolling baseline) — это давало корректный return только первые
    # 14 дней работы, после baseline начинал «скользить». Cumulative
    # baseline — 1-в-1 с source. Формула инвариантна к equity scaling.
    total_return_pct = 0.0
    first_snapshot = store.get_first_equity_snapshot()
    if first_snapshot:
        baseline = float(first_snapshot["total_equity_usd"])
        if baseline > 0:
            total_return_pct = (
                (ctx.real_equity_usd - baseline) / baseline * 100
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
        "LLM call: positions=%d real_equity=$%.2f scaled=$%.2f sharpe=%s minutes=%d",
        len(ctx.open_positions),
        ctx.real_equity_usd,
        scaled_equity,
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
            cost_usd=resp.cost_usd,
        )
        log.error("LLM error: %s", resp.error)
        if tg:
            tg.notify_error("LLM", resp.error)
        _save_equity_snapshot(store, ctx, cycle, sharpe, total_return_pct)
        return

    log.info(
        "LLM tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
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
            cost_usd=resp.cost_usd,
        )
        log.error("Parse error: %s", parsed)
        _save_equity_snapshot(store, ctx, cycle, sharpe, total_return_pct)
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

    _save_equity_snapshot(store, ctx, cycle, sharpe, total_return_pct)


def _save_equity_snapshot(
    store: AiArenaStore,
    ctx,
    cycle: int,
    sharpe: float | None,
    total_return_pct: float,
) -> None:
    """Шаг 13 — equity snapshot для следующего rolling Sharpe."""
    try:
        store.add_equity_snapshot(
            total_equity_usd=ctx.real_equity_usd,
            available_cash_usd=ctx.available_cash_usd,
            total_return_pct=total_return_pct,
            sharpe_rolling_14d=sharpe,
            cycle_no=cycle,
        )
    except Exception:
        log.exception("equity snapshot save failed (cycle %d)", cycle)


if __name__ == "__main__":
    run()
