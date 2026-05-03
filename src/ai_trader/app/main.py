"""AI-Trader main loop.

Раз в `poll_interval_sec` (default 15 минут):
1. Сверяем закрытые на бирже позиции (наши, по orderLinkId) с БД,
   обновляем PnL.
2. Killswitch check — если daily/total лимит — спим до следующего цикла.
3. Собираем market context.
4. Спрашиваем DeepSeek-V4.
5. Парсим JSON, валидируем, записываем decision (audit-trail).
6. Применяем действие (open/close/hold).
7. Логируем результат + спим.

Запускается как `python -m ai_trader.app.main` в Docker-контейнере.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime

from ai_trader.config.settings import AiTraderSettings
from ai_trader.llm.client import DeepSeekClient
from ai_trader.llm.prompts import SYSTEM_PROMPT, build_user_prompt
from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from ai_trader.state.db import AiTraderStore
from ai_trader.trading.client import AiBybitClient
from ai_trader.trading.context import collect_market_context, format_context_for_prompt
from ai_trader.trading.executor import apply_action, parse_action

log = logging.getLogger("ai_trader")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _reconcile_closed_positions(client: AiBybitClient, store: AiTraderStore) -> None:
    """Если SL/TP закрыли позицию на бирже — обновим её в БД."""
    open_db = store.get_open_positions()
    if not open_db:
        return

    api_positions_by_symbol: dict[str, list] = {}
    for sym in {p.symbol for p in open_db}:
        for p in client.get_positions(symbol=sym):
            api_positions_by_symbol.setdefault(p.symbol, []).append(p)

    for db_pos in open_db:
        # Если на бирже нет открытой позиции в этом символе с тем же size+side,
        # считаем закрытой. Берём последний ticker как exit price (грубое
        # приближение; точную цену забора можно достать из get_closed_pnl
        # по orderLinkId, добавим в v0.2).
        api_list = api_positions_by_symbol.get(db_pos.symbol, [])
        still_open = any(
            p.side == db_pos.side and abs(p.size - db_pos.qty) < 1e-6 for p in api_list
        )
        if still_open:
            continue
        ticker = client.get_ticker(db_pos.symbol)
        exit_price = ticker.last_price if ticker else db_pos.entry_price
        if db_pos.side == "Buy":
            pnl = (exit_price - db_pos.entry_price) * db_pos.qty
        else:
            pnl = (db_pos.entry_price - exit_price) * db_pos.qty
        store.close_position(
            db_pos.id,
            exit_price=exit_price,
            realized_pnl_usd=pnl,
            close_reason="exchange_closed (SL/TP/manual)",
        )
        log.info(
            "RECONCILE closed: id=%d %s %s qty=%s entry=$%.6g exit=$%.6g pnl=$%+.2f",
            db_pos.id, db_pos.side, db_pos.symbol, db_pos.qty,
            db_pos.entry_price, exit_price, pnl,
        )


def run() -> None:
    settings = AiTraderSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("=" * 60)
    log.info("AI-Trader запущен (DeepSeek-V4 experiment)")
    log.info("Demo: %s | Symbols: %s", settings.bybit_demo, ", ".join(settings.symbols))
    log.info("Virtual capital: $%.2f | Poll: %ds", settings.virtual_capital_usd, settings.poll_interval_sec)
    log.info(
        "Killswitch: daily=$%.0f total=$%.0f maxpos=%d maxlev=%dx",
        settings.max_daily_loss_usd, settings.max_total_loss_usd,
        settings.max_open_positions, settings.max_leverage,
    )
    log.info("Trading mode: %s", "LIVE" if settings.trading_enabled else "PAPER (decisions only)")
    log.info("=" * 60)

    if not settings.deepseek_api_key:
        log.error("DEEPSEEK_API_KEY не задан, выход")
        return
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        log.error("AI_TRADER_BYBIT_API_KEY/SECRET не заданы, выход")
        return

    store = AiTraderStore(settings.db_path)
    bybit = AiBybitClient(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        demo=settings.bybit_demo,
        category=settings.bybit_category,
    )
    llm = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=settings.deepseek_max_tokens,
        thinking_enabled=settings.deepseek_thinking_enabled,
    )
    killswitch = KillSwitch(
        KillSwitchConfig(
            max_daily_loss_usd=settings.max_daily_loss_usd,
            max_total_loss_usd=settings.max_total_loss_usd,
            max_open_positions=settings.max_open_positions,
            max_leverage=settings.max_leverage,
        ),
        store,
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    while not _shutdown:
        cycle += 1
        try:
            _run_cycle(cycle, settings, store, bybit, llm, killswitch)
        except Exception:
            log.exception("Cycle %d crashed (продолжаю)", cycle)

        # Сон с проверкой shutdown каждую секунду
        for _ in range(settings.poll_interval_sec):
            if _shutdown:
                break
            time.sleep(1)

    log.info("AI-Trader остановлен")


def _run_cycle(
    cycle: int,
    settings: AiTraderSettings,
    store: AiTraderStore,
    bybit: AiBybitClient,
    llm: DeepSeekClient,
    killswitch: KillSwitch,
) -> None:
    log.info("─── Cycle %d @ %s ───", cycle, datetime.now(tz=UTC).isoformat())

    _reconcile_closed_positions(bybit, store)

    gen = killswitch.check_can_trade()
    if not gen.allowed:
        log.warning("KILLSWITCH: %s — пропускаю цикл", gen.reason)
        return

    ctx = collect_market_context(
        bybit, store, settings.symbols, settings.virtual_capital_usd
    )
    user_prompt = build_user_prompt(format_context_for_prompt(ctx))

    log.info(
        "LLM call: positions=%d real_equity=$%.2f",
        len(ctx.open_positions), ctx.real_equity_usd,
    )
    resp = llm.ask(SYSTEM_PROMPT, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle,
            prompt_system=SYSTEM_PROMPT,
            prompt_user=user_prompt,
            response_raw=None,
            parsed_action=None,
            executed=False,
            error=f"llm_error: {resp.error}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("LLM error: %s", resp.error)
        return

    log.info(
        "LLM tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
    )
    log.info("LLM response: %s", resp.text[:300].replace("\n", " "))

    parsed = parse_action(resp.text, settings.symbols)
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle,
            prompt_system=SYSTEM_PROMPT,
            prompt_user=user_prompt,
            response_raw=resp.text,
            parsed_action=None,
            executed=False,
            error=f"parse_error: {parsed}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Parse error: %s", parsed)
        return

    apply = apply_action(
        parsed, client=bybit, store=store, settings=settings, killswitch=killswitch
    )
    store.log_decision(
        cycle=cycle,
        prompt_system=SYSTEM_PROMPT,
        prompt_user=user_prompt,
        response_raw=resp.text,
        parsed_action=parsed.raw,
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


if __name__ == "__main__":
    run()
