"""Bybit Crypto Bot — точка входа и главный цикл.

Стратегии:
- Momentum (ансамбль 5 индикаторов)
- Скальпинг: VWAP reversion, Stat-Arb, Funding Rate, Volume Spike
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

from bybit_bot.analysis.scanner import ScanResult, active_signals, scan_instruments
from bybit_bot.analysis.signals import Direction, atr as compute_atr
from bybit_bot.config.settings import Settings, display_name
from bybit_bot.market_data.feed import fetch_bars, fetch_bars_batch
from bybit_bot.market_data.models import Bar
from bybit_bot.stats.store import PositionRow, StatsStore
from bybit_bot.strategies.momentum import MomentumStrategy
from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
from bybit_bot.trading.client import BybitClient, InstrumentInfo
from bybit_bot.trading.executor import TradeExecutor
from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig

TIME_STOP_SECONDS = 50 * 300  # 50 баров × 5 мин = 15000 сек (~4.2 часа)
TRAILING_ACTIVATION_ATR = 0.7
TRAILING_DISTANCE_ATR = 0.5
STATARB_EMERGENCY_LOSS = 15.0
STATARB_PAIR_TP_USD = 0.80  # take-profit по суммарному uPnL пары

log = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def run_bot() -> None:
    settings = Settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("=" * 60)
    log.info("Bybit Crypto Bot запущен")
    log.info("Demo: %s | Category: %s", settings.demo, settings.category)
    log.info("Символы: %s", ", ".join(settings.scan_symbols))
    log.info("Торговля: %s", "ВКЛЮЧЕНА" if settings.trading_enabled else "только сигналы")
    _log_scalping_config(settings)
    log.info("=" * 60)

    stats = StatsStore(settings.stats_db_path)
    momentum = MomentumStrategy(min_votes=settings.min_ensemble_votes)

    scalp_vwap = VwapCryptoStrategy(
        max_positions=settings.scalping_max_positions,
    ) if settings.scalping_vwap_enabled else None

    scalp_statarb = StatArbCryptoStrategy() if settings.scalping_statarb_enabled else None
    scalp_volume = VolumeSpikeStrategy() if settings.scalping_volume_enabled else None

    client: BybitClient | None = None
    executor: TradeExecutor | None = None
    killswitch: KillSwitch | None = None
    scalp_funding: FundingScalpStrategy | None = None

    if settings.trading_enabled and settings.api_key and settings.api_secret:
        try:
            client = BybitClient(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                demo=settings.demo,
                category=settings.category,
            )
            instruments = client.get_instruments(settings.scan_symbols)
            tradeable_symbols = tuple(s for s in settings.scan_symbols if s in instruments)
            skipped = set(settings.scan_symbols) - set(tradeable_symbols)
            if skipped:
                log.warning("Символы НЕ доступны на Bybit (%s): %s",
                            "demo" if settings.demo else "live", ", ".join(sorted(skipped)))
            log.info("Торгуемые символы: %d/%d", len(tradeable_symbols),
                     len(tradeable_symbols) + len(skipped))

            executor = TradeExecutor(client, settings, instruments)
            killswitch = KillSwitch(
                KillSwitchConfig(
                    max_daily_loss_usd=settings.killswitch_max_daily_loss,
                    max_drawdown_pct=settings.killswitch_max_drawdown_pct,
                    max_positions=settings.killswitch_max_positions,
                    max_loss_per_trade_usd=settings.killswitch_max_loss_per_trade,
                ),
                initial_equity=settings.account_balance,
            )
            if settings.scalping_funding_enabled:
                scalp_funding = FundingScalpStrategy(client)
            balance = client.get_balance()
            log.info(
                "Bybit баланс: equity=%.2f, available=%.2f, uPnL=%.2f",
                balance.total_equity, balance.available_balance, balance.unrealised_pnl,
            )
        except Exception:
            log.exception("Не удалось подключиться к Bybit API")
            client = None
            executor = None
    else:
        log.info("Торговля отключена — работаю в режиме сигналов")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cycle = 0
    while not _shutdown:
        cycle += 1
        try:
            _run_cycle(
                cycle=cycle,
                settings=settings,
                stats=stats,
                momentum=momentum,
                scalp_vwap=scalp_vwap,
                scalp_statarb=scalp_statarb,
                scalp_funding=scalp_funding,
                scalp_volume=scalp_volume,
                client=client,
                executor=executor,
                killswitch=killswitch,
            )
        except Exception:
            log.exception("Ошибка в цикле %d", cycle)

        if not _shutdown:
            log.info("Следующий цикл через %d сек...", settings.poll_interval_sec)
            _sleep_interruptible(settings.poll_interval_sec)

    log.info("Bybit Crypto Bot остановлен")


def _run_cycle(
    *,
    cycle: int,
    settings: Settings,
    stats: StatsStore,
    momentum: MomentumStrategy,
    scalp_vwap: VwapCryptoStrategy | None,
    scalp_statarb: StatArbCryptoStrategy | None,
    scalp_funding: FundingScalpStrategy | None,
    scalp_volume: VolumeSpikeStrategy | None,
    client: BybitClient | None,
    executor: TradeExecutor | None,
    killswitch: KillSwitch | None,
) -> None:
    now = datetime.now(tz=UTC)
    log.info("─── Цикл %d │ %s ───", cycle, now.strftime("%H:%M:%S UTC"))

    # Batch-загрузка: один запрос yfinance.download() вместо 38 отдельных
    bars_map = fetch_bars_batch(
        settings.scan_symbols,
        period=settings.yfinance_period,
        interval=settings.yfinance_interval,
    )

    # Momentum: ансамбль → сигналы (используем уже загруженные бары)
    scan = scan_instruments(
        settings.scan_symbols,
        period=settings.yfinance_period,
        interval=settings.yfinance_interval,
        min_votes=settings.min_ensemble_votes,
        bars_map=bars_map,
    )
    signals = active_signals(scan)
    _log_scan_results(scan, signals)

    for sr in signals:
        stats.log_signal(
            symbol=sr.symbol,
            direction=sr.signal.direction.value,
            strength=sr.signal.strength,
            reasons=", ".join(sr.signal.reasons),
            price=sr.last_price,
        )

    if not executor or not client or not killswitch:
        log.info("Торговля отключена — только сигналы")
        return

    _process_exits(
        client=client,
        stats=stats,
        killswitch=killswitch,
        settings=settings,
        bars_map=bars_map,
        scalp_statarb=scalp_statarb,
    )

    _process_momentum(
        signals=signals,
        settings=settings,
        stats=stats,
        momentum=momentum,
        client=client,
        executor=executor,
        killswitch=killswitch,
    )

    _process_scalping(
        bars_map=bars_map,
        settings=settings,
        stats=stats,
        client=client,
        executor=executor,
        killswitch=killswitch,
        scalp_vwap=scalp_vwap,
        scalp_statarb=scalp_statarb,
        scalp_funding=scalp_funding,
        scalp_volume=scalp_volume,
        cycle_counter=cycle,
    )


def _fetch_entry_price(client: BybitClient, symbol: str, fallback: float) -> float:
    """Получить реальную цену входа (avgPrice) из Bybit API после открытия позиции."""
    try:
        positions = client.get_positions()
        for p in positions:
            if p.symbol == symbol:
                return p.entry_price
    except Exception:
        log.warning("Не удалось получить entry_price для %s из API", symbol)
    return fallback


def _close_and_record(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    db_pos: PositionRow,
    api_pnl: float,
    reason: str,
) -> bool:
    """Закрыть позицию на бирже и записать результат в БД + KillSwitch.

    После закрытия ордера запрашивает реальный PnL из Bybit closed-pnl API
    (включая комиссии и проскальзывание). Если API не вернул данные —
    фоллбэк на unrealisedPnl.
    """
    since_ms = int((datetime.now(tz=UTC) - timedelta(minutes=5)).timestamp() * 1000)
    result = client.close_position(db_pos.symbol, db_pos.side, db_pos.qty)
    if not result.success:
        log.error("Не удалось закрыть %s: %s", db_pos.symbol, result.message)
        return False

    real_pnl = api_pnl
    exit_price = 0.0
    time.sleep(0.5)
    cpnl = client.fetch_realized_pnl(db_pos.symbol, since_ms)
    if cpnl:
        real_pnl = float(cpnl["closedPnl"])
        exit_price = float(cpnl.get("avgExitPrice", 0))
        log.info("REAL PnL %s: closedPnl=%.4f (uPnL was %.4f), exit=%.4f",
                 db_pos.symbol, real_pnl, api_pnl, exit_price)
    else:
        log.warning("Не удалось получить closed-pnl для %s, используем uPnL=%.4f",
                     db_pos.symbol, api_pnl)

    stats.close_position(db_pos.id, exit_price=exit_price, pnl_usd=real_pnl, close_reason=reason)
    killswitch.record_trade_close(real_pnl)
    log.info("ЗАКРЫТА: %s %s pnl=%.4f [%s]", db_pos.side, db_pos.symbol, real_pnl, reason)
    return True


def _close_pair_legs(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    pair_tag: str,
    exclude_symbol: str,
    api_positions: dict[str, object],
) -> None:
    """Закрыть все ноги Stat-Arb пары кроме уже закрытого символа."""
    pair_positions = stats.get_open_by_pair_tag(pair_tag)
    for pp in pair_positions:
        if pp.symbol == exclude_symbol:
            continue
        api_pos = api_positions.get(pp.symbol)
        pnl = api_pos.unrealised_pnl if api_pos else 0.0
        _close_and_record(client, stats, killswitch, pp, pnl, "pair_close")


def _process_exits(
    *,
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    settings: Settings,
    bars_map: dict[str, list[Bar]],
    scalp_statarb: StatArbCryptoStrategy | None,
) -> None:
    """Проверить открытые позиции и закрыть по условиям exit-логики."""
    try:
        api_positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения позиций для exit-проверки")
        return

    api_map = {p.symbol: p for p in api_positions}
    db_open = stats.get_open_positions()

    if not db_open:
        return

    # Синхронизация: закрыть в БД позиции, которых уже нет на бирже.
    # Подтягиваем реальный PnL из Bybit closed-pnl API.
    for db_pos in db_open:
        if db_pos.symbol not in api_map:
            real_pnl = 0.0
            exit_price = 0.0
            try:
                opened_dt = datetime.fromisoformat(db_pos.opened_at)
                if opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=UTC)
                since_ms = int(opened_dt.timestamp() * 1000)
            except (ValueError, TypeError):
                since_ms = int((datetime.now(tz=UTC) - timedelta(hours=12)).timestamp() * 1000)

            cpnl = client.fetch_realized_pnl(db_pos.symbol, since_ms)
            if cpnl:
                real_pnl = float(cpnl["closedPnl"])
                exit_price = float(cpnl.get("avgExitPrice", 0))
                log.info("SYNC %s: real PnL=%.4f exit=%.4f (from API)",
                         db_pos.symbol, real_pnl, exit_price)
            else:
                log.warning("SYNC %s: closed-pnl не найден в API, pnl=0", db_pos.symbol)

            stats.close_position(db_pos.id, exit_price=exit_price, pnl_usd=real_pnl,
                                 close_reason="sync_closed")

    db_open = [p for p in db_open if p.symbol in api_map]
    if not db_open:
        return

    already_closed: set[str] = set()
    trailing_set: set[str] = set()

    # 1. Stat-Arb z-score exit
    if scalp_statarb and bars_map:
        open_pair_tags = stats.get_open_pair_tags()
        if open_pair_tags:
            tags_to_close = scalp_statarb.check_exits(bars_map, open_pair_tags)
            for tag in tags_to_close:
                pair_positions = stats.get_open_by_pair_tag(tag)
                for pp in pair_positions:
                    if pp.symbol in already_closed:
                        continue
                    api_pos = api_map.get(pp.symbol)
                    pnl = api_pos.unrealised_pnl if api_pos else 0.0
                    if _close_and_record(client, stats, killswitch, pp, pnl, "statarb_zscore_exit"):
                        already_closed.add(pp.symbol)

    # 1b. Stat-Arb pair take-profit: суммарный uPnL пары >= порог
    checked_tags: set[str] = set()
    for db_pos in db_open:
        tag = db_pos.pair_tag
        if not tag or tag in checked_tags or db_pos.symbol in already_closed:
            continue
        checked_tags.add(tag)
        pair_positions = stats.get_open_by_pair_tag(tag)
        pair_upnl = sum(
            api_map[pp.symbol].unrealised_pnl
            for pp in pair_positions
            if pp.symbol in api_map
        )
        if pair_upnl >= STATARB_PAIR_TP_USD:
            log.info("PAIR-TP: %s суммарный uPnL=$%.2f >= $%.2f, фиксирую прибыль",
                     tag, pair_upnl, STATARB_PAIR_TP_USD)
            for pp in pair_positions:
                if pp.symbol in already_closed:
                    continue
                pp_pnl = api_map[pp.symbol].unrealised_pnl if pp.symbol in api_map else 0.0
                if _close_and_record(client, stats, killswitch, pp, pp_pnl, "statarb_pair_tp"):
                    already_closed.add(pp.symbol)

    now = datetime.now(tz=UTC)

    for db_pos in db_open:
        if db_pos.symbol in already_closed:
            continue

        api_pos = api_map.get(db_pos.symbol)
        if not api_pos:
            continue

        upnl = api_pos.unrealised_pnl

        try:
            opened_dt = datetime.fromisoformat(db_pos.opened_at)
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=UTC)
            age_sec = (now - opened_dt).total_seconds()
        except (ValueError, TypeError):
            age_sec = 0.0
        age_min = age_sec / 60

        log.info(
            "EXIT-CHECK: %s %s uPnL=%.2f age=%.0fmin strat=%s pair=%s",
            db_pos.side, db_pos.symbol, upnl, age_min,
            db_pos.strategy, db_pos.pair_tag or "-",
        )

        # 2. KillSwitch: max_loss_per_trade
        if upnl <= -killswitch._config.max_loss_per_trade_usd:
            log.warning(
                "MAX_LOSS_PER_TRADE: %s uPnL=%.2f < -%.2f",
                db_pos.symbol, upnl, killswitch._config.max_loss_per_trade_usd,
            )
            if _close_and_record(client, stats, killswitch, db_pos, upnl, "max_loss_per_trade"):
                already_closed.add(db_pos.symbol)
                if db_pos.pair_tag:
                    _close_pair_legs(client, stats, killswitch, db_pos.pair_tag, db_pos.symbol, api_map)
                    for pp in stats.get_open_by_pair_tag(db_pos.pair_tag):
                        already_closed.add(pp.symbol)
            continue

        # 3. Stat-Arb emergency: суммарный uPnL пары < -$15
        if db_pos.pair_tag and db_pos.strategy == "scalp_statarb":
            pair_positions = stats.get_open_by_pair_tag(db_pos.pair_tag)
            pair_upnl = sum(
                api_map[pp.symbol].unrealised_pnl
                for pp in pair_positions
                if pp.symbol in api_map
            )
            if pair_upnl <= -STATARB_EMERGENCY_LOSS:
                log.warning("STAT-ARB EMERGENCY: pair %s uPnL=%.2f", db_pos.pair_tag, pair_upnl)
                for pp in pair_positions:
                    if pp.symbol in already_closed:
                        continue
                    pp_pnl = api_map[pp.symbol].unrealised_pnl if pp.symbol in api_map else 0.0
                    if _close_and_record(client, stats, killswitch, pp, pp_pnl, "statarb_emergency"):
                        already_closed.add(pp.symbol)
                continue

        # 4. Time-stop по реальному времени (~4.2 часа)
        if age_sec >= TIME_STOP_SECONDS:
            log.info("TIME-STOP: %s held %.0f min (limit %.0f min)",
                     db_pos.symbol, age_min, TIME_STOP_SECONDS / 60)
            if _close_and_record(client, stats, killswitch, db_pos, upnl, "time_stop"):
                already_closed.add(db_pos.symbol)
                if db_pos.pair_tag:
                    _close_pair_legs(client, stats, killswitch, db_pos.pair_tag, db_pos.symbol, api_map)
            continue

        # 5. Trailing stop: при прибыли > 0.7 ATR подтянуть через Bybit API (один раз за цикл)
        if upnl > 0 and not db_pos.pair_tag and db_pos.symbol not in trailing_set:
            bars = bars_map.get(db_pos.symbol, [])
            if bars:
                atr_val = compute_atr(bars)
                size = float(db_pos.qty)
                if size > 0 and atr_val > 0:
                    profit_in_atr = upnl / (atr_val * size)
                    if profit_in_atr >= TRAILING_ACTIVATION_ATR:
                        distance = atr_val * TRAILING_DISTANCE_ATR
                        client.set_trailing_stop(db_pos.symbol, distance)
                        trailing_set.add(db_pos.symbol)

    closed_count = len(already_closed)
    if closed_count > 0:
        log.info("Exit-проверка: закрыто %d позиций", closed_count)


def _process_momentum(
    *,
    signals: list[ScanResult],
    settings: Settings,
    stats: StatsStore,
    momentum: MomentumStrategy,
    client: BybitClient,
    executor: TradeExecutor,
    killswitch: KillSwitch,
) -> None:
    try:
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения данных Bybit")
        return

    effective_equity = settings.account_balance + stats.get_cumulative_pnl()
    if not killswitch.check_allowed(len(positions), effective_equity):
        if killswitch.is_tripped:
            log.critical("KillSwitch сработал: %s — закрываю все позиции!", killswitch.trip_reason)
            client.close_all_positions()
        return

    open_symbols = {p.symbol for p in positions}

    for sr in signals:
        if sr.symbol in open_symbols:
            continue

        trade_signal = momentum.evaluate(sr.symbol, sr.bars)
        if trade_signal is None:
            continue

        executor.set_leverage(sr.symbol)

        params = executor.compute_trade(
            sr.symbol,
            sr.signal,
            sr.bars,
            balance.available_balance,
        )
        if params is None:
            continue

        result = executor.execute(params)
        if result.success:
            entry = _fetch_entry_price(client, params.symbol, sr.last_price)
            stats.open_position(
                symbol=params.symbol,
                side=params.side,
                qty=params.qty,
                entry_price=entry,
                order_id=result.order_id,
                sl=params.sl,
                tp=params.tp,
                strategy="momentum",
                signal_strength=sr.signal.strength,
                signal_reasons=", ".join(sr.signal.reasons),
            )
            log.info(
                "ОТКРЫТА: %s %s %s qty=%s entry=%.4f SL=%.4f TP=%.4f",
                params.side, display_name(params.symbol), params.symbol,
                params.qty, entry, params.sl or 0, params.tp or 0,
            )


def _process_scalping(
    *,
    bars_map: dict[str, list[Bar]],
    settings: Settings,
    stats: StatsStore,
    client: BybitClient,
    executor: TradeExecutor,
    killswitch: KillSwitch,
    scalp_vwap: VwapCryptoStrategy | None,
    scalp_statarb: StatArbCryptoStrategy | None,
    scalp_funding: FundingScalpStrategy | None,
    scalp_volume: VolumeSpikeStrategy | None,
    cycle_counter: int = 0,
) -> None:
    """Исполнение скальпинг-сигналов: открытие позиций на Bybit."""
    try:
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения данных Bybit для скальпинга")
        return

    open_symbols = {p.symbol for p in positions}
    scalp_strategies = {"scalp_vwap", "scalp_statarb", "scalp_funding", "scalp_volume"}
    db_open = stats.get_open_positions()
    scalp_opened = sum(1 for dp in db_open if dp.strategy in scalp_strategies)

    if scalp_opened >= settings.scalping_max_positions:
        log.debug("Скальпинг: макс позиций (%d/%d)", scalp_opened, settings.scalping_max_positions)
        return

    from bybit_bot.analysis.signals import Signal

    scalp_trades: list[tuple[str, Signal, list[Bar], str]] = []

    if scalp_vwap and bars_map:
        for vs in scalp_vwap.scan(bars_map):
            if vs.symbol not in open_symbols:
                sig = Signal(
                    direction=vs.direction, strength=0.7,
                    reasons=(f"vwap_dev={vs.deviation_atr:.1f}",),
                    sl_atr_mult=2.0, tp_atr_mult=1.5, strategy_name="scalp_vwap",
                )
                scalp_trades.append((vs.symbol, sig, bars_map[vs.symbol], "scalp_vwap"))

    if scalp_statarb and bars_map:
        for sa in scalp_statarb.scan(bars_map):
            if sa.symbol_a not in open_symbols:
                sig = Signal(
                    direction=sa.direction_a, strength=0.7,
                    reasons=(f"statarb_z={sa.z_score:.2f}",),
                    sl_atr_mult=None, tp_atr_mult=None,
                    pair_tag=sa.pair_tag, strategy_name="scalp_statarb",
                )
                scalp_trades.append((sa.symbol_a, sig, bars_map[sa.symbol_a], "scalp_statarb"))
            if sa.symbol_b not in open_symbols:
                sig = Signal(
                    direction=sa.direction_b, strength=0.7,
                    reasons=(f"statarb_z={sa.z_score:.2f}",),
                    sl_atr_mult=None, tp_atr_mult=None,
                    pair_tag=sa.pair_tag, strategy_name="scalp_statarb",
                )
                scalp_trades.append((sa.symbol_b, sig, bars_map[sa.symbol_b], "scalp_statarb"))

    if scalp_funding and bars_map:
        for fs in scalp_funding.scan(settings.scan_symbols, bars_map):
            if fs.symbol not in open_symbols:
                sig = Signal(
                    direction=fs.direction, strength=fs.strength,
                    reasons=(f"funding={fs.funding_rate:.4f}%",),
                    sl_atr_mult=1.5, tp_atr_mult=1.0, strategy_name="scalp_funding",
                )
                scalp_trades.append((fs.symbol, sig, bars_map.get(fs.symbol, []), "scalp_funding"))

    if scalp_volume and bars_map:
        for vs in scalp_volume.scan(bars_map):
            if vs.symbol not in open_symbols:
                sig = Signal(
                    direction=vs.direction, strength=0.8,
                    reasons=(f"vol_spike={vs.volume_ratio:.1f}x",),
                    sl_atr_mult=2.0, tp_atr_mult=2.0, strategy_name="scalp_volume",
                )
                scalp_trades.append((vs.symbol, sig, bars_map[vs.symbol], "scalp_volume"))

    log.info("Скальпинг: найдено %d сигналов (max позиций=%d, открыто=%d)",
             len(scalp_trades), settings.scalping_max_positions, scalp_opened)

    effective_equity = settings.account_balance + stats.get_cumulative_pnl()
    for symbol, sig, bars, strategy in scalp_trades:
        if not killswitch.check_allowed(len(positions), effective_equity):
            if killswitch.is_tripped:
                log.critical("KillSwitch: %s — закрываю все позиции!", killswitch.trip_reason)
                client.close_all_positions()
            break

        if not bars:
            continue

        executor.set_leverage(symbol)
        params = executor.compute_trade(symbol, sig, bars, balance.available_balance)
        if params is None:
            continue

        result = executor.execute(params)
        if result.success:
            entry = _fetch_entry_price(client, params.symbol, bars[-1].close)
            stats.open_position(
                symbol=params.symbol,
                side=params.side,
                qty=params.qty,
                entry_price=entry,
                order_id=result.order_id,
                sl=params.sl,
                tp=params.tp,
                strategy=strategy,
                signal_strength=sig.strength,
                signal_reasons=", ".join(sig.reasons),
                pair_tag=sig.pair_tag or "",
                opened_bar_idx=cycle_counter,
            )
            open_symbols.add(symbol)
            log.info(
                "СКАЛЬП ОТКРЫТ: %s %s %s qty=%s entry=%.4f [%s]",
                params.side, display_name(symbol), symbol, params.qty, entry, strategy,
            )


def _log_scalping_config(settings: Settings) -> None:
    active = []
    if settings.scalping_vwap_enabled:
        active.append("VWAP")
    if settings.scalping_statarb_enabled:
        active.append("StatArb")
    if settings.scalping_funding_enabled:
        active.append("Funding")
    if settings.scalping_volume_enabled:
        active.append("VolSpike")
    log.info("Скальпинг: %s", ", ".join(active) if active else "отключён")


def _log_scan_results(scan: list[ScanResult], signals: list[ScanResult]) -> None:
    log.info("Просканировано %d инструментов, сигналов: %d", len(scan), len(signals))
    for sr in signals:
        arrow = "▲" if sr.signal.direction == Direction.LONG else "▼"
        log.info(
            "  %s %s %s (сила %.0f%%) @ %.4f │ %s",
            arrow,
            sr.signal.direction.value.upper(),
            display_name(sr.symbol),
            sr.signal.strength * 100,
            sr.last_price,
            ", ".join(sr.signal.reasons),
        )


def _sleep_interruptible(seconds: int) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and not _shutdown:
        time.sleep(1)


def main() -> None:
    try:
        run_bot()
    except KeyboardInterrupt:
        log.info("Прервано пользователем")
    sys.exit(0)


if __name__ == "__main__":
    main()
