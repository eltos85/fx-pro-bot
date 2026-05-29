"""AI-Trader main loop (v0.2 — Wave 2+3+4).

Раз в `poll_interval_sec` (default 15 минут):
1. Сверяем закрытые на бирже позиции (наши, по orderLinkId) с БД,
   обновляем PnL. Push в Telegram если позиция закрылась.
2. Killswitch check — если daily/total лимит — спим до следующего цикла.
3. Pause check — если /pause из Telegram — спим, не дёргаем LLM.
4. Собираем market context (с индикаторами 1h/4h и свежими новостями).
5. Спрашиваем DeepSeek-V4.
6. Парсим JSON, валидируем, записываем decision (audit-trail).
7. Применяем действие (open/close/hold). Push в Telegram если open/close.
8. Логируем результат + спим.

Запускается как `python -m ai_trader.app.main` в Docker-контейнере.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime

from ai_trader.config.settings import AiTraderSettings
from ai_trader.data.crypto_macro import CryptoMacroProvider
from ai_trader.data.macro_rates import MacroRatesProvider
from ai_trader.llm.client import DeepSeekClient
from ai_trader.llm.prompts import (
    SYSTEM_PROMPT_REVIEW,
    build_system_prompt,
    build_system_prompt_review,
    build_user_prompt,
    build_user_prompt_review,
)
from ai_trader.news.rss import RssNewsProvider
from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from ai_trader.state.db import AiTraderStore
from ai_trader.telegram.bot import TelegramBot, TelegramConfig, build_command_handlers
from ai_trader.trading.client import AiBybitClient
from ai_trader.trading.context import (
    collect_market_context,
    collect_review_context,
    format_context_for_prompt,
    format_context_for_review,
)
from ai_trader.trading.executor import apply_action, parse_action
from ai_trader.trading.funding_reconcile import fetch_position_funding
from ai_trader.trading.pnl_reconcile import fetch_net_pnl
from ai_trader.trading.price_sensor import (
    AdverseMoveSensor,
    EntryBreakoutSensor,
    EventDecision,
    LockedProfitSensor,
    compute_unrealised_r,
)
from ai_trader.trading.price_stream import BybitPriceStream

log = logging.getLogger("ai_trader")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _reconcile_closed_positions(
    client: AiBybitClient, store: AiTraderStore, tg: TelegramBot | None = None
) -> None:
    """Если SL/TP закрыли позицию на бирже — обновим её в БД + push в TG.

    Защита от false-close при transient outage биржи (DNS / network /
    non-zero retCode). Инцидент 2026-05-07: на VPS 30 минут отказывал DNS,
    `get_positions` возвращал [] (молча), reconcile помечал реально
    открытую позицию как closed, в БД появлялась exit_price=entry_price
    и PnL=$0.00 (визитная карточка фейк-клоза). Теперь:
    - `get_positions` возвращает None при API failure → этот символ
      пропускается полностью, ни одна его позиция не помечается closed.
    - `get_ticker` неудача → exit_price нет → позиция тоже НЕ помечается
      closed (вернёмся в следующем цикле, когда биржа отвечает).
    """
    open_db = store.get_open_positions()
    if not open_db:
        return

    # Собираем positions per-symbol. None-маркер означает «API не ответил»,
    # для этого символа reconcile пропускаем целиком.
    api_positions_by_symbol: dict[str, list] = {}
    failed_symbols: set[str] = set()
    for sym in {p.symbol for p in open_db}:
        positions = client.get_positions(symbol=sym)
        if positions is None:
            failed_symbols.add(sym)
            log.warning(
                "RECONCILE skipped for %s: get_positions returned None "
                "(API unavailable, deferring to next cycle)",
                sym,
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
        ticker = client.get_ticker(db_pos.symbol)
        if ticker is None or ticker.last_price <= 0:
            log.warning(
                "RECONCILE deferred for id=%d %s %s: ticker unavailable, "
                "не помечаю closed без цены выхода",
                db_pos.id, db_pos.side, db_pos.symbol,
            )
            continue
        exit_price = ticker.last_price
        if db_pos.side == "Buy":
            pnl = (exit_price - db_pos.entry_price) * db_pos.qty
        else:
            pnl = (db_pos.entry_price - exit_price) * db_pos.qty
        pnl_source = "gross"
        # v0.18: при reconcile через exchange-close (SL/TP / manual)
        # сразу пытаемся синхронизировать с Bybit net-PnL. Это
        # критично для KillSwitch на закрытиях через биржу — там нет
        # path через executor, поэтому без fetch_net_pnl будет gross.
        net = fetch_net_pnl(client, db_pos)
        if net is not None:
            pnl, exit_price = net
            pnl_source = "net"
        store.close_position(
            db_pos.id,
            exit_price=exit_price,
            realized_pnl_usd=pnl,
            close_reason="exchange_closed (SL/TP/manual)",
            pnl_source=pnl_source,
        )

        # v0.21: попытка немедленного funding-sync. Для exchange-close
        # (особенно через SL/TP в течение дня без holding overnight)
        # обычно funding=0 (позиция не пересекла 00/08/16 UTC). Если
        # пересекла — funding появится в transaction-log через 1-2 мин.
        funding_suffix = ""
        try:
            closed_pos = store.get_position_by_link_id(db_pos.order_link_id)
            if closed_pos is not None and closed_pos.closed_at:
                funding = fetch_position_funding(client, closed_pos)
                if funding is not None:
                    store.update_funding(closed_pos.id, funding_usd=funding)
                    if abs(funding) >= 0.005:
                        net_total = pnl + funding
                        funding_suffix = (
                            f"\nFunding: ${funding:+.2f} → "
                            f"NET total: ${net_total:+.2f}"
                        )
        except Exception:
            log.exception(
                "immediate funding fetch failed for id=%d", db_pos.id
            )

        msg = (
            f"id={db_pos.id} {db_pos.side} {db_pos.symbol} qty={db_pos.qty}\n"
            f"entry=${db_pos.entry_price:.6g} exit=${exit_price:.6g}\n"
            f"PnL: ${pnl:+.2f} ({pnl_source}){funding_suffix}\n"
            f"Reason: exchange_closed (SL/TP)"
        )
        log.info("RECONCILE closed: %s", msg.replace("\n", " | "))
        if tg:
            tg.notify_close(msg)


def _reconcile_funding(
    client: AiBybitClient, store: AiTraderStore, *, hours: int = 96
) -> None:
    """v0.21: догонная запись funding_usd для закрытых позиций.

    Funding settlements происходят каждые 8ч (00:00 / 08:00 / 16:00 UTC)
    и НЕ включены в Bybit ``closedPnl`` (которым мы пользуемся в
    ``_reconcile_pnl_to_net``). Это четвёртая утечка от gross к net:
    позиции которые висят дольше 8ч могут иметь до ±0.05% / 8h от
    notional funding-cost (типично 0.01–0.02%).

    Идём по closed-позициям с ``funding_usd IS NULL`` за последние
    ``hours``, через ``get_transaction_log`` (type=SETTLEMENT) собираем
    суммарный funding в окне ``[opened_at..closed_at]`` и пишем в БД
    через ``update_funding`` (она же корректирует ``daily_pnl``).

    Default 96h — funding records появляются обычно в течение 1-2 мин
    после settlement, но запас на случай отказа API при первой попытке.
    Bybit transaction-log хранит данные до 2 лет (см.
    https://bybit-exchange.github.io/docs/v5/account/transaction-log).
    """
    candidates = store.get_positions_missing_funding(hours=hours)
    if not candidates:
        return
    fixed = 0
    nonzero = 0
    for pos in candidates:
        funding = fetch_position_funding(client, pos)
        if funding is None:
            continue
        store.update_funding(pos.id, funding_usd=funding)
        fixed += 1
        if abs(funding) > 1e-9:
            nonzero += 1
            log.info(
                "FUNDING-RECONCILE id=%d %s %s: funding=$%+.4f (was NULL)",
                pos.id, pos.side, pos.symbol, funding,
            )
    if fixed:
        log.info(
            "FUNDING-RECONCILE: synced %d/%d positions (%d with non-zero "
            "funding, last %dh)",
            fixed, len(candidates), nonzero, hours,
        )


def _reconcile_pnl_to_net(
    client: AiBybitClient, store: AiTraderStore, *, hours: int = 24
) -> None:
    """v0.18: догонная синхронизация gross→net для недавно закрытых позиций.

    Запускается один раз на full-cycle. Берёт позиции закрытые за
    последние ``hours`` часов с ``pnl_source != 'net'`` и пытается
    получить точное ``closedPnl`` от Bybit. Если получили — обновляет
    ``realized_pnl_usd`` и ``daily_pnl`` на разницу (``update_pnl_to_net``).

    Это safety-net для случая когда в момент close API был недоступен —
    через 5-15 минут (следующий full-cycle) gross перетекает в net.
    """
    candidates = store.get_recent_closed_gross_positions(hours=hours)
    if not candidates:
        return
    fixed = 0
    for pos in candidates:
        net = fetch_net_pnl(client, pos)
        if net is None:
            continue
        new_pnl, new_exit = net
        old_pnl = float(pos.realized_pnl_usd or 0.0)
        store.update_pnl_to_net(
            pos.id,
            new_realized_pnl_usd=new_pnl,
            new_exit_price=new_exit,
        )
        log.info(
            "PNL-RECONCILE id=%d %s %s: gross=$%+.2f → net=$%+.2f (Δ=$%+.2f)",
            pos.id, pos.side, pos.symbol, old_pnl, new_pnl, new_pnl - old_pnl,
        )
        fixed += 1
    if fixed:
        log.info(
            "PNL-RECONCILE: synced %d/%d gross→net positions (last %dh)",
            fixed, len(candidates), hours,
        )


def run() -> None:
    settings = AiTraderSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("=" * 60)
    log.info("AI-Trader v0.2 запущен (DeepSeek-V4 + indicators + news + telegram)")
    log.info("Demo: %s | Symbols: %s", settings.bybit_demo, ", ".join(settings.symbols))
    log.info(
        "Virtual capital: $%.2f | Full poll: %ds | Review poll: %ds",
        settings.virtual_capital_usd,
        settings.poll_interval_sec,
        settings.review_interval_sec,
    )
    log.info(
        "Killswitch: daily=$%.0f total=$%.0f maxpos=%d maxlev=%dx",
        settings.max_daily_loss_usd, settings.max_total_loss_usd,
        settings.max_open_positions, settings.max_leverage,
    )
    log.info("Trading mode: %s", "LIVE" if settings.trading_enabled else "PAPER (decisions only)")
    log.info("News: %s | Telegram: %s",
             "ON" if settings.news_enabled else "OFF",
             "ON" if (settings.telegram_enabled and settings.telegram_bot_token) else "OFF")
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

    # ─── News ────────────────────────────────────────────────────────────
    news_provider: RssNewsProvider | None = None
    if settings.news_enabled:
        news_provider = RssNewsProvider(
            cache_ttl_sec=600,
            max_items=settings.news_max_items,
            max_age_hours=settings.news_max_age_hours,
        )

    # ─── v0.30: external macro providers ─────────────────────────────────
    macro_rates_provider: MacroRatesProvider | None = None
    if getattr(settings, "macro_rates_enabled", False):
        macro_rates_provider = MacroRatesProvider(
            cache_ttl_sec=getattr(settings, "macro_rates_cache_ttl_sec", 1800),
        )
        log.info("MacroRates provider initialized (DXY + UST10Y via yfinance)")

    crypto_macro_provider: CryptoMacroProvider | None = None
    if getattr(settings, "crypto_macro_enabled", False):
        crypto_macro_provider = CryptoMacroProvider(
            cache_ttl_sec=getattr(settings, "crypto_macro_cache_ttl_sec", 3600),
        )
        log.info("CryptoMacro provider initialized (BTC.D + total cap via CoinGecko /global)")

    log.info(
        "v0.30 features: macro_rates=%s crypto_macro=%s "
        "stats_window=%s uncertainty_block=%.2f",
        "ON" if macro_rates_provider else "OFF",
        "ON" if crypto_macro_provider else "OFF",
        getattr(settings, "stats_window_start", "") or "(legacy: no cutoff)",
        getattr(settings, "news_uncertainty_block_threshold", 0.7),
    )

    # ─── Telegram ────────────────────────────────────────────────────────
    tg: TelegramBot | None = None
    if settings.telegram_enabled and settings.telegram_bot_token:
        tg_cfg = TelegramConfig(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            enabled=True,
        )
        tg = TelegramBot(
            tg_cfg, store, build_command_handlers(store, settings, killswitch)
        )
        tg.start()
        # Welcome message — отправится только если chat_id уже привязан
        tg.send(
            "🚀 *AI-Trader v0.2 started*\n\n"
            f"Mode: `{'LIVE' if settings.trading_enabled else 'PAPER'}`\n"
            f"Symbols: {', '.join(settings.symbols)}\n"
            f"Poll: {settings.poll_interval_sec}s\n\n"
            "Send /help to see commands."
        )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Двойной таймер: full-cycle каждые `poll_interval_sec` секунд +
    # review-cycle каждые `review_interval_sec` секунд между ними.
    # `cycle` общий счётчик — full и review увеличивают его одинаково,
    # в БД (`decisions.cycle`) хранится для audit-trail; различить full
    # и review можно по `prompt_system` (review-промпт начинается с
    # «You are reviewing your existing open Bybit perpetual-futures…»).
    cycle = 0
    last_full_ts = 0.0  # monotonic timestamp последнего full-cycle (0 = ещё не было)
    last_review_ts = 0.0
    last_sensor_ts = 0.0
    review_enabled = settings.review_interval_sec > 0

    # ─── v0.34: event-driven analyst (живой поток цены + датчики) ────────
    price_stream: BybitPriceStream | None = None
    locked_profit_sensor: LockedProfitSensor | None = None
    entry_sensor: EntryBreakoutSensor | None = None
    adverse_sensor: AdverseMoveSensor | None = None
    if settings.event_full_enabled:
        price_stream = BybitPriceStream(
            list(settings.symbols),
            category=settings.bybit_category,
            testnet=False,  # public market-data одинаковы для demo/live
            max_age_sec=float(settings.event_price_max_age_sec),
        )
        price_stream.start()
        # LockedProfit → внеплановый REVIEW (только если review включён).
        if settings.locked_profit_enabled and review_enabled:
            locked_profit_sensor = LockedProfitSensor(
                threshold_r=settings.locked_profit_threshold_r,
                hysteresis_r=settings.locked_profit_hysteresis_r,
                cooldown_sec=float(settings.locked_profit_cooldown_sec),
                max_events_per_hour=settings.locked_profit_max_per_hour,
            )
        # EntryBreakout + AdverseMove → внеплановый FULL.
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
            "v0.34 EVENT-DRIVEN: stream=on interval=%ds | locked-profit=%s "
            "(%.2fR, max/h=%d) | adverse=%s (≤−%.2fR, max/h=%d) | "
            "entry-breakout=%s (Donchian-%d, buf=%.2fATR, max/h=%d)",
            settings.event_sensor_interval_sec,
            "on" if locked_profit_sensor else "off",
            settings.locked_profit_threshold_r, settings.locked_profit_max_per_hour,
            "on" if adverse_sensor else "off",
            settings.adverse_move_threshold_r, settings.adverse_move_max_per_hour,
            "on" if entry_sensor else "off",
            settings.entry_breakout_lookback, settings.entry_breakout_buffer_atr,
            settings.entry_breakout_max_per_hour,
        )

    any_sensor = (
        locked_profit_sensor is not None
        or entry_sensor is not None
        or adverse_sensor is not None
    )

    while not _shutdown:
        now_mono = time.monotonic()
        # full-cycle: первый запуск сразу, дальше — каждые poll_interval_sec
        if last_full_ts == 0.0 or (now_mono - last_full_ts) >= settings.poll_interval_sec:
            cycle += 1
            try:
                _run_cycle(
                    cycle, settings, store, bybit, llm, killswitch,
                    news_provider, tg,
                    macro_rates_provider=macro_rates_provider,
                    crypto_macro_provider=crypto_macro_provider,
                    entry_sensor=entry_sensor,
                )
            except Exception as e:
                log.exception("Cycle %d crashed (продолжаю)", cycle)
                if tg:
                    tg.notify_error(f"cycle {cycle}", str(e))
            last_full_ts = time.monotonic()
            last_review_ts = last_full_ts  # reset review-таймер от свежего full
        elif review_enabled and (now_mono - last_review_ts) >= settings.review_interval_sec:
            cycle += 1
            try:
                _run_review_cycle(cycle, settings, store, bybit, llm, killswitch, tg)
            except Exception as e:
                log.exception("Review %d crashed (продолжаю)", cycle)
                if tg:
                    tg.notify_error(f"review {cycle}", str(e))
            last_review_ts = time.monotonic()
        elif any_sensor and price_stream is not None and (
            now_mono - last_sensor_ts
        ) >= settings.event_sensor_interval_sec:
            last_sensor_ts = now_mono
            full_dec, review_dec = _check_event_sensors(
                settings, store, price_stream,
                locked_profit_sensor=locked_profit_sensor,
                entry_sensor=entry_sensor,
                adverse_sensor=adverse_sensor,
            )
            if full_dec.fire:
                cycle += 1
                log.info("EVENT-FULL trigger: %s", "; ".join(full_dec.triggers))
                try:
                    _run_cycle(
                        cycle, settings, store, bybit, llm, killswitch,
                        news_provider, tg,
                        macro_rates_provider=macro_rates_provider,
                        crypto_macro_provider=crypto_macro_provider,
                        entry_sensor=entry_sensor,
                        trigger="event",
                    )
                except Exception as e:
                    log.exception("Event full %d crashed (продолжаю)", cycle)
                    if tg:
                        tg.notify_error(f"event full {cycle}", str(e))
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
                        cycle, settings, store, bybit, llm, killswitch, tg,
                        trigger="event",
                    )
                except Exception as e:
                    log.exception("Event review %d crashed (продолжаю)", cycle)
                    if tg:
                        tg.notify_error(f"event review {cycle}", str(e))
                last_review_ts = time.monotonic()

        # Спим короткими отрезками (1с) чтобы быстро реагировать на
        # SIGTERM. Между full и review проверяем таймеры каждую секунду.
        time.sleep(1)

    if price_stream is not None:
        price_stream.stop()
    if tg:
        tg.stop()
    log.info("AI-Trader остановлен")


def _check_event_sensors(
    settings: AiTraderSettings,
    store: AiTraderStore,
    price_stream: BybitPriceStream,
    *,
    locked_profit_sensor: LockedProfitSensor | None,
    entry_sensor: EntryBreakoutSensor | None,
    adverse_sensor: AdverseMoveSensor | None,
) -> tuple[EventDecision, EventDecision]:
    """Опросить event-датчики по живому кэшу цены (БЕЗ API-вызовов).

    Возвращает (full_decision, review_decision):
    - full_decision.fire → внеплановый FULL-цикл (adverse-move и/или
      entry-breakout). FULL приоритетнее review (делает всё + macro).
    - review_decision.fire → внеплановый REVIEW (locked-profit).

    Цены берутся из in-memory кэша ``price_stream.get_live_mid`` —
    стейл/обрыв даёт None, и датчики на символе молчат.
    """
    positions = store.get_open_positions()
    pos_r: list[tuple[int, float | None]] = []
    for p in positions:
        price = price_stream.get_live_mid(p.symbol)
        r = compute_unrealised_r(p.side, p.entry_price, p.sl_price, price)
        pos_r.append((p.id, r))

    # ── FULL-cycle события: adverse-move + entry-breakout ──
    full_triggers: list[str] = []
    if adverse_sensor is not None:
        adv = adverse_sensor.evaluate(pos_r)
        if adv.fire:
            full_triggers.extend(adv.triggers)
    if entry_sensor is not None:
        live_prices = {
            sym: price_stream.get_live_mid(sym) for sym in settings.symbols
        }
        slots_free = len(positions) < settings.max_open_positions
        ent = entry_sensor.evaluate(live_prices, slots_free)
        if ent.fire:
            full_triggers.extend(ent.triggers)
    if full_triggers:
        return EventDecision(fire=True, triggers=full_triggers), EventDecision(fire=False)

    # ── REVIEW-cycle событие: locked-profit ──
    if locked_profit_sensor is not None:
        review_dec = locked_profit_sensor.evaluate(pos_r)
        if review_dec.fire:
            return EventDecision(fire=False), review_dec

    return EventDecision(fire=False), EventDecision(fire=False)


def _run_cycle(
    cycle: int,
    settings: AiTraderSettings,
    store: AiTraderStore,
    bybit: AiBybitClient,
    llm: DeepSeekClient,
    killswitch: KillSwitch,
    news_provider: RssNewsProvider | None,
    tg: TelegramBot | None,
    *,
    macro_rates_provider: MacroRatesProvider | None = None,
    crypto_macro_provider: CryptoMacroProvider | None = None,
    entry_sensor: EntryBreakoutSensor | None = None,
    trigger: str = "scheduled",
) -> None:
    log.info(
        "─── Cycle %d (%s) @ %s ───",
        cycle, trigger, datetime.now(tz=UTC).isoformat(),
    )

    _reconcile_closed_positions(bybit, store, tg)
    # v0.18: после reconcile через get_positions догоняем те gross-PnL
    # записи которые в момент close не получили net (API падал и т.п.).
    _reconcile_pnl_to_net(bybit, store)
    # v0.21: после net-PnL синка догоняем funding_usd для закрытых позиций
    # которые висели >= 1 settlement (8ч). Это четвёртая утечка от
    # gross к real net (closedPnl не включает funding).
    _reconcile_funding(bybit, store)

    if store.is_paused():
        log.info("PAUSED (через /pause из Telegram) — пропускаю цикл")
        return

    gen = killswitch.check_can_trade()
    if not gen.allowed:
        log.warning("KILLSWITCH: %s — пропускаю цикл", gen.reason)
        if tg:
            tg.notify_killswitch(gen.reason)
        return

    ctx = collect_market_context(
        bybit,
        store,
        settings.symbols,
        settings.virtual_capital_usd,
        news_provider,
        taker_fee_pct=settings.taker_fee_pct,
        macro_rates_provider=macro_rates_provider,
        crypto_macro_provider=crypto_macro_provider,
        stats_window_start=getattr(settings, "stats_window_start", None) or None,
    )

    # v0.34: обновить Donchian-референс датчика входа из уже добытых
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

    system_prompt = build_system_prompt(settings)
    user_prompt = build_user_prompt(format_context_for_prompt(ctx))

    n_recent = len(ctx.recent_closed_trades)
    log.info(
        "LLM call: positions=%d real_equity=$%.2f news=%d macro_rates=%s "
        "crypto_macro=%s self_reflection=%d_trades_in_window",
        len(ctx.open_positions), ctx.real_equity_usd, len(ctx.news),
        "yes" if ctx.macro_rates_block else "no",
        "yes" if ctx.crypto_macro_block else "no",
        n_recent,
    )
    resp = llm.ask(system_prompt, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle,
            prompt_system=system_prompt,
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
        if tg:
            tg.notify_error("LLM", resp.error)
        return

    log.info(
        "LLM tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
    )
    log.info("LLM response: %s", resp.text[:300].replace("\n", " "))

    parsed = parse_action(
        resp.text,
        settings.symbols,
        risk_usd_cap=settings.virtual_capital_usd * settings.risk_per_trade_pct,
        strict_v030_schema=True,
        news_uncertainty_block=getattr(
            settings, "news_uncertainty_block_threshold", 0.7
        ),
        position_size_cap_usd=getattr(
            settings, "max_position_size_usd", settings.virtual_capital_usd
        ),
    )
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle,
            prompt_system=system_prompt,
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
    decision_id = store.log_decision(
        cycle=cycle,
        prompt_system=system_prompt,
        prompt_user=user_prompt,
        response_raw=resp.text,
        parsed_action=parsed.raw,
        executed=apply.executed,
        error=apply.error,
        tokens_input=resp.tokens_input,
        tokens_output=resp.tokens_output,
        cost_usd=resp.cost_usd,
    )
    # v0.30: audit-trail update в decisions для post-hoc анализа.
    if decision_id and (
        apply.thesis_status is not None
        or apply.thesis_invalidator is not None
    ):
        store.update_decision_thesis(
            decision_id,
            thesis_status=apply.thesis_status,
            thesis_invalidator=apply.thesis_invalidator,
        )
    if decision_id and (
        apply.aggregate_uncertainty is not None
        or apply.sentiment_items_json is not None
    ):
        store.update_decision_sentiment(
            decision_id,
            aggregate_uncertainty=apply.aggregate_uncertainty,
            sentiment_items_json=apply.sentiment_items_json,
            macro_rates_snapshot=ctx.macro_rates_block,
        )
    if apply.error:
        log.error("Apply error: %s", apply.error)
    elif apply.summary:
        log.info("APPLY: %s", apply.summary)
        if tg and apply.executed:
            if parsed.action == "open":
                tg.notify_open(apply.summary)
            elif parsed.action == "close":
                tg.notify_close(apply.summary)


def _run_review_cycle(
    cycle: int,
    settings: AiTraderSettings,
    store: AiTraderStore,
    bybit: AiBybitClient,
    llm: DeepSeekClient,
    killswitch: KillSwitch,
    tg: TelegramBot | None,
    *,
    trigger: str = "scheduled",
) -> None:
    """Lite-цикл review (v0.10, 2026-05-10).

    Запускается между full-cycles. Цель: дать LLM возможность принять
    early-close решение по уже открытым позициям до того как сработает
    биржевой SL. NEW open запрещён (validate в parse_action(review_mode=True)).

    Skip-логика:
    - PAUSE через /pause в Telegram → пропускаем.
    - Killswitch → пропускаем (no trading allowed).
    - Нет открытых позиций → пропускаем (нечего ревьюить).
    """
    log.info(
        "─── Review %d (%s) @ %s ───",
        cycle, trigger, datetime.now(tz=UTC).isoformat(),
    )

    _reconcile_closed_positions(bybit, store, tg)

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

    ctx = collect_review_context(
        bybit,
        store,
        settings.virtual_capital_usd,
        taker_fee_pct=settings.taker_fee_pct,
    )
    system_prompt = build_system_prompt_review(settings)
    user_prompt = build_user_prompt_review(format_context_for_review(ctx))

    log.info("Review LLM call: positions=%d", len(ctx.open_positions))
    resp = llm.ask(system_prompt, user_prompt)
    store.add_api_cost(resp.cost_usd)

    if resp.error:
        store.log_decision(
            cycle=cycle,
            prompt_system=system_prompt,
            prompt_user=user_prompt,
            response_raw=None,
            parsed_action=None,
            executed=False,
            error=f"llm_error: {resp.error}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Review LLM error: %s", resp.error)
        return

    log.info(
        "Review tokens: in=%d out=%d cost=$%.5f",
        resp.tokens_input, resp.tokens_output, resp.cost_usd,
    )
    log.info("Review response: %s", resp.text[:200].replace("\n", " "))

    parsed = parse_action(
        resp.text,
        settings.symbols,
        review_mode=True,
        risk_usd_cap=settings.virtual_capital_usd * settings.risk_per_trade_pct,
        strict_v030_schema=True,
        news_uncertainty_block=getattr(
            settings, "news_uncertainty_block_threshold", 0.7
        ),
        position_size_cap_usd=getattr(
            settings, "max_position_size_usd", settings.virtual_capital_usd
        ),
    )
    if isinstance(parsed, str):
        store.log_decision(
            cycle=cycle,
            prompt_system=system_prompt,
            prompt_user=user_prompt,
            response_raw=resp.text,
            parsed_action=None,
            executed=False,
            error=f"parse_error: {parsed}",
            tokens_input=resp.tokens_input,
            tokens_output=resp.tokens_output,
            cost_usd=resp.cost_usd,
        )
        log.error("Review parse error: %s", parsed)
        return

    apply = apply_action(
        parsed, client=bybit, store=store, settings=settings, killswitch=killswitch
    )
    decision_id = store.log_decision(
        cycle=cycle,
        prompt_system=system_prompt,
        prompt_user=user_prompt,
        response_raw=resp.text,
        parsed_action=parsed.raw,
        executed=apply.executed,
        error=apply.error,
        tokens_input=resp.tokens_input,
        tokens_output=resp.tokens_output,
        cost_usd=resp.cost_usd,
    )
    # v0.30: thesis audit-trail для close-actions из review.
    if decision_id and (
        apply.thesis_status is not None
        or apply.thesis_invalidator is not None
    ):
        store.update_decision_thesis(
            decision_id,
            thesis_status=apply.thesis_status,
            thesis_invalidator=apply.thesis_invalidator,
        )
    if apply.error:
        log.error("Review apply error: %s", apply.error)
    elif apply.summary:
        log.info("REVIEW APPLY: %s", apply.summary)
        if tg and apply.executed and parsed.action == "close":
            tg.notify_close(apply.summary)


if __name__ == "__main__":
    run()
