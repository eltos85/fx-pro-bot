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
from fx_ai_trader.data.cot import CotProvider
from fx_ai_trader.data.econ_calendar import EconCalendarProvider
from fx_ai_trader.data.macro_rates import MacroRatesProvider
from fx_ai_trader.data.risk_regime import RiskRegimeProvider
from fx_ai_trader.llm.client import DeepSeekClient
from fx_ai_trader.llm.prompts import (
    SYSTEM_PROMPT,
    build_system_prompt_review,
    build_user_prompt,
    build_user_prompt_review,
    format_event_trigger,
    format_performance_by_symbol,
    format_performance_by_symbol_side,
    format_recent_trades,
)
from fx_ai_trader.news.eia import EiaProvider
from fx_ai_trader.news.gdelt import GdeltProvider
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
from fx_ai_trader.trading.price_sensor import (
    AdverseMoveSensor,
    EntryBreakoutSensor,
    EventDecision,
    LockedProfitSensor,
    compute_unrealised_r,
)

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

    # Phase 1 (2026-05-29): подписка на live spot-стрим. get_current_price
    # далее использует реальную цену вместо H1-close. Graceful — при сбое
    # фолбэк на M1-close сохраняется.
    adapter.subscribe_live_prices()

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
    # Macro rates (DXY / UST10Y / TIP) — BUILDLOG 2026-05-27 D1.
    # Включаем по дефолту; env-flag для отключения в тестах / при
    # rate-limit issues yfinance. yfinance без API-ключа, retry-loop
    # внутри библиотеки.
    macro_rates_provider: MacroRatesProvider | None = None
    if settings.macro_rates_enabled:
        macro_rates_provider = MacroRatesProvider(
            cache_ttl_sec=settings.macro_rates_cache_ttl_sec,
            fred_api_key=settings.fred_api_key,
        )
    # Risk regime (VIX) — Enhancement C (2026-05-29). yfinance ^VIX, no key.
    risk_regime_provider: RiskRegimeProvider | None = None
    if settings.risk_regime_enabled:
        risk_regime_provider = RiskRegimeProvider(
            cache_ttl_sec=settings.risk_regime_cache_ttl_sec,
        )
    # CFTC COT — Enhancement A (2026-05-29). Public API, no key, weekly.
    cot_provider: CotProvider | None = None
    if settings.cot_enabled:
        cot_provider = CotProvider(cache_ttl_sec=settings.cot_cache_ttl_sec)
    # GDELT news tone — Enhancement D (2026-05-29). Public API, no key.
    gdelt_provider: GdeltProvider | None = None
    if settings.gdelt_enabled:
        gdelt_provider = GdeltProvider(cache_ttl_sec=settings.gdelt_cache_ttl_sec)
    # Economic calendar — Enhancement E (2026-05-29). Pure-compute, no net.
    econ_calendar_provider: EconCalendarProvider | None = None
    if settings.econ_calendar_enabled:
        econ_calendar_provider = EconCalendarProvider(
            horizon_hours=settings.econ_calendar_horizon_hours,
        )

    log.info(
        "Data feeds: macro_rates=%s (FRED=%s) | VIX=%s | COT=%s | "
        "GDELT=%s | econ-cal=%s",
        "on" if macro_rates_provider else "off",
        "on" if settings.fred_api_key else "off(TIP-proxy)",
        "on" if risk_regime_provider else "off",
        "on" if cot_provider else "off",
        "on" if gdelt_provider else "off",
        "on" if econ_calendar_provider else "off",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    last_full_ts = 0.0
    last_review_ts = 0.0
    last_sensor_ts = 0.0
    review_enabled = settings.review_interval_sec > 0

    # Phase 2 (2026-05-29): event-driven locked-profit датчик. Активен
    # только если включён review + live price (датчик читает живую цену
    # из spot-кэша). Будит внеплановый review при входе позиции в зону
    # ≥ threshold_r. См. price_sensor.py.
    event_sensor: LockedProfitSensor | None = None
    if (
        settings.event_review_enabled
        and review_enabled
        and settings.live_price_enabled
    ):
        event_sensor = LockedProfitSensor(
            threshold_r=settings.event_review_threshold_r,
            hysteresis_r=settings.event_review_hysteresis_r,
            cooldown_sec=float(settings.event_review_cooldown_sec),
            max_events_per_hour=settings.event_review_max_per_hour,
        )
        log.info(
            "Event-review датчик ON: threshold=%.2fR hysteresis=%.2fR "
            "cooldown=%ds interval=%ds max/h=%d",
            settings.event_review_threshold_r,
            settings.event_review_hysteresis_r,
            settings.event_review_cooldown_sec,
            settings.event_review_sensor_interval_sec,
            settings.event_review_max_per_hour,
        )

    # Phase 3 (2026-05-29): event-driven FULL-цикл. Будит аналитика по
    # событиям — пробой Donchian (entry) и движение против позиции
    # (adverse). Активны только при live price. См. price_sensor.py.
    entry_sensor: EntryBreakoutSensor | None = None
    adverse_sensor: AdverseMoveSensor | None = None
    if settings.event_full_enabled and settings.live_price_enabled:
        if settings.entry_breakout_enabled:
            entry_sensor = EntryBreakoutSensor(
                buffer_atr=settings.entry_breakout_buffer_atr,
                cooldown_sec=float(settings.entry_breakout_cooldown_sec),
                max_events_per_hour=settings.entry_breakout_max_per_hour,
            )
        if settings.adverse_move_enabled:
            adverse_sensor = AdverseMoveSensor(
                threshold_r=settings.adverse_move_threshold_r,
                hysteresis_r=settings.adverse_move_hysteresis_r,
                cooldown_sec=float(settings.adverse_move_cooldown_sec),
                max_events_per_hour=settings.adverse_move_max_per_hour,
            )
        log.info(
            "Event-full датчики ON: entry-breakout=%s (Donchian-%d, buf=%.2fATR, "
            "max/h=%d) | adverse-move=%s (≤−%.2fR, max/h=%d)",
            "on" if entry_sensor else "off",
            settings.entry_breakout_lookback,
            settings.entry_breakout_buffer_atr,
            settings.entry_breakout_max_per_hour,
            "on" if adverse_sensor else "off",
            settings.adverse_move_threshold_r,
            settings.adverse_move_max_per_hour,
        )

    while not _shutdown:
        now_mono = time.monotonic()

        any_sensor = (
            event_sensor is not None
            or entry_sensor is not None
            or adverse_sensor is not None
        )

        if last_full_ts == 0.0 or (now_mono - last_full_ts) >= settings.poll_interval_sec:
            cycle += 1
            try:
                _run_full_cycle(
                    cycle, settings, store, adapter, llm, killswitch,
                    news_provider, eia_provider, noaa_provider,
                    macro_rates_provider,
                    risk_regime_provider=risk_regime_provider,
                    cot_provider=cot_provider,
                    gdelt_provider=gdelt_provider,
                    econ_calendar_provider=econ_calendar_provider,
                    entry_sensor=entry_sensor,
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
        elif any_sensor and (
            now_mono - last_sensor_ts
        ) >= settings.event_review_sensor_interval_sec:
            last_sensor_ts = now_mono
            full_dec, review_dec = _check_event_sensors(
                settings, store, adapter,
                event_sensor=event_sensor,
                entry_sensor=entry_sensor,
                adverse_sensor=adverse_sensor,
            )
            if full_dec.fire:
                cycle += 1
                log.info(
                    "EVENT-FULL trigger: %s", "; ".join(full_dec.triggers)
                )
                try:
                    _run_full_cycle(
                        cycle, settings, store, adapter, llm, killswitch,
                        news_provider, eia_provider, noaa_provider,
                        macro_rates_provider,
                        risk_regime_provider=risk_regime_provider,
                        cot_provider=cot_provider,
                        gdelt_provider=gdelt_provider,
                        econ_calendar_provider=econ_calendar_provider,
                        entry_sensor=entry_sensor,
                        trigger="event", event_triggers=full_dec.triggers,
                    )
                except Exception:
                    log.exception("Event full %d crashed (продолжаю)", cycle)
                last_full_ts = time.monotonic()
                last_review_ts = last_full_ts
            elif review_dec.fire:
                cycle += 1
                log.info(
                    "EVENT-REVIEW trigger (locked-profit zone): %s",
                    ", ".join(review_dec.triggers),
                )
                try:
                    _run_review_cycle(
                        cycle, settings, store, adapter, llm, killswitch,
                        trigger="event", event_triggers=review_dec.triggers,
                    )
                except Exception:
                    log.exception("Event review %d crashed (продолжаю)", cycle)
                last_review_ts = time.monotonic()

        time.sleep(1)

    adapter.stop()
    log.info("FX AI Trader остановлен")


def _check_event_sensors(
    settings: AiFxTraderSettings,
    store: AiFxTraderStore,
    adapter: CTraderFxAdapter,
    *,
    event_sensor: LockedProfitSensor | None,
    entry_sensor: EntryBreakoutSensor | None,
    adverse_sensor: AdverseMoveSensor | None,
) -> tuple[EventDecision, EventDecision]:
    """Опросить все event-датчики (без API: локальная БД + spot-кэш).

    Возвращает (full_decision, review_decision):
    - full_decision.fire → нужен внеплановый FULL-цикл (entry-breakout
      и/или adverse-move). FULL приоритетнее review (делает всё + macro).
    - review_decision.fire → нужен внеплановый REVIEW (locked-profit).

    Все цены берутся из in-memory spot-кэша (``get_live_spot_mid`` —
    БЕЗ фолбэка на trendbars), поэтому опрос бесплатен по API.
    """
    positions = store.get_open_positions()
    pos_r: list[tuple[int, float | None]] = []
    for p in positions:
        price = adapter.get_live_spot_mid(p.symbol)
        r = compute_unrealised_r(p.side, p.entry_price, p.sl_price, price)
        pos_r.append((p.id, r))

    # ── FULL-cycle события (Phase 3): adverse-move + entry-breakout ──
    full_triggers: list[str] = []
    if adverse_sensor is not None:
        adv = adverse_sensor.evaluate(pos_r)
        if adv.fire:
            full_triggers.extend(adv.triggers)
    if entry_sensor is not None:
        live_prices = {
            sym: adapter.get_live_spot_mid(sym) for sym in settings.symbols
        }
        slots_free = len(positions) < settings.max_open_positions
        ent = entry_sensor.evaluate(live_prices, slots_free)
        if ent.fire:
            full_triggers.extend(ent.triggers)
    if full_triggers:
        return EventDecision(fire=True, triggers=full_triggers), EventDecision(fire=False)

    # ── REVIEW-cycle событие (Phase 2): locked-profit ──
    if event_sensor is not None:
        review_dec = event_sensor.evaluate(pos_r)
        if review_dec.fire:
            return EventDecision(fire=False), review_dec

    return EventDecision(fire=False), EventDecision(fire=False)


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
    macro_rates_provider: MacroRatesProvider | None,
    *,
    risk_regime_provider: RiskRegimeProvider | None = None,
    cot_provider: CotProvider | None = None,
    gdelt_provider: GdeltProvider | None = None,
    econ_calendar_provider: EconCalendarProvider | None = None,
    entry_sensor: EntryBreakoutSensor | None = None,
    trigger: str = "scheduled",
    event_triggers: list[str] | None = None,
) -> None:
    log.info(
        "─── Full cycle %d (%s) @ %s ───",
        cycle, trigger, datetime.now(tz=UTC).isoformat(),
    )

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
        macro_rates_provider=macro_rates_provider,
        risk_regime_provider=risk_regime_provider,
        cot_provider=cot_provider,
        gdelt_provider=gdelt_provider,
        econ_calendar_provider=econ_calendar_provider,
    )

    # Диагностика фидов (2026-05-29): какие data-блоки реально попали в
    # контекст этого цикла. Подтверждает, что FRED/VIX/COT/GDELT/calendar
    # дошли до промпта (а не молча отвалились). Не торговая логика.
    mr = ctx.macro_rates_block or ""
    log.info(
        "Context feeds: macro_rates=%s%s | VIX=%s | COT=%s | GDELT=%s | "
        "calendar=%s | news=%d",
        "Y" if mr else "N",
        " (real-yield)" if "REAL YIELD" in mr else (" (TIP-proxy)" if mr else ""),
        "Y" if ctx.risk_regime_block else "N",
        "Y" if ctx.cot_block else "N",
        "Y" if ctx.gdelt_block else "N",
        "Y" if ctx.econ_calendar_block else "N",
        sum(len(v) for v in ctx.news_per_symbol.values()),
    )

    # Phase 3: обновить Donchian-референс датчика входа из уже добытых
    # 1H-баров (бесплатно — full-цикл их и так тянет). Датчик далее
    # сравнивает живую цену с этими уровнями БЕЗ API-вызовов.
    if entry_sensor is not None:
        lookback = settings.entry_breakout_lookback
        for s in ctx.snapshots:
            if len(s.bars_1h) >= lookback:
                recent = s.bars_1h[-lookback:]
                hi = max(b.high for b in recent)
                lo = min(b.low for b in recent)
                atr = s.ind_1h.atr14 if s.ind_1h else None
                entry_sensor.update_reference(s.symbol, hi, lo, atr)
    # v1.X self-reflection (2026-05-26): per-symbol perf + последние
    # closed live trades в USER_PROMPT. Источник правды: store.
    # См. BUILDLOG_AI_FX_TRADER.md v1.X запись и плановый файл.
    # v1.Y COLD-START (2026-05-28): дополнительно per-(symbol × side)
    # split, чтобы LLM явно видел untested направления и мог
    # применить COLD-START DISCOVERY RULE (Sutton & Barto 2018 §2.7).
    # v1.Z REGIME CHANGE CUTOFF (2026-05-28): фильтр trades < Phase 1
    # deploy (settings.stats_window_start). Pre-Phase-1 trades — outcome
    # другой стратегии (Lopez de Prado «Advances in Financial ML» 2018
    # ch.7 structural breaks); БД сохраняется, но LLM видит only
    # post-cutoff. См. BUILDLOG 2026-05-28.
    since = settings.stats_window_start or None
    window_label = (
        f"since {since[:10]} regime-change cutoff" if since else None
    )
    symbol_stats = store.get_pnl_by_symbol(settings.symbols, since=since)
    symbol_side_stats = store.get_pnl_by_symbol_side(
        list(settings.symbols), since=since
    )
    recent_trades = store.get_recent_closed_trades(limit=10, since=since)
    user_prompt = build_user_prompt(
        format_context_for_prompt(ctx),
        performance_by_symbol=format_performance_by_symbol(
            symbol_stats, window_label=window_label
        ),
        performance_by_symbol_side=format_performance_by_symbol_side(
            symbol_side_stats, window_label=window_label
        ),
        recent_trades=format_recent_trades(
            recent_trades, window_label=window_label
        ),
        event_trigger=format_event_trigger(event_triggers),
    )

    log.info(
        "LLM call (full): positions=%d news_total=%d macro_symbols=%s "
        "us_rates=%s self_reflection=closed_trades:%d",
        len(ctx.open_positions),
        sum(len(v) for v in ctx.news_per_symbol.values()),
        ",".join(sorted(ctx.macro_per_symbol.keys())) or "none",
        "on" if ctx.macro_rates_block else "off",
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
    *,
    trigger: str = "scheduled",
    event_triggers: list[str] | None = None,
) -> None:
    log.info(
        "─── Review %d (%s) @ %s ───",
        cycle, trigger, datetime.now(tz=UTC).isoformat(),
    )

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
    # v1.Z regime-change cutoff применяется и здесь — consistency с
    # full cycle (settings.stats_window_start).
    since = settings.stats_window_start or None
    window_label = (
        f"since {since[:10]} regime-change cutoff" if since else None
    )
    symbol_stats = store.get_pnl_by_symbol(settings.symbols, since=since)
    user_prompt = build_user_prompt_review(
        format_context_for_review(ctx),
        performance_by_symbol=format_performance_by_symbol(
            symbol_stats, window_label=window_label
        ),
        event_trigger=format_event_trigger(event_triggers),
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
