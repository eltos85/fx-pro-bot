"""Сканер-советник v0.7: ансамбль + Leaders + Outsiders + Shadow + Scalping + cTrader auto-trading."""

from __future__ import annotations

import logging
import time

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.scanner import active_signals, scan_instruments
from fx_pro_bot.analysis.signals import TrendDirection, _atr
from fx_pro_bot.config.settings import SCALPING_EXTRA_SYMBOLS, Settings, display_name, pip_size, pip_value_usd, spread_cost_pips
from fx_pro_bot.copytrading.ctrader import CTraderCopyClient, format_top_strategies
from fx_pro_bot.events import events_near, events_to_json_blob, load_events
from fx_pro_bot.stats.cleanup import cleanup_shadow_log, db_size_mb, vacuum_if_needed
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.stats.verifier import run_verification
from fx_pro_bot.strategies.leaders import LeadersStrategy, aggregate_leader_signals
from fx_pro_bot.strategies.monitor import PositionMonitor
from fx_pro_bot.strategies.outsiders import OutsidersStrategy, detect_extreme_setups
from fx_pro_bot.strategies.shadow import ShadowTracker
from fx_pro_bot.trading.auth import TokenStore, ensure_valid_token
from fx_pro_bot.trading.killswitch import KillSwitch, KillSwitchConfig
from fx_pro_bot.trading.symbols import SymbolCache
from fx_pro_bot.strategies.scalping.session_orb import SessionOrbStrategy
from fx_pro_bot.strategies.scalping.stat_arb import StatArbStrategy
from fx_pro_bot.strategies.scalping.vwap_reversion import VwapReversionStrategy
from fx_pro_bot.whales.cot import fetch_cot_signals
from fx_pro_bot.whales.sentiment import fetch_sentiment_signals
from fx_pro_bot.whales.tracker import WhaleTracker

log = logging.getLogger(__name__)

CTRADER_POLL_CYCLES = 12
WHALE_POLL_CYCLES = 6
CLEANUP_POLL_CYCLES = 288


def _log_stats(store: StatsStore, horizons: tuple[int, ...], settings: Settings) -> None:
    lot = settings.lot_size
    balance = settings.account_balance

    for h in horizons:
        vs = store.verification_summary(h)
        if vs["total"] == 0:
            continue
        log.info(
            "  Горизонт %dм: %d проверок, win-rate %.0f%%, средний %+.1f пунктов, "
            "сумма %+.1f пунктов",
            h, vs["total"], vs["win_rate"] * 100, vs["avg_profit"], vs["total_profit"],
        )

    by_instr = store.verification_summary_by_instrument()
    gross_usd = 0.0
    spread_usd = 0.0
    if by_instr:
        log.info("  По инструментам (лот %.2f):", lot)
        for row in by_instr:
            pips = float(row["total_profit"])
            symbol = str(row["instrument"])
            num_trades = int(row["total"]) // len(horizons) if horizons else int(row["total"])
            pv = pip_value_usd(symbol, lot)
            instr_gross = pips * pv
            instr_spread = spread_cost_pips(symbol) * pv * num_trades
            instr_net = instr_gross - instr_spread
            gross_usd += instr_gross
            spread_usd += instr_spread
            log.info(
                "    %s: %d проверок, win-rate %.0f%%, %+.1f пунктов → "
                "брутто $%+.2f, спред -$%.2f, чистыми $%+.2f",
                display_name(symbol), row["total"],
                row["win_rate"] * 100, pips,  # type: ignore[arg-type]
                instr_gross, instr_spread, instr_net,
            )

    net_usd = gross_usd - spread_usd
    log.info(
        "  Счёт $%.0f, лот %.2f → брутто $%+.2f, комиссии -$%.2f, чистыми $%+.2f (%+.1f%%)",
        balance, lot, gross_usd, spread_usd, net_usd,
        (net_usd / balance * 100) if balance else 0,
    )


def _log_strategy_stats(store: StatsStore, settings: Settings) -> None:
    """Статистика по Leaders / Outsiders позициям."""
    by_strat = store.position_summary_by_strategy()
    if not by_strat:
        return

    log.info("── Позиции по стратегиям ──")
    for row in by_strat:
        pv = pip_value_usd("EURUSD=X", settings.lot_size)
        gross_pips = float(row["total_pips"])
        cost_pips = float(row["total_cost_pips"])
        net_pips = float(row["net_pips"])
        net_usd = net_pips * pv
        log.info(
            "  %s: %d всего (%d откр, %d закр), win-rate %.0f%%, "
            "%+.1f gross, -%0.1f издержки, %+.1f net пипсов, ~$%+.2f",
            str(row["strategy"]).capitalize(),
            row["total"], row["open"], row["closed"],
            float(row["win_rate"]) * 100,
            gross_pips, cost_pips, net_pips, net_usd,
        )

    by_exit = store.paper_summary_by_exit_strategy()
    if by_exit:
        log.info("── Paper exit-стратегии ──")
        for row in by_exit:
            pv = pip_value_usd("EURUSD=X", settings.lot_size)
            net_usd = float(row["total_pips"]) * pv
            log.info(
                "  %s: %d всего, %d закрыто, win-rate %.0f%%, "
                "%+.1f пипсов, ~$%+.2f",
                row["exit_strategy"],
                row["total"], row["closed"],
                float(row["win_rate"]) * 100,
                row["total_pips"], net_usd,
            )


def _log_scalping_stats(store: StatsStore, settings: Settings) -> None:
    """Статистика по скальпинг-стратегиям (VWAP / Stat-Arb / ORB)."""
    strats = ("vwap_reversion", "stat_arb", "session_orb")
    any_data = False
    for name in strats:
        positions = store.get_open_positions(strategy=name)
        total_open = len(positions)
        all_positions = store.position_summary_by_strategy()
        row = next((r for r in all_positions if r["strategy"] == name), None)
        if row is None and total_open == 0:
            continue
        if not any_data:
            log.info("── Скальпинг-стратегии ──")
            any_data = True
        if row:
            pv = pip_value_usd("EURUSD=X", settings.lot_size)
            net_usd = float(row["total_pips"]) * pv
            log.info(
                "  %s: %d всего (%d откр, %d закр), win-rate %.0f%%, "
                "%+.1f пипсов, ~$%+.2f",
                name.replace("_", "-"),
                row["total"], row["open"], row["closed"],
                float(row["win_rate"]) * 100,
                row["total_pips"], net_usd,
            )
        else:
            log.info("  %s: %d открыто, нет закрытых", name.replace("_", "-"), total_open)


def _init_trading(settings: Settings, store: StatsStore):
    """Инициализация модуля автоторговли (cTrader). Возвращает (executor, killswitch) или (None, None)."""
    from fx_pro_bot.trading.client import CTraderClient
    from fx_pro_bot.trading.executor import TradeExecutor

    if not settings.ctrader_trading_enabled:
        log.info("cTrader автоторговля: ВЫКЛЮЧЕНА")
        return None, None

    if not settings.ctrader_client_id or not settings.ctrader_client_secret:
        log.warning("cTrader: CTRADER_CLIENT_ID/SECRET не заданы, торговля отключена")
        return None, None

    token_store = TokenStore(settings.ctrader_token_path)
    try:
        token_data = ensure_valid_token(
            token_store, settings.ctrader_client_id, settings.ctrader_client_secret,
        )
    except Exception as exc:
        log.warning("cTrader: токены недоступны (%s), торговля отключена", exc)
        return None, None

    try:
        client = CTraderClient(
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
            access_token=token_data.access_token,
            account_id=settings.ctrader_account_id,
            host_type=settings.ctrader_host_type,
        )
        client.start(timeout=30)
    except Exception as exc:
        log.error("cTrader: не удалось подключиться (%s), торговля отключена", exc)
        return None, None

    symbol_cache = SymbolCache()
    executor = TradeExecutor(client, symbol_cache, lot_size=settings.lot_size)

    try:
        count = executor.load_symbols()
        log.info("cTrader: загружено %d символов", count)
    except Exception as exc:
        log.warning("cTrader: не удалось загрузить символы (%s)", exc)

    ks_config = KillSwitchConfig(
        max_daily_loss_usd=settings.killswitch_max_daily_loss,
        max_drawdown_pct=settings.killswitch_max_drawdown_pct,
        max_positions=settings.killswitch_max_positions,
        max_loss_per_trade_usd=settings.killswitch_max_loss_per_trade,
    )
    account = executor.get_account_info()
    killswitch = KillSwitch(ks_config, initial_equity=account.balance)

    log.info(
        "cTrader автоторговля: ВКЛЮЧЕНА (%s), баланс $%.2f, kill switch: "
        "макс убыток/день $%.0f, макс просадка %.0f%%, макс позиций %d",
        settings.ctrader_host_type.upper(), account.balance,
        ks_config.max_daily_loss_usd, ks_config.max_drawdown_pct, ks_config.max_positions,
    )
    return executor, killswitch


def run_advisor() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )

    store = StatsStore(settings.stats_db_path)
    events = load_events(settings.events_calendar_path)
    last_directions: dict[str, TrendDirection] = {}
    ctrader_client = CTraderCopyClient()
    whale_tracker = WhaleTracker(store, settings)

    leaders_strat = LeadersStrategy(
        store,
        max_positions=settings.leaders_max_positions,
        sl_atr_mult=settings.leaders_sl_atr,
        trail_atr_mult=settings.leaders_trail_atr,
    )
    outsiders_strat = OutsidersStrategy(
        store,
        max_positions=settings.outsiders_max_positions,
        mode=settings.outsiders_mode,
    )
    monitor = PositionMonitor(store, outsiders_mode=settings.outsiders_mode)
    shadow = ShadowTracker(store)

    vwap_strat = (
        VwapReversionStrategy(store, max_positions=settings.scalping_max_positions)
        if settings.scalping_vwap_enabled else None
    )
    statarb_strat = (
        StatArbStrategy(store, max_positions=settings.scalping_max_positions)
        if settings.scalping_statarb_enabled else None
    )
    orb_strat = (
        SessionOrbStrategy(store, max_positions=settings.scalping_max_positions)
        if settings.scalping_orb_enabled else None
    )

    cycle_count = 0

    executor, killswitch = _init_trading(settings, store)

    scalp_names = [n for n, s in [("VWAP", vwap_strat), ("StatArb", statarb_strat), ("ORB", orb_strat)] if s]
    log.info(
        "Запуск v0.8: ансамбль + Leaders + Outsiders(%s) + Shadow + Scalping(%s)%s, "
        "%d инструментов, цикл %d сек",
        settings.outsiders_mode.upper(),
        "+".join(scalp_names) if scalp_names else "OFF",
        " + cTrader LIVE" if executor else "",
        len(settings.scan_symbols),
        settings.poll_interval_sec,
    )

    while True:
        try:
            _run_cycle(
                settings, store, events, last_directions,
                ctrader_client, whale_tracker,
                leaders_strat, outsiders_strat, monitor, shadow,
                cycle_count, executor, killswitch,
                vwap_strat=vwap_strat,
                statarb_strat=statarb_strat,
                orb_strat=orb_strat,
            )
            cycle_count += 1
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            break
        except Exception:
            log.exception("Ошибка в цикле, повтор через %d сек", settings.poll_interval_sec)

        time.sleep(settings.poll_interval_sec)


def _run_cycle(
    settings: Settings,
    store: StatsStore,
    events: tuple,
    last_directions: dict[str, TrendDirection],
    ctrader_client: CTraderCopyClient,
    whale_tracker: WhaleTracker,
    leaders_strat: LeadersStrategy,
    outsiders_strat: OutsidersStrategy,
    monitor: PositionMonitor,
    shadow: ShadowTracker,
    cycle_count: int,
    executor=None,
    killswitch=None,
    *,
    vwap_strat: VwapReversionStrategy | None = None,
    statarb_strat: StatArbStrategy | None = None,
    orb_strat: SessionOrbStrategy | None = None,
) -> None:
    # 1. Сканирование ансамблем
    log.info("── Сканирование (ансамбль 5 стратегий) ──")
    results = scan_instruments(
        settings.scan_symbols,
        period=settings.yfinance_period,
        interval=settings.yfinance_interval,
    )

    active = active_signals(results)
    prices: dict[str, float] = {r.symbol: r.last_price for r in results}
    bars_map: dict[str, list] = {r.symbol: r.bars for r in results}
    atrs: dict[str, float] = {}
    for r in results:
        if len(r.bars) > 14:
            atrs[r.symbol] = _atr(r.bars)

    if not active:
        log.info("Ансамбль: нет согласия")
    else:
        for r in active:
            prev = last_directions.get(r.symbol)
            if r.signal.direction == prev:
                continue
            last_directions[r.symbol] = r.signal.direction

            ev_now = events_near(events, now=r.bars[-1].ts, within_hours=48.0, min_importance="medium")
            text = advice_for_signal(
                display_name=r.display_name,
                signal=r.signal,
                last_price=r.last_price,
                nearby_events=ev_now,
            )
            strategies = ", ".join(
                reason for reason in r.signal.reasons
                if not reason[0].isdigit() and "/" not in reason
            )
            log.info(
                "— %s %s @ %.5f (сила %s, стратегии: %s) —\n%s",
                r.display_name, r.signal.direction.value.upper(),
                r.last_price, f"{r.signal.strength:.0%}", strategies, text,
            )
            store.record_suggestion(
                instrument=r.symbol,
                direction=r.signal.direction.value,
                advice_text=text,
                reasons=r.signal.reasons,
                price_at_signal=r.last_price,
                events_context=events_to_json_blob(ev_now) if ev_now else None,
            )

    for r in results:
        if r.signal.direction == TrendDirection.FLAT:
            if last_directions.get(r.symbol) != TrendDirection.FLAT:
                last_directions[r.symbol] = TrendDirection.FLAT

    # 2. Leaders
    if settings.leaders_enabled and cycle_count % WHALE_POLL_CYCLES == 0:
        log.info("── Leaders (copy-trading) ──")
        try:
            cot_signals = fetch_cot_signals()
            sentiment_signals = fetch_sentiment_signals(
                settings.myfxbook_email, settings.myfxbook_password,
            )
            leader_sigs = aggregate_leader_signals(cot_signals, sentiment_signals, bars_map)
            before_ids = {p.id for p in store.get_open_positions()}
            opened = leaders_strat.process_signals(leader_sigs, prices)
            _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings)
            closed = leaders_strat.check_source_reversals(cot_signals, sentiment_signals)
            if opened or closed:
                log.info("  Leaders: +%d открыто, -%d закрыто по развороту", opened, closed)
            else:
                log.info("  Leaders: без изменений")
        except Exception:
            log.exception("Ошибка Leaders")

    # 3. Outsiders
    if settings.outsiders_enabled:
        log.info("── Outsiders (extreme setups) ──")
        try:
            outsider_sigs = detect_extreme_setups(
                settings.scan_symbols, bars_map, events,
                now=results[0].bars[-1].ts if results and results[0].bars else None,
                mode=settings.outsiders_mode,
            )
            if outsider_sigs:
                before_ids = {p.id for p in store.get_open_positions()}
                opened = outsiders_strat.process_signals(outsider_sigs, prices)
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings)
                log.info("  Outsiders: %d extreme-сигналов, %d открыто", len(outsider_sigs), opened)
            else:
                log.info("  Outsiders: нет extreme-ситуаций")
        except Exception:
            log.exception("Ошибка Outsiders")

    # 3b. Scalping strategies (отдельный bars_map, чтобы не затрагивать основные стратегии)
    if vwap_strat or statarb_strat or orb_strat:
        log.info("── Скальпинг ──")
        try:
            scalping_bars = dict(bars_map)
            scalping_prices = dict(prices)

            extra = set(SCALPING_EXTRA_SYMBOLS) - set(settings.scan_symbols)
            if extra:
                extra_results = scan_instruments(
                    tuple(extra),
                    period=settings.yfinance_period,
                    interval=settings.yfinance_interval,
                )
                for r in extra_results:
                    scalping_bars[r.symbol] = r.bars
                    scalping_prices[r.symbol] = r.last_price
                    prices[r.symbol] = r.last_price
                    if len(r.bars) > 14:
                        atrs[r.symbol] = _atr(r.bars)

            if vwap_strat:
                v_sigs = vwap_strat.scan(scalping_bars, scalping_prices)
                before_ids = {p.id for p in store.get_open_positions()}
                v_opened = vwap_strat.process_signals(v_sigs, scalping_prices) if v_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings)
                log.info("  VWAP: %d сигналов, %d открыто", len(v_sigs), v_opened)

            if statarb_strat:
                sa_sigs = statarb_strat.scan(scalping_bars)
                before_ids = {p.id for p in store.get_open_positions()}
                sa_opened = statarb_strat.process_signals(sa_sigs, scalping_prices) if sa_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings)
                sa_closed = statarb_strat.check_exits(scalping_bars)
                log.info("  Stat-Arb: %d сигналов, %d открыто, %d закрыто", len(sa_sigs), sa_opened, sa_closed)

            if orb_strat:
                o_sigs = orb_strat.scan(scalping_bars, scalping_prices, events)
                before_ids = {p.id for p in store.get_open_positions()}
                o_opened = orb_strat.process_signals(o_sigs, scalping_prices) if o_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings)
                log.info("  ORB: %d сигналов, %d открыто", len(o_sigs), o_opened)
        except Exception:
            log.exception("Ошибка скальпинг-стратегий")

    # 4. Monitor all positions
    log.info("── Мониторинг позиций ──")
    positions_before_close = {p.id: p for p in store.get_open_positions()}
    mon_stats = monitor.run(prices, atrs)
    open_total = store.count_open_positions()
    log.info(
        "  Позиций: %d открыто, обновлено %d, закрыто: SL=%d trail=%d TP=%d time=%d",
        open_total, mon_stats["updated"],
        mon_stats["closed_sl"], mon_stats["closed_trail"],
        mon_stats["closed_tp"], mon_stats["closed_time"],
    )

    # 4b. cTrader: закрыть реальные позиции, если бот закрыл виртуальные
    if executor and killswitch:
        _sync_broker_closes(store, executor, killswitch, positions_before_close, prices, settings)

    # 5. Shadow
    if settings.shadow_enabled:
        shadow.run(prices)
        shadow.log_summary()

    # 6. Verification
    log.info("── Проверка старых сигналов ──")
    verified = run_verification(store, settings.verify_horizons)
    if verified:
        log.info("Проверено %d сигналов", verified)
    else:
        log.info("Нет созревших сигналов для проверки")

    # 7. Statistics
    log.info("── Статистика ансамбля ──")
    _log_stats(store, settings.verify_horizons, settings)
    _log_strategy_stats(store, settings)
    _log_scalping_stats(store, settings)
    whale_tracker.log_whale_stats()

    if cycle_count % CTRADER_POLL_CYCLES == 0:
        _log_ctrader_top(ctrader_client)

    # 8. Cleanup (раз в ~24 часа: 288 циклов × 300 сек)
    if cycle_count > 0 and cycle_count % CLEANUP_POLL_CYCLES == 0:
        try:
            deleted = cleanup_shadow_log(settings.stats_db_path)
            size = db_size_mb(settings.stats_db_path)
            log.info("── Обслуживание БД: shadow_log -%d строк, размер %.1f MB ──", deleted, size)
            vacuum_if_needed(settings.stats_db_path, threshold_mb=100.0)
        except Exception:
            log.exception("Ошибка cleanup")


def _open_broker_for_new(
    store: StatsStore,
    executor,
    killswitch,
    before_ids: set[str],
    prices: dict[str, float],
    settings: Settings,
) -> None:
    """Открыть cTrader-ордера для позиций, появившихся после before_ids snapshot."""
    if not executor or not killswitch:
        return

    new_positions = [p for p in store.get_open_positions() if p.id not in before_ids]
    if not new_positions:
        return

    for pos in new_positions:
        if pos.broker_position_id:
            continue
        try:
            account = executor.get_account_info()
            open_count = len(executor.get_open_positions())
            if not killswitch.check_allowed(open_count, account.balance):
                log.warning("KillSwitch: заблокировано, пропускаем %s", pos.instrument)
                break

            result = executor.open_position(
                yf_symbol=pos.instrument,
                direction=pos.direction,
                sl_price=pos.stop_loss_price if pos.stop_loss_price > 0 else None,
                lot_size=settings.lot_size,
                comment=f"fx-pro-bot {pos.id[:8]}",
            )

            if result.success and result.broker_position_id:
                store.set_broker_position_id(pos.id, result.broker_position_id)
                log.info(
                    "  cTrader OPEN: %s %s → broker #%d @ %.5f",
                    pos.instrument, pos.direction,
                    result.broker_position_id, result.fill_price,
                )
            elif not result.success:
                log.warning("  cTrader OPEN FAILED: %s — %s", pos.instrument, result.error)
        except Exception:
            log.exception("  cTrader OPEN error: %s", pos.instrument)


def _sync_broker_closes(
    store: StatsStore,
    executor,
    killswitch,
    positions_before: dict,
    prices: dict[str, float],
    settings: Settings,
) -> None:
    """Закрыть реальные позиции, если бот закрыл виртуальные."""
    from fx_pro_bot.trading.symbols import lots_to_volume

    current_open = {p.id for p in store.get_open_positions()}
    closed_ids = set(positions_before.keys()) - current_open

    for pid in closed_ids:
        pos = positions_before.get(pid)
        if not pos or pos.broker_position_id == 0:
            continue

        volume = lots_to_volume(settings.lot_size)
        result = executor.close_position(pos.broker_position_id, volume)

        if result.success:
            pnl = pos.profit_pips * pip_value_usd(pos.instrument, settings.lot_size)
            killswitch.record_trade_close(pnl)
            log.info(
                "  cTrader CLOSE: broker pos #%d (%s), P&L $%.2f",
                pos.broker_position_id, pos.instrument, pnl,
            )
        else:
            log.error(
                "  cTrader CLOSE FAILED: broker pos #%d — %s",
                pos.broker_position_id, result.error,
            )

    if killswitch.is_tripped:
        log.critical("KILL SWITCH: аварийное закрытие ВСЕХ позиций!")
        closed = executor.close_all_positions()
        log.critical("KILL SWITCH: закрыто %d позиций у брокера", closed)


def _log_ctrader_top(client: CTraderCopyClient) -> None:
    try:
        strategies = client.top_strategies(limit=5)
        text = format_top_strategies(strategies, limit=5)
        log.info("── %s", text)
    except Exception:
        log.debug("cTrader Copy: не удалось получить топ-стратегии")


def main() -> None:
    run_advisor()


if __name__ == "__main__":
    main()
