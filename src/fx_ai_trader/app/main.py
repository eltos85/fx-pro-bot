"""FX AI Trader main loop — dual-timer (15 мин full + 5 мин review).

Архитектура повторяет ai_trader/app/main.py с поправками:
- cTrader через CTraderFxAdapter (вместо Bybit)
- paper-mode reconcile через M1-bars (broker не отрабатывает SL/TP)
- multi-dim sentiment extraction в decisions audit
- gold + oil в одном агенте, label-based broker-side изоляция

Запуск: ``python -m fx_ai_trader`` или ``fx-ai-trader`` (entry-point).
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime
from typing import Any

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.llm.client import DeepSeekClient
from fx_ai_trader.llm.prompts import (
    SYSTEM_PROMPT,
    build_system_prompt_review,
    build_user_prompt,
    build_user_prompt_review,
    format_performance_by_symbol,
    format_recent_trades,
)
from fx_ai_trader.news.eia import EiaProvider
from fx_ai_trader.news.rss import CommodityRssNewsProvider
from fx_ai_trader.news.weather import NoaaOutlookProvider
from fx_ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter
from fx_ai_trader.trading.context import (
    collect_market_context,
    collect_review_context,
    format_context_for_prompt,
    format_context_for_review,
)
from fx_ai_trader.trading.broker_reconcile import reconcile_broker_positions
from fx_ai_trader.trading.executor import apply_action, parse_action
from fx_ai_trader.trading.paper_reconcile import reconcile_paper_positions

log = logging.getLogger("fx_ai_trader")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _extract_sentiment(parsed_raw: dict[str, Any]) -> dict[str, Any] | None:
    """Извлекает sentiment-блок из parsed JSON для audit-log."""
    s = parsed_raw.get("sentiment")
    if isinstance(s, dict):
        return s
    return None


def _extract_thesis(parsed_raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """Извлекает (thesis_status, thesis_invalidator) для audit-log.

    Phase 1 persistent-thesis (2026-05-26): поля присутствуют только на
    close-actions, NULL для open/hold/parse-errors. Soft-валидация в
    `executor._log_thesis_audit` уже отработала к моменту вызова —
    здесь только persist в БД.
    """
    status = parsed_raw.get("thesis_status")
    inv = parsed_raw.get("thesis_invalidator")
    return (
        status if isinstance(status, str) else None,
        inv if isinstance(inv, str) else None,
    )


def run() -> None:
    settings = AiFxTraderSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("=" * 60)
    log.info("FX AI Trader Phase 1 (gold XAUUSD + oil BRENT, paper-mode)")
    log.info("Symbols: %s | Mode: %s",
             ", ".join(settings.symbols),
             "LIVE" if settings.trading_enabled else "PAPER")
    log.info("Full poll: %ds | Review poll: %ds",
             settings.poll_interval_sec, settings.review_interval_sec)
    log.info(
        "Killswitch v1.0 (broker-safety only): daily=$%.0f total=$%.0f "
        "maxpos=%d maxpos/sym=%d max_lot=%.2f "
        "[R:R/risk/correlation сняты — LLM решает сам]",
        settings.max_daily_loss_usd, settings.max_total_loss_usd,
        settings.max_open_positions, settings.max_positions_per_symbol,
        settings.max_lot_size,
    )
    log.info("Order label: %s", settings.order_label)
    log.info("News (RSS): %s | EIA: %s",
             "ON" if settings.news_enabled else "OFF",
             "ON" if settings.eia_api_key else "OFF")
    log.info("=" * 60)

    if not settings.deepseek_api_key:
        log.error("DEEPSEEK_API_KEY не задан, выход")
        return
    if not settings.ctrader_client_id or not settings.ctrader_client_secret:
        log.error("CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET не заданы, выход")
        return

    store = AiFxTraderStore(settings.db_path)

    adapter = CTraderFxAdapter(settings)
    try:
        adapter.start(timeout=30.0)
    except Exception:
        log.exception(
            "CTraderFxAdapter.start() failed — exiting. Проверьте токены: "
            "fx-pro-auth и наличие %s",
            settings.ctrader_token_path,
        )
        return
    if not adapter.is_ready:
        log.error("Adapter не готов после start(), exit")
        adapter.stop()
        return

    # Token-status log на старте: видимость в `docker logs` про сколько
    # дней до expiration. WARNING при <7d, ERROR при expired (детали в
    # auth.log_token_status). Часть «защиты от просрочки» 2026-05-12.
    try:
        from fx_pro_bot.trading.auth import log_token_status
        from fx_ai_trader.trading.token_lock import _read_token  # noqa: PLC2701
        from pathlib import Path as _Path
        _tok = _read_token(_Path(settings.ctrader_token_path))
        log_token_status(_tok, label="FX-AI-Trader cTrader", logger=log)
    except Exception:
        log.debug("Token status log skipped", exc_info=True)

    llm = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=settings.deepseek_max_tokens,
        thinking_enabled=settings.deepseek_thinking_enabled,
        effort=(settings.deepseek_effort or None),
    )

    killswitch = KillSwitch(
        KillSwitchConfig(
            max_daily_loss_usd=settings.max_daily_loss_usd,
            max_total_loss_usd=settings.max_total_loss_usd,
            max_open_positions=settings.max_open_positions,
            max_positions_per_symbol=settings.max_positions_per_symbol,
            per_symbol_max_positions=dict(settings.per_symbol_max_positions),
        ),
        store,
    )

    news_provider: CommodityRssNewsProvider | None = None
    if settings.news_enabled:
        news_provider = CommodityRssNewsProvider(
            cache_ttl_sec=600,
            max_items_per_symbol=settings.news_max_items_per_symbol,
            max_age_hours=settings.news_max_age_hours,
        )
    eia_provider = EiaProvider(
        api_key=settings.eia_api_key,
        cache_ttl_sec=settings.eia_cache_ttl_sec,
    )
    # NOAA CPC outlook нужен ТОЛЬКО если в symbols есть NG=F. Включаем
    # только в этом случае, чтобы не тратить HTTP-запросы.
    noaa_provider: NoaaOutlookProvider | None = None
    if "NG=F" in settings.symbols:
        noaa_provider = NoaaOutlookProvider(cache_ttl_sec=21600)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    last_full_ts = 0.0
    last_review_ts = 0.0
    review_enabled = settings.review_interval_sec > 0

    while not _shutdown:
        now_mono = time.monotonic()

        if last_full_ts == 0.0 or (now_mono - last_full_ts) >= settings.poll_interval_sec:
            cycle += 1
            try:
                _run_full_cycle(
                    cycle, settings, store, adapter, llm, killswitch,
                    news_provider, eia_provider, noaa_provider,
                )
            except Exception:
                log.exception("Full cycle %d crashed (продолжаю)", cycle)
            last_full_ts = time.monotonic()
            last_review_ts = last_full_ts
        elif review_enabled and (now_mono - last_review_ts) >= settings.review_interval_sec:
            cycle += 1
            try:
                _run_review_cycle(
                    cycle, settings, store, adapter, llm, killswitch,
                )
            except Exception:
                log.exception("Review cycle %d crashed (продолжаю)", cycle)
            last_review_ts = time.monotonic()

        time.sleep(1)

    adapter.stop()
    log.info("FX AI Trader остановлен")


def _run_full_cycle(
    cycle: int,
    settings: AiFxTraderSettings,
    store: AiFxTraderStore,
    adapter: CTraderFxAdapter,
    llm: DeepSeekClient,
    killswitch: KillSwitch,
    news_provider: CommodityRssNewsProvider | None,
    eia_provider: EiaProvider,
    noaa_provider: NoaaOutlookProvider | None,
) -> None:
    log.info("─── Full cycle %d @ %s ───", cycle, datetime.now(tz=UTC).isoformat())

    # 1a. Paper-reconcile (SL/TP hit detection через M1) — для paper-позиций.
    closed = reconcile_paper_positions(adapter, store)
    if closed:
        log.info("Paper reconcile: закрыто %d позиций", closed)

    # 1b. Broker-reconcile (SL/TP сработавшие на cTrader стороне) — для live.
    closed_live = reconcile_broker_positions(adapter, store)
    if closed_live:
        log.info("Broker reconcile: закрыто %d live-позиций", closed_live)

    if store.is_paused():
        log.info("PAUSED — пропускаю цикл")
        return

    gen = killswitch.check_can_trade()
    if not gen.allowed:
        log.warning("KILLSWITCH: %s — пропускаю цикл", gen.reason)
        return

    ctx = collect_market_context(
        adapter, store, settings.symbols, settings.virtual_capital_usd,
        news_provider=news_provider, eia_provider=eia_provider,
        noaa_provider=noaa_provider,
    )
    # v1.X self-reflection (2026-05-26): per-symbol perf + последние
    # closed live trades в USER_PROMPT. Источник правды: store.
    # См. BUILDLOG_AI_FX_TRADER.md v1.X запись и плановый файл.
    symbol_stats = store.get_pnl_by_symbol(settings.symbols)
    recent_trades = store.get_recent_closed_trades(limit=10)
    user_prompt = build_user_prompt(
        format_context_for_prompt(ctx),
        performance_by_symbol=format_performance_by_symbol(symbol_stats),
        recent_trades=format_recent_trades(recent_trades),
    )

    log.info(
        "LLM call (full): positions=%d news_total=%d macro_symbols=%s "
        "self_reflection=closed_trades:%d",
        len(ctx.open_positions),
        sum(len(v) for v in ctx.news_per_symbol.values()),
        ",".join(sorted(ctx.macro_per_symbol.keys())) or "none",
        len(recent_trades),
    )
    resp = llm.ask(SYSTEM_PROMPT, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle, cycle_type="full",
            prompt_system=SYSTEM_PROMPT, prompt_user=user_prompt,
            response_raw=None, parsed_action=None, sentiment=None,
            executed=False, error=f"llm_error: {resp.error}",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("LLM error: %s", resp.error)
        return

    log.info(
        "LLM tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
    )
    # Truncation guard: если out впритык к max_tokens — JSON скорее всего
    # обрезан. Не парсим broken-payload, поднимаем явный WARNING,
    # чтобы регрессия лимита была видна в logs (см. инцидент 2026-05-18
    # «not a decision dict (missing 'action')» из-за дефолта 4096).
    if resp.tokens_output >= settings.deepseek_max_tokens - 16:
        log.warning(
            "LLM truncated: out=%d ≈ max_tokens=%d. JSON вероятно обрезан, "
            "skipping cycle. Поднимите AI_FX_TRADER_DEEPSEEK_MAX_TOKENS.",
            resp.tokens_output, settings.deepseek_max_tokens,
        )
        store.log_decision(
            cycle=cycle, cycle_type="full",
            prompt_system=SYSTEM_PROMPT, prompt_user=user_prompt,
            response_raw=resp.text, parsed_action=None, sentiment=None,
            executed=False, error="llm_truncated_at_max_tokens",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        return
    log.info("LLM response: %s", resp.text[:300].replace("\n", " "))

    parsed = parse_action(resp.text, settings.symbols)
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle, cycle_type="full",
            prompt_system=SYSTEM_PROMPT, prompt_user=user_prompt,
            response_raw=resp.text, parsed_action=None, sentiment=None,
            executed=False, error=f"parse_error: {parsed}",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Parse error: %s", parsed)
        return

    sentiment = _extract_sentiment(parsed.raw)
    thesis_status, thesis_invalidator = _extract_thesis(parsed.raw)
    apply = apply_action(
        parsed, adapter=adapter, store=store,
        settings=settings, killswitch=killswitch,
    )
    store.log_decision(
        cycle=cycle, cycle_type="full",
        prompt_system=SYSTEM_PROMPT, prompt_user=user_prompt,
        response_raw=resp.text, parsed_action=parsed.raw, sentiment=sentiment,
        executed=apply.executed, error=apply.error,
        tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
        cost_usd=resp.cost_usd,
        thesis_status=thesis_status,
        thesis_invalidator=thesis_invalidator,
    )
    if apply.error:
        log.error("Apply error: %s", apply.error)
    elif apply.summary:
        log.info("APPLY: %s", apply.summary)


def _run_review_cycle(
    cycle: int,
    settings: AiFxTraderSettings,
    store: AiFxTraderStore,
    adapter: CTraderFxAdapter,
    llm: DeepSeekClient,
    killswitch: KillSwitch,
) -> None:
    log.info("─── Review %d @ %s ───", cycle, datetime.now(tz=UTC).isoformat())

    closed = reconcile_paper_positions(adapter, store)
    if closed:
        log.info("Paper reconcile (review): закрыто %d позиций", closed)

    closed_live = reconcile_broker_positions(adapter, store)
    if closed_live:
        log.info("Broker reconcile (review): закрыто %d live-позиций", closed_live)

    if store.is_paused():
        log.info("PAUSED — пропускаю review")
        return

    gen = killswitch.check_can_trade()
    if not gen.allowed:
        log.info("KILLSWITCH (%s) — пропускаю review", gen.reason)
        return

    open_positions = store.get_open_positions()
    if not open_positions:
        log.info("Нет открытых позиций — пропускаю review")
        return

    ctx = collect_review_context(adapter, store, settings.virtual_capital_usd)
    system_prompt = build_system_prompt_review(settings)
    # v1.X self-reflection (review variant): только per-symbol агрегаты
    # (без recent_trades — review должен оставаться lightweight, см.
    # SYSTEM_PROMPT_REVIEW «NO macro feed, NO news, NO EIA, NO 4H bars»).
    symbol_stats = store.get_pnl_by_symbol(settings.symbols)
    user_prompt = build_user_prompt_review(
        format_context_for_review(ctx),
        performance_by_symbol=format_performance_by_symbol(symbol_stats),
    )

    log.info("Review LLM call: positions=%d", len(ctx.open_positions))
    resp = llm.ask(system_prompt, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle, cycle_type="review",
            prompt_system=system_prompt, prompt_user=user_prompt,
            response_raw=None, parsed_action=None, sentiment=None,
            executed=False, error=f"llm_error: {resp.error}",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Review LLM error: %s", resp.error)
        return

    log.info(
        "Review tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
    )
    if resp.tokens_output >= settings.deepseek_max_tokens - 16:
        log.warning(
            "Review LLM truncated: out=%d ≈ max_tokens=%d, skipping cycle",
            resp.tokens_output, settings.deepseek_max_tokens,
        )
        store.log_decision(
            cycle=cycle, cycle_type="review",
            prompt_system=system_prompt, prompt_user=user_prompt,
            response_raw=resp.text, parsed_action=None, sentiment=None,
            executed=False, error="llm_truncated_at_max_tokens",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        return
    log.info("Review response: %s", resp.text[:200].replace("\n", " "))

    parsed = parse_action(resp.text, settings.symbols, review_mode=True)
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle, cycle_type="review",
            prompt_system=system_prompt, prompt_user=user_prompt,
            response_raw=resp.text, parsed_action=None, sentiment=None,
            executed=False, error=f"parse_error: {parsed}",
            tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Review parse error: %s", parsed)
        return

    thesis_status, thesis_invalidator = _extract_thesis(parsed.raw)
    apply = apply_action(
        parsed, adapter=adapter, store=store,
        settings=settings, killswitch=killswitch,
    )
    store.log_decision(
        cycle=cycle, cycle_type="review",
        prompt_system=system_prompt, prompt_user=user_prompt,
        response_raw=resp.text, parsed_action=parsed.raw, sentiment=None,
        executed=apply.executed, error=apply.error,
        tokens_input=resp.tokens_input, tokens_output=resp.tokens_output,
        cost_usd=resp.cost_usd,
        thesis_status=thesis_status,
        thesis_invalidator=thesis_invalidator,
    )
    if apply.error:
        log.error("Review apply error: %s", apply.error)
    elif apply.summary:
        log.info("REVIEW APPLY: %s", apply.summary)


if __name__ == "__main__":
    run()
