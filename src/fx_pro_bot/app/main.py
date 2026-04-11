"""Сканер-советник v0.7: ансамбль + Leaders + Outsiders + Shadow + Scalping + cTrader auto-trading."""

from __future__ import annotations

import logging
import time

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.scanner import active_signals, scan_instruments
from fx_pro_bot.analysis.signals import TrendDirection, _atr
from fx_pro_bot.config.settings import SCALPING_EXCLUDE_SYMBOLS, SCALPING_EXTRA_SYMBOLS, Settings, broker_commission_usd, display_name, is_crypto, pip_size, pip_value_usd, spread_cost_pips
from fx_pro_bot.strategies.monitor import (
    SCALPING_TP_PIPS, SCALPING_TRAIL_TRIGGER_PIPS, SCALPING_TRAIL_DISTANCE_PIPS,
    OUTSIDERS_CONFIRMED_AGGRESSIVE_TP,
    OUTSIDERS_TP_ATR_MULT, SCALPING_TP_ATR_MULT,
    OUTSIDERS_TRAIL_TRIGGER_ATR_MULT, OUTSIDERS_TRAIL_DISTANCE_ATR_MULT,
)

LEADERS_TP_PIPS = 50.0
from fx_pro_bot.copytrading.ctrader import CTraderCopyClient, format_top_strategies
from fx_pro_bot.events import events_near, events_to_json_blob, load_events
from fx_pro_bot.stats.cleanup import cleanup_shadow_log, db_size_mb, vacuum_if_needed
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.stats.verifier import run_verification
from fx_pro_bot.strategies.leaders import LeadersStrategy, aggregate_leader_signals
from fx_pro_bot.strategies.monitor import PositionMonitor
from fx_pro_bot.strategies.exits import create_paper_positions
from fx_pro_bot.strategies.outsiders import ADX_MAX_FOR_MEAN_REVERSION, CONFIRMED_SL_ATR, OUTSIDERS_EXCLUDE_SYMBOLS, OutsidersStrategy, detect_extreme_setups
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
    comm_usd = 0.0
    comm_per = broker_commission_usd(lot)
    total_trades = 0
    if by_instr:
        log.info("  По инструментам (лот %.2f):", lot)
        for row in by_instr:
            pips = float(row["total_profit"])
            symbol = str(row["instrument"])
            num_trades = int(row["total"]) // len(horizons) if horizons else int(row["total"])
            total_trades += num_trades
            pv = pip_value_usd(symbol, lot)
            instr_gross = pips * pv
            instr_spread = spread_cost_pips(symbol) * pv * num_trades
            instr_comm = comm_per * num_trades
            instr_net = instr_gross - instr_spread - instr_comm
            gross_usd += instr_gross
            spread_usd += instr_spread
            comm_usd += instr_comm
            log.info(
                "    %s: %d проверок, win-rate %.0f%%, %+.1f пунктов → "
                "брутто $%+.2f, спред -$%.2f, комис -$%.2f, чист $%+.2f",
                display_name(symbol), row["total"],
                row["win_rate"] * 100, pips,  # type: ignore[arg-type]
                instr_gross, instr_spread, instr_comm, instr_net,
            )

    total_costs = spread_usd + comm_usd
    net_usd = gross_usd - total_costs
    log.info(
        "  Счёт $%.0f, лот %.2f → брутто $%+.2f, спред -$%.2f, "
        "комиссия FxPro -$%.2f (%d×$%.2f), чистыми $%+.2f (%+.1f%%)",
        balance, lot, gross_usd, spread_usd,
        comm_usd, total_trades, comm_per,
        net_usd, (net_usd / balance * 100) if balance else 0,
    )


def _get_closed_broker_positions(store: StatsStore) -> list:
    """Закрытые позиции которые были на cTrader."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND broker_position_id > 0"
        ).fetchall()
    from fx_pro_bot.stats.store import _row_to_position
    return [_row_to_position(r) for r in rows]


def _log_strategy_stats(store: StatsStore, settings: Settings, executor=None) -> None:
    """Статистика по стратегиям. Unrealized P&L берём с cTrader (точный)."""
    broker_pnl: dict[int, tuple[float, float]] = {}
    if executor:
        try:
            broker_pnl = executor.get_unrealized_pnl()
        except Exception as exc:
            log.debug("get_unrealized_pnl failed: %s", exc)

    positions = [p for p in store.get_open_positions() if p.broker_position_id]
    positions += [p for p in _get_closed_broker_positions(store)]

    strats: dict[str, dict] = {}
    for pos in positions:
        s = pos.strategy
        if s not in strats:
            strats[s] = {"total": 0, "closed": 0, "wins": 0,
                         "realized": 0.0, "unrealized_net": 0.0}
        strats[s]["total"] += 1
        if pos.status == "closed":
            strats[s]["closed"] += 1
        if pos.status == "open" and pos.broker_position_id in broker_pnl:
            _, net = broker_pnl[pos.broker_position_id]
            strats[s]["unrealized_net"] += net

    if not strats:
        return

    comm_per_trade = broker_commission_usd(settings.lot_size)
    log.info("── P&L по стратегиям (cTrader, комиссия $%.2f/сделка) ──", comm_per_trade)
    total_unrealized = 0.0
    total_trades = 0
    total_closed = 0
    for name, s in sorted(strats.items()):
        total_unrealized += s["unrealized_net"]
        total_trades += s["total"]
        total_closed += s["closed"]
        est_comm = s["total"] * comm_per_trade
        log.info(
            "  %s: %d сделок (%d закр), нереализ $%+.2f, комиссия ~$%.2f",
            name, s["total"], s["closed"], s["unrealized_net"], est_comm,
        )
    total_comm = total_trades * comm_per_trade
    log.info(
        "  ИТОГО: нереализ $%+.2f, комиссия ~$%.2f (%d сделок × $%.2f)",
        total_unrealized, total_comm, total_trades, comm_per_trade,
    )

    by_exit = store.paper_summary_by_exit_strategy()
    if by_exit:
        log.info("── Paper exit-стратегии (бумага) ──")
        for row in by_exit:
            log.info(
                "  %s: %d всего, %d закрыто, win-rate %.0f%%, %+.1f pips",
                row["exit_strategy"],
                row["total"], row["closed"],
                float(row["win_rate"]) * 100,
                row["total_pips"],
            )


def _log_scalping_stats(store: StatsStore, settings: Settings) -> None:
    """Статистика по скальпинг-стратегиям — объединена с основной _log_strategy_stats."""
    pass


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

    def _on_token_refreshed(new_access: str, new_refresh: str) -> None:
        from fx_pro_bot.trading.auth import TokenData
        import time as _time

        updated = TokenData(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_at=_time.time() + 2_628_000,
        )
        try:
            token_store.save(updated)
        except Exception as exc:
            log.warning("cTrader: не удалось сохранить обновлённые токены: %s", exc)

    try:
        client = CTraderClient(
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
            access_token=token_data.access_token,
            account_id=settings.ctrader_account_id,
            host_type=settings.ctrader_host_type,
            refresh_token=token_data.refresh_token,
            on_token_refreshed=_on_token_refreshed,
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
    monitor = PositionMonitor(store, outsiders_mode=settings.outsiders_mode, lot_size=settings.lot_size)
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

    if executor and killswitch:
        _reconcile_broker_positions(store, executor, settings)
        _sync_unlinked_positions(store, executor, killswitch, settings)

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
        ensemble_before_ids = {p.id for p in store.get_open_positions()}
        ensemble_opened = 0
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

            price = r.last_price
            atr = atrs.get(r.symbol, price * 0.005)
            ps = pip_size(r.symbol)

            if r.signal.direction == TrendDirection.LONG:
                sl = price - CONFIRMED_SL_ATR * atr
            else:
                sl = price + CONFIRMED_SL_ATR * atr

            if r.symbol in OUTSIDERS_EXCLUDE_SYMBOLS:
                continue

            from fx_pro_bot.analysis.signals import compute_adx
            sym_bars = bars_map.get(r.symbol, [])
            if sym_bars and compute_adx(sym_bars) > ADX_MAX_FOR_MEAN_REVERSION:
                log.info("  %s: ADX > %.0f — пропуск (сильный тренд)", r.display_name, ADX_MAX_FOR_MEAN_REVERSION)
                continue

            ens_count = store.count_open_positions(strategy="ensemble", instrument=r.symbol)
            if ens_count >= 2:
                continue

            pid = store.open_position(
                strategy="ensemble",
                source="ensemble_vote",
                instrument=r.symbol,
                direction=r.signal.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )
            cost = estimate_entry_cost(r.symbol, "cot", atr, ps)
            store.set_estimated_cost(pid, cost.round_trip_pips)
            create_paper_positions(store, pid, price, r.signal.direction, atr, ps)

            log.info(
                "  ENSEMBLE OPEN: %s %s @ %.5f (SL=%.5f, ATR=%.5f)",
                display_name(r.symbol), r.signal.direction.value.upper(),
                price, sl, atr,
            )
            ensemble_opened += 1

        _open_broker_for_new(store, executor, killswitch, ensemble_before_ids, prices, settings, atrs)
        if ensemble_opened:
            log.info("  Ансамбль: %d позиций открыто", ensemble_opened)

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
            _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings, atrs)
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
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings, atrs)
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
                v_sigs = [s for s in vwap_strat.scan(scalping_bars, scalping_prices)
                          if s.instrument not in SCALPING_EXCLUDE_SYMBOLS]
                before_ids = {p.id for p in store.get_open_positions()}
                v_opened = vwap_strat.process_signals(v_sigs, scalping_prices) if v_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings, atrs)
                log.info("  VWAP: %d сигналов, %d открыто", len(v_sigs), v_opened)

            if statarb_strat:
                sa_sigs = statarb_strat.scan(scalping_bars)
                before_ids = {p.id for p in store.get_open_positions()}
                sa_opened = statarb_strat.process_signals(sa_sigs, scalping_prices) if sa_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings, atrs)
                sa_closed = statarb_strat.check_exits(scalping_bars)
                log.info("  Stat-Arb: %d сигналов, %d открыто, %d закрыто", len(sa_sigs), sa_opened, sa_closed)

            if orb_strat:
                o_sigs = [s for s in orb_strat.scan(scalping_bars, scalping_prices, events)
                          if s.instrument not in SCALPING_EXCLUDE_SYMBOLS]
                before_ids = {p.id for p in store.get_open_positions()}
                o_opened = orb_strat.process_signals(o_sigs, scalping_prices) if o_sigs else 0
                _open_broker_for_new(store, executor, killswitch, before_ids, prices, settings, atrs)
                log.info("  ORB: %d сигналов, %d открыто", len(o_sigs), o_opened)
        except Exception:
            log.exception("Ошибка скальпинг-стратегий")

    # 4. Detect broker-side closures (server-side TP/SL hit by cTrader)
    if executor:
        _detect_broker_closures(store, executor)

    # 4b. Monitor all positions
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

    # 4c. cTrader: гарантировать SL+TP на всех позициях
    if executor:
        _ensure_broker_sl_tp(store, executor, atrs, prices)

    # 4d. cTrader: двинуть trailing SL на брокере
    if executor:
        _update_broker_trailing_sl(store, executor, atrs)

    # 4e. cTrader: закрыть реальные позиции, если бот закрыл виртуальные
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
    _log_strategy_stats(store, settings, executor)
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


def _reconcile_broker_positions(
    store: StatsStore,
    executor,
    settings: Settings,
) -> None:
    """Сверка DB с реальными cTrader-позициями при старте.

    1. DB open + broker_id → cTrader closed? → закрыть в DB.
    2. cTrader open → нет в DB? → orphan, закрыть на брокере.
    """
    from fx_pro_bot.trading.symbols import lots_to_volume

    broker_positions = {bp.positionId: bp for bp in executor.get_open_positions()}
    db_with_broker = [
        p for p in store.get_open_positions() if p.broker_position_id
    ]

    db_broker_ids = set()
    closed_in_broker = 0
    backfilled = 0
    for pos in db_with_broker:
        db_broker_ids.add(pos.broker_position_id)
        if pos.broker_position_id not in broker_positions:
            store.close_position(pos.id, "broker_closed")
            closed_in_broker += 1
            log.info(
                "  RECONCILE: %s %s broker #%d закрыта на стороне брокера",
                pos.instrument, pos.direction, pos.broker_position_id,
            )
        elif pos.broker_volume == 0:
            bp = broker_positions[pos.broker_position_id]
            vol = bp.tradeData.volume if hasattr(bp, "tradeData") else 0
            if vol:
                store.set_broker_position_id(pos.id, pos.broker_position_id, vol)
                backfilled += 1

    orphans = set(broker_positions.keys()) - db_broker_ids
    closed_orphans = 0
    for bp_id in orphans:
        bp = broker_positions[bp_id]
        vol = bp.tradeData.volume if hasattr(bp, "tradeData") else 0
        if not vol:
            log.warning("  RECONCILE: orphan #%d — не удалось определить volume", bp_id)
            continue
        try:
            executor.close_position(bp_id, vol)
            closed_orphans += 1
            log.warning("  RECONCILE: orphan broker #%d закрыт", bp_id)
        except Exception as exc:
            log.error("  RECONCILE: не удалось закрыть orphan #%d: %s", bp_id, exc)

    log.info(
        "cTrader reconcile: %d в DB, %d на брокере, "
        "закрыто в DB=%d, orphans=%d, backfill volume=%d",
        len(db_with_broker), len(broker_positions),
        closed_in_broker, closed_orphans, backfilled,
    )


def _sync_unlinked_positions(
    store: StatsStore,
    executor,
    killswitch,
    settings: Settings,
    atrs: dict[str, float] | None = None,
) -> None:
    """Открыть cTrader-ордера для бумажных позиций без broker_position_id."""
    unlinked = [p for p in store.get_open_positions() if not p.broker_position_id]
    if not unlinked:
        log.info("cTrader sync: все позиции уже привязаны к брокеру")
        return

    available = [p for p in unlinked
                 if executor._symbols.resolve_yfinance(p.instrument) is not None]
    log.info("cTrader sync: %d без broker_id, %d доступны на бирже", len(unlinked), len(available))
    opened = 0
    for pos in available:
        try:
            account = executor.get_account_info()
            open_count = len(executor.get_open_positions())
            if not killswitch.check_allowed(open_count, account.balance):
                log.warning("KillSwitch: лимит достигнут, остановка sync (%d/%d)", opened, len(available))
                break

            ps = pip_size(pos.instrument)
            pos_atr = (atrs or {}).get(pos.instrument, 0.0)
            tp_dist = _calc_tp_distance(pos.strategy, ps, pos_atr, pos.instrument, pos.entry_price)
            sl_dist: float | None = None
            if pos.stop_loss_price > 0 and pos.entry_price > 0:
                sl_dist = abs(pos.entry_price - pos.stop_loss_price)
            elif is_crypto(pos.instrument) and pos_atr > 0:
                from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_ATR_MULT
                sl_dist = CRYPTO_SCALP_SL_ATR_MULT * pos_atr

            result = executor.open_position(
                yf_symbol=pos.instrument,
                direction=pos.direction,
                sl_distance=sl_dist,
                tp_distance=tp_dist,
                lot_size=settings.lot_size,
                comment=f"fx-pro-bot sync {pos.id[:8]}",
                entry_price_hint=pos.entry_price,
            )

            if result.success and result.broker_position_id:
                store.set_broker_position_id(pos.id, result.broker_position_id, result.volume)
                log.info(
                    "  cTrader SYNC: %s %s → broker #%d @ %.5f (vol=%d)",
                    pos.instrument, pos.direction,
                    result.broker_position_id, result.fill_price, result.volume,
                )
                opened += 1
            elif not result.success:
                if "NOT_ENOUGH_MONEY" in result.error:
                    log.warning("cTrader sync: недостаточно средств, стоп")
                    break
                if "не найден" not in result.error:
                    log.warning("  cTrader SYNC FAILED: %s — %s", pos.instrument, result.error)
        except Exception:
            log.exception("  cTrader SYNC error: %s", pos.instrument)

    log.info("cTrader sync: открыто %d/%d ордеров", opened, len(available))


def _calc_tp_distance(
    strategy: str, ps: float, atr: float = 0.0,
    instrument: str = "", entry_price: float = 0.0,
) -> float | None:
    """Расстояние TP от entry в единицах цены. cTrader сам применит к fill price.

    Для скальпинга: TP >= max(ATR-based, fixed pips, commission buffer).
    Commission buffer гарантирует что TP покрывает round-trip costs (спред + комиссия).
    """
    from fx_pro_bot.strategies.monitor import (
        CRYPTO_SCALP_TP_ATR_MULT, CRYPTO_SCALP_TP_MIN_PCT,
    )
    scalping = ("vwap_reversion", "stat_arb", "session_orb")
    if strategy in scalping:
        if is_crypto(instrument) and entry_price > 0:
            atr_tp = CRYPTO_SCALP_TP_ATR_MULT * atr if atr > 0 else 0.0
            pct_tp = entry_price * CRYPTO_SCALP_TP_MIN_PCT
            return max(atr_tp, pct_tp)
        atr_tp = SCALPING_TP_ATR_MULT * atr if atr > 0 else 0.0
        fixed_tp = SCALPING_TP_PIPS * ps
        commission_pips = broker_commission_usd() / pip_value_usd(instrument) if pip_value_usd(instrument) > 0 else 1.0
        cost_floor = (spread_cost_pips(instrument) + commission_pips) * 3.0 * ps
        return max(atr_tp, fixed_tp, cost_floor)
    if strategy in ("outsiders", "ensemble"):
        atr_tp = OUTSIDERS_TP_ATR_MULT * atr if atr > 0 else 0.0
        fixed_tp = OUTSIDERS_CONFIRMED_AGGRESSIVE_TP * ps
        return max(atr_tp, fixed_tp)
    if strategy == "leaders":
        return LEADERS_TP_PIPS * ps
    return None


def _open_broker_for_new(
    store: StatsStore,
    executor,
    killswitch,
    before_ids: set[str],
    prices: dict[str, float],
    settings: Settings,
    atrs: dict[str, float] | None = None,
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

            ps = pip_size(pos.instrument)
            sl_dist: float | None = None
            if pos.stop_loss_price > 0 and pos.entry_price > 0:
                sl_dist = abs(pos.entry_price - pos.stop_loss_price)
            elif is_crypto(pos.instrument):
                from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_ATR_MULT
                pos_atr_sl = (atrs or {}).get(pos.instrument, 0.0)
                if pos_atr_sl > 0:
                    sl_dist = CRYPTO_SCALP_SL_ATR_MULT * pos_atr_sl

            if is_crypto(pos.instrument) and pos.entry_price > 0:
                from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_MIN_PCT
                min_sl = pos.entry_price * CRYPTO_SCALP_SL_MIN_PCT
                sl_dist = max(sl_dist or 0.0, min_sl) or None

            pos_atr = (atrs or {}).get(pos.instrument, 0.0)
            tp_dist = _calc_tp_distance(pos.strategy, ps, pos_atr, pos.instrument, pos.entry_price)

            result = executor.open_position(
                yf_symbol=pos.instrument,
                direction=pos.direction,
                sl_distance=sl_dist,
                tp_distance=tp_dist,
                lot_size=settings.lot_size,
                comment=f"fx-pro-bot {pos.id[:8]}",
                entry_price_hint=pos.entry_price,
            )

            if result.success and result.broker_position_id:
                store.set_broker_position_id(pos.id, result.broker_position_id, result.volume)
                log.info(
                    "  cTrader OPEN: %s %s → broker #%d @ %.5f (vol=%d) TP±%.5f SL±%s",
                    pos.instrument, pos.direction,
                    result.broker_position_id, result.fill_price, result.volume,
                    tp_dist or 0, f"{sl_dist:.5f}" if sl_dist else "—",
                )
            elif not result.success:
                if "NOT_ENOUGH_MONEY" in result.error:
                    log.warning("cTrader: недостаточно средств, стоп")
                    break
                log.warning("  cTrader OPEN FAILED: %s — %s", pos.instrument, result.error)
        except Exception:
            log.exception("  cTrader OPEN error: %s", pos.instrument)


def _ensure_broker_sl_tp(
    store: StatsStore, executor, atrs: dict[str, float],
    prices: dict[str, float] | None = None,
) -> None:
    """Каждый цикл проверяем все позиции на брокере — если SL/TP отсутствует, доставляем.

    Защита от сбоев: если amend при открытии упал (таймаут, сеть),
    следующий цикл подхватит и доставит недостающие уровни.
    """
    log.info("── Аудит SL/TP на брокере ──")

    try:
        broker_positions = executor.get_open_positions()
    except Exception:
        log.warning("  Аудит SL/TP: не удалось получить позиции с брокера")
        return

    if not broker_positions:
        log.info("  Нет открытых позиций на брокере")
        return

    db_map: dict[int, Any] = {}
    for p in store.get_open_positions():
        if p.broker_position_id:
            db_map[p.broker_position_id] = p

    ok_count = 0
    no_sl = 0
    no_tp = 0
    fixed = 0

    for bp in broker_positions:
        pos_id = bp.positionId
        has_sl = hasattr(bp, "stopLoss") and bp.HasField("stopLoss")
        has_tp = hasattr(bp, "takeProfit") and bp.HasField("takeProfit")

        if has_sl and has_tp:
            ok_count += 1
            continue

        if not has_sl:
            no_sl += 1
        if not has_tp:
            no_tp += 1

        db_pos = db_map.get(pos_id)
        if not db_pos:
            td_orphan = bp.tradeData if hasattr(bp, "tradeData") else None
            orphan_vol = td_orphan.volume if td_orphan else 0

            if not has_sl:
                log.warning("  ORPHAN CLOSE: #%d без SL и без DB → закрываем (vol=%s)", pos_id, orphan_vol)
                try:
                    result = executor.close_position(pos_id, orphan_vol if orphan_vol else None)
                    if result.success:
                        fixed += 1
                        continue
                    log.warning("  ORPHAN CLOSE FAILED #%d: %s → ставим аварийный SL/TP", pos_id, result.error)
                except Exception as exc:
                    log.warning("  ORPHAN CLOSE FAILED #%d: %s → ставим аварийный SL/TP", pos_id, exc)

            entry = bp.price if hasattr(bp, "price") and bp.price else 0
            if entry:
                is_buy_orphan = td_orphan.tradeSide == 1 if td_orphan else True
                emergency_dist = entry * 0.02
                e_sl = (entry - emergency_dist) if is_buy_orphan else (entry + emergency_dist)
                e_tp = (entry + emergency_dist) if is_buy_orphan else (entry - emergency_dist)
                try:
                    executor.amend_sl_tp(pos_id, sl_price=e_sl if not has_sl else None,
                                         tp_price=e_tp if not has_tp else None)
                    fixed += 1
                    log.info("  ORPHAN SL/TP: #%d → SL=%.5f TP=%.5f (emergency ±2%%)", pos_id, e_sl, e_tp)
                except Exception as exc:
                    log.error("  ORPHAN SL/TP FAILED #%d: %s", pos_id, exc)
            else:
                log.warning("  #%d — orphan, нет entry price, пропускаем", pos_id)
            continue

        ps = pip_size(db_pos.instrument)
        if ps == 0:
            continue

        entry = bp.price if hasattr(bp, "price") and bp.price else db_pos.entry_price
        if not entry:
            continue

        td = bp.tradeData if hasattr(bp, "tradeData") else None
        is_buy = td.tradeSide == 1 if td else (db_pos.direction == "long")

        new_sl: float | None = None
        new_tp: float | None = None

        if not has_sl:
            if db_pos.stop_loss_price and db_pos.stop_loss_price > 0:
                new_sl = db_pos.stop_loss_price
            else:
                pos_atr = atrs.get(db_pos.instrument, 0.0)
                if is_crypto(db_pos.instrument) and pos_atr > 0:
                    from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_ATR_MULT, CRYPTO_SCALP_SL_MIN_PCT
                    sl_dist = max(CRYPTO_SCALP_SL_ATR_MULT * pos_atr, entry * CRYPTO_SCALP_SL_MIN_PCT)
                elif is_crypto(db_pos.instrument):
                    from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_MIN_PCT
                    sl_dist = entry * CRYPTO_SCALP_SL_MIN_PCT
                else:
                    sl_dist = pos_atr * CONFIRMED_SL_ATR if pos_atr > 0 else 10 * ps
                new_sl = (entry - sl_dist) if is_buy else (entry + sl_dist)

            cur_price = (prices or {}).get(db_pos.instrument, 0.0)
            spread_buf = spread_cost_pips(db_pos.instrument) * ps
            if cur_price > 0:
                sl_past = (is_buy and new_sl > cur_price - spread_buf) or (not is_buy and new_sl < cur_price + spread_buf)
                if sl_past:
                    log.warning(
                        "  FORCE CLOSE: %s #%d — SL %.5f уже пройден (price=%.5f)",
                        display_name(db_pos.instrument), pos_id, new_sl, cur_price,
                    )
                    try:
                        executor.close_position(pos_id)
                        if db_pos:
                            store.close_position(db_pos.id, "audit_sl_past")
                        fixed += 1
                    except Exception as exc:
                        log.error("  Ошибка FORCE CLOSE #%d: %s", pos_id, exc)
                    continue

        if not has_tp:
            pos_atr = atrs.get(db_pos.instrument, 0.0)
            tp_dist = _calc_tp_distance(db_pos.strategy, ps, pos_atr, db_pos.instrument, entry)
            if tp_dist:
                new_tp = (entry + tp_dist) if is_buy else (entry - tp_dist)
            else:
                fallback = entry * 0.002 if is_crypto(db_pos.instrument) else 10 * ps
                new_tp = (entry + fallback) if is_buy else (entry - fallback)

        ok = executor.amend_sl_tp(
            pos_id,
            sl_price=new_sl,
            tp_price=new_tp,
            yf_symbol=db_pos.instrument,
        )
        if ok:
            fixed += 1
            log.info(
                "  FIX: %s %s #%d → %s%s",
                display_name(db_pos.instrument), db_pos.direction.upper(), pos_id,
                f"SL={new_sl:.5f} " if new_sl else "",
                f"TP={new_tp:.5f}" if new_tp else "",
            )

    if no_sl == 0 and no_tp == 0:
        log.info("  Все %d позиций с SL и TP ✓", ok_count)
    else:
        log.info(
            "  Итого: %d ок, %d без SL, %d без TP → исправлено %d",
            ok_count, no_sl, no_tp, fixed,
        )


def _update_broker_trailing_sl(store: StatsStore, executor, atrs: dict[str, float]) -> None:
    """Двигать SL на cTrader вслед за trailing stop.

    Каждый цикл пересчитываем trailing SL и если он лучше текущего — обновляем на брокере.
    Так cTrader сам закроет при откате, не дожидаясь 5-минутного цикла.
    """
    for pos in store.get_open_positions():
        if not pos.broker_position_id:
            continue

        ps = pip_size(pos.instrument)
        if ps == 0:
            continue

        peak_pips = (
            (pos.peak_price - pos.entry_price) / ps if pos.direction == "long"
            else (pos.entry_price - pos.peak_price) / ps
        )

        scalping = ("vwap_reversion", "stat_arb", "session_orb")
        if pos.strategy in scalping and peak_pips >= SCALPING_TRAIL_TRIGGER_PIPS:
            trail_dist = SCALPING_TRAIL_DISTANCE_PIPS * ps
        elif pos.strategy in ("outsiders", "ensemble"):
            atr = atrs.get(pos.instrument, pos.entry_price * 0.005)
            atr_pips = atr / ps if ps > 0 else 0
            trigger = max(OUTSIDERS_TRAIL_TRIGGER_ATR_MULT * atr_pips, 5.0)
            if peak_pips < trigger:
                continue
            trail_dist = max(OUTSIDERS_TRAIL_DISTANCE_ATR_MULT * atr_pips, 3.0) * ps
        elif pos.strategy == "leaders" and peak_pips > 0:
            atr = atrs.get(pos.instrument, pos.entry_price * 0.005)
            trail_dist = 0.7 * atr
        else:
            continue

        if pos.direction == "long":
            new_sl = pos.peak_price - trail_dist
            if new_sl <= pos.stop_loss_price:
                continue
        else:
            new_sl = pos.peak_price + trail_dist
            if pos.stop_loss_price > 0 and new_sl >= pos.stop_loss_price:
                continue

        ok = executor.amend_sl_tp(
            pos.broker_position_id, sl_price=new_sl, yf_symbol=pos.instrument,
        )
        if ok:
            store.update_stop_loss(pos.id, new_sl)
            log.info(
                "  TRAIL SL: %s %s → SL %.5f (peak=%.5f, dist=%.5f)",
                display_name(pos.instrument), pos.direction.upper(),
                new_sl, pos.peak_price, trail_dist,
            )


def _detect_broker_closures(store: StatsStore, executor) -> int:
    """Обнаружить позиции, закрытые брокером (серверный TP/SL).

    Каждый цикл сверяем DB-позиции с broker_id против реально открытых на cTrader.
    Если позиции нет у брокера — значит cTrader закрыл по TP/SL.
    Запрашиваем deal history для реального P&L.
    """
    try:
        broker_ids = {bp.positionId for bp in executor.get_open_positions()}
    except Exception as exc:
        log.debug("_detect_broker_closures: get_open_positions failed: %s", exc)
        return 0

    db_open = [p for p in store.get_open_positions() if p.broker_position_id]
    closed = 0
    closed_broker_ids: list[tuple[str, int, str, str, float]] = []
    for pos in db_open:
        if pos.broker_position_id not in broker_ids:
            store.close_position(pos.id, "broker_tp_sl")
            closed += 1
            closed_broker_ids.append((pos.id, pos.broker_position_id, pos.instrument, pos.direction, pos.entry_price))
            log.info(
                "  BROKER TP/SL: %s %s %s → broker #%d закрыт сервером",
                pos.strategy.upper(), display_name(pos.instrument),
                pos.direction.upper(), pos.broker_position_id,
            )

    if closed_broker_ids:
        _update_broker_pnl(store, executor, closed_broker_ids)

    return closed


def _update_broker_pnl(
    store: StatsStore,
    executor,
    closed_positions: list[tuple[str, int, str, str, float]],
) -> None:
    """Запросить deal history и обновить P&L для закрытых брокером позиций."""
    import time as _time

    now_ms = int(_time.time() * 1000)
    day_ago_ms = now_ms - 24 * 3600 * 1000

    try:
        deals = executor.get_deal_list(day_ago_ms, now_ms)
    except Exception as exc:
        log.warning("_update_broker_pnl: get_deal_list failed: %s", exc)
        return

    deal_by_pos: dict[int, dict] = {}
    for d in deals:
        deal_by_pos[d["positionId"]] = d

    for pos_id, broker_id, instrument, direction, entry_price in closed_positions:
        deal = deal_by_pos.get(broker_id)
        if not deal:
            log.debug("_update_broker_pnl: no deal for broker #%d", broker_id)
            continue

        ps = pip_size(instrument)
        exec_price = deal.get("executionPrice", 0)
        cpd_entry = deal.get("entryPrice", 0)
        actual_entry = cpd_entry if cpd_entry else entry_price

        pnl_pips: float | None = None

        if exec_price and actual_entry and ps > 0:
            price_diff = abs(exec_price - actual_entry)
            if price_diff < actual_entry * 0.05:
                if direction == "long":
                    pnl_pips = (exec_price - actual_entry) / ps
                else:
                    pnl_pips = (actual_entry - exec_price) / ps
            else:
                log.warning(
                    "  BROKER PNL: broker #%d exec=%.5f слишком далеко от entry=%.5f, "
                    "используем grossProfit", broker_id, exec_price, actual_entry,
                )

        if pnl_pips is None:
            gross = deal.get("grossProfit", 0)
            if gross and ps > 0:
                pv = pip_value_usd(instrument)
                pnl_pips = gross / pv if pv > 0 else 0.0
                log.info(
                    "  BROKER PNL: broker #%d → %+.1f pips (from grossProfit $%.2f)",
                    broker_id, pnl_pips, gross,
                )

        if pnl_pips is not None:
            pnl_pct = pnl_pips * ps / actual_entry * 100 if actual_entry else 0.0
            store.update_closed_pnl(pos_id, round(pnl_pips, 2), round(pnl_pct, 4))
            if exec_price and abs(exec_price - actual_entry) < actual_entry * 0.05:
                log.info(
                    "  BROKER PNL: broker #%d → %+.1f pips (exec=%.5f, entry=%.5f)",
                    broker_id, pnl_pips, exec_price, actual_entry,
                )


def _sync_broker_closes(
    store: StatsStore,
    executor,
    killswitch,
    positions_before: dict,
    prices: dict[str, float],
    settings: Settings,
) -> None:
    """Закрыть реальные позиции, если бот закрыл виртуальные."""
    current_open = {p.id for p in store.get_open_positions()}
    closed_ids = set(positions_before.keys()) - current_open

    to_close = [positions_before[pid] for pid in closed_ids
                if positions_before[pid].broker_position_id]
    if not to_close:
        return

    broker_positions = {bp.positionId: bp for bp in executor.get_open_positions()}

    for pos in to_close:
        bp = broker_positions.get(pos.broker_position_id)
        if bp is None:
            log.info("  cTrader CLOSE: broker pos #%d уже закрыта", pos.broker_position_id)
            pnl = pos.profit_pips * pip_value_usd(pos.instrument, settings.lot_size)
            killswitch.record_trade_close(pnl)
            continue

        vol = bp.tradeData.volume if hasattr(bp, "tradeData") else 0
        if not vol:
            log.warning("  cTrader CLOSE: не удалось определить volume для #%d", pos.broker_position_id)
            continue

        result = executor.close_position(pos.broker_position_id, vol)

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
