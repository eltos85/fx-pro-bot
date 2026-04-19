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
from bybit_bot.market_data.feed import fetch_bars_batch
from bybit_bot.market_data.models import Bar
from bybit_bot.stats.store import PositionRow, StatsStore
from bybit_bot.strategies.momentum import MomentumStrategy
from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
from bybit_bot.trading.client import BybitClient, InstrumentInfo
from bybit_bot.trading.executor import TradeExecutor
from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig

TIME_STOP_SECONDS = 24 * 3600  # 24 часа (Finaur/TrendRider 2026: time-exit убирает dead money)
TRAILING_ACTIVATION_ATR = 0.7
TRAILING_DISTANCE_ATR = 0.5
STATARB_EMERGENCY_LOSS = 25.0
STATARB_PAIR_TP_USD = 2.00  # take-profit по суммарному uPnL пары (с запасом на комиссии ~$0.70)

log = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _sync_positions_on_startup(client: BybitClient, stats: StatsStore) -> None:
    """При старте: восстановить в БД позиции, открытые на бирже но потерянные ботом.

    Все позиции с биржи добавляются в БД (даже если символ не в scan_symbols),
    чтобы exit-логика (trailing, time-stop) могла ими управлять.
    """
    try:
        api_positions = client.get_positions()
    except Exception:
        log.exception("Не удалось получить позиции при старте")
        return

    if not api_positions:
        return

    db_open = stats.get_open_positions()
    db_symbols = {p.symbol for p in db_open}

    recovered = 0
    for ap in api_positions:
        if ap.symbol in db_symbols:
            continue
        stats.open_position(
            symbol=ap.symbol,
            side=ap.side,
            qty=ap.size,
            entry_price=ap.entry_price,
            order_id="recovered_on_startup",
            strategy="recovered",
        )
        recovered += 1
        log.warning(
            "RECOVERED: %s %s qty=%s entry=%.4f uPnL=%.2f (не было в БД)",
            ap.side, ap.symbol, ap.size, ap.entry_price, ap.unrealised_pnl,
        )

    if recovered:
        log.info("Синхронизация при старте: восстановлено %d позиций", recovered)


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
    log.info("Momentum: %s", "ВКЛ" if settings.momentum_enabled else "ОТКЛ")
    _log_scalping_config(settings)
    log.info("=" * 60)

    stats = StatsStore(settings.stats_db_path)
    momentum = MomentumStrategy(min_votes=settings.min_ensemble_votes) if settings.momentum_enabled else None

    scalp_vwap = VwapCryptoStrategy() if settings.scalping_vwap_enabled else None

    scalp_statarb = StatArbCryptoStrategy() if settings.scalping_statarb_enabled else None
    scalp_volume = VolumeSpikeStrategy() if settings.scalping_volume_enabled else None
    scalp_orb = SessionOrbStrategy() if settings.scalping_orb_enabled else None
    scalp_turtle = TurtleSoupStrategy() if settings.scalping_turtle_enabled else None

    client: BybitClient | None = None
    executor: TradeExecutor | None = None
    killswitch: KillSwitch | None = None
    scalp_funding: FundingScalpStrategy | None = None
    tradeable_symbols: tuple[str, ...] = ()

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

    if client and stats:
        _sync_positions_on_startup(client, stats)

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
                scalp_orb=scalp_orb,
                scalp_turtle=scalp_turtle,
                client=client,
                executor=executor,
                killswitch=killswitch,
                tradeable_symbols=set(tradeable_symbols) if client else set(),
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
    momentum: MomentumStrategy | None,
    scalp_vwap: VwapCryptoStrategy | None,
    scalp_statarb: StatArbCryptoStrategy | None,
    scalp_funding: FundingScalpStrategy | None,
    scalp_volume: VolumeSpikeStrategy | None,
    scalp_orb: SessionOrbStrategy | None,
    scalp_turtle: TurtleSoupStrategy | None,
    client: BybitClient | None,
    executor: TradeExecutor | None,
    killswitch: KillSwitch | None,
    tradeable_symbols: set[str] | None = None,
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

    _reconcile_pending_sync(client=client, stats=stats, killswitch=killswitch)

    _process_exits(
        client=client,
        stats=stats,
        killswitch=killswitch,
        settings=settings,
        bars_map=bars_map,
        scalp_statarb=scalp_statarb,
    )

    if momentum:
        _process_momentum(
            signals=signals,
            settings=settings,
            stats=stats,
            momentum=momentum,
            client=client,
            executor=executor,
            killswitch=killswitch,
        )

    if scalp_vwap and client:
        _update_htf_slopes(scalp_vwap, client, settings)

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
        scalp_orb=scalp_orb,
        scalp_turtle=scalp_turtle,
        cycle_counter=cycle,
        tradeable_symbols=tradeable_symbols,
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
    time.sleep(1.5)
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


def _reconcile_pending_sync(
    *,
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
) -> None:
    """Дозаполнить реальный PnL для позиций со статусом sync_pending.

    Закрытия, где closed-pnl API сразу не успел отдать запись, откладываются
    с close_reason='sync_pending'. Здесь повторно запрашиваем API не ранее чем
    через 30 секунд и обновляем pnl_usd + close_reason='sync_closed'. Если
    трижды подряд не удалось — пометим sync_orphan (закрыт вручную / истёк).
    """
    pending = stats.get_pending_sync_positions(older_than_sec=30)
    if not pending:
        return

    for db_pos in pending:
        try:
            opened_dt = datetime.fromisoformat(db_pos.opened_at)
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=UTC)
            since_ms = int(opened_dt.timestamp() * 1000)
        except (ValueError, TypeError):
            since_ms = int((datetime.now(tz=UTC) - timedelta(hours=24)).timestamp() * 1000)

        cpnl = client.fetch_realized_pnl(db_pos.symbol, since_ms, retries=1, retry_delay_sec=0)
        if not cpnl:
            closed_age_min = 0.0
            try:
                if db_pos.closed_at:
                    closed_dt = datetime.fromisoformat(db_pos.closed_at)
                    if closed_dt.tzinfo is None:
                        closed_dt = closed_dt.replace(tzinfo=UTC)
                    closed_age_min = (datetime.now(tz=UTC) - closed_dt).total_seconds() / 60
            except (ValueError, TypeError):
                pass
            # Если прошло >30 минут и API всё ещё пусто — закрываем как orphan
            if closed_age_min > 30:
                stats.update_closed_pnl(db_pos.id, exit_price=0.0, pnl_usd=0.0,
                                        close_reason="sync_orphan")
                log.warning("RECONCILE %s: closed >%.0fмин, API пусто → sync_orphan",
                            db_pos.symbol, closed_age_min)
            continue

        real_pnl = float(cpnl["closedPnl"])
        exit_price = float(cpnl.get("avgExitPrice", 0))
        stats.update_closed_pnl(db_pos.id, exit_price=exit_price, pnl_usd=real_pnl,
                                close_reason="sync_closed")
        killswitch.record_trade_close(real_pnl)
        log.info("RECONCILE %s: real PnL=%.4f exit=%.4f (from API, was sync_pending)",
                 db_pos.symbol, real_pnl, exit_price)


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

    # API race guard: иногда get_positions() возвращает неполный список
    # (пропадают свежеоткрытые позиции). Если в db_open есть символы не из api_map —
    # сделаем повторный запрос, и только позиции, отсутствующие ДВАЖДЫ, считаем
    # реально закрытыми на бирже. Без этого бот фантомно закрывает живые позиции
    # как sync_orphan (pnl=0) и теряет над ними контроль (time-stop не работает).
    missing = [p.symbol for p in db_open if p.symbol not in api_map]
    if missing:
        try:
            api_positions_2 = client.get_positions()
            api_map_2 = {p.symbol: p for p in api_positions_2}
        except Exception:
            log.exception("API race guard: повторный get_positions() не удался")
            api_map_2 = {}

        recovered = [s for s in missing if s in api_map_2]
        if recovered:
            log.warning("SYNC race-guard: %d позиций восстановились во втором запросе: %s",
                        len(recovered), recovered)
            for sym in recovered:
                api_map[sym] = api_map_2[sym]

    # Синхронизация: закрыть в БД позиции, которых уже нет на бирже.
    # Подтягиваем реальный PnL из Bybit closed-pnl API.
    # Если API не вернул запись (закрытие <1-2с назад) — помечаем позицию
    # close_reason="sync_pending" и возвращаемся к ней в следующем цикле.
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
                stats.close_position(db_pos.id, exit_price=exit_price, pnl_usd=real_pnl,
                                     close_reason="sync_closed")
            else:
                log.warning("SYNC %s: closed-pnl API пусто — помечаем sync_pending, повтор в след. цикле",
                            db_pos.symbol)
                stats.close_position(db_pos.id, exit_price=0.0, pnl_usd=0.0,
                                     close_reason="sync_pending")

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

        # 2. Stat-Arb emergency: суммарный uPnL пары < -$25
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

        # 3. Time-stop 24ч: убирает "dead money" трейды (Finaur/TrendRider 2026)
        if age_sec >= TIME_STOP_SECONDS:
            log.info("TIME-STOP: %s held %.0f min (limit %.0f min) uPnL=%.2f",
                     db_pos.symbol, age_min, TIME_STOP_SECONDS / 60, upnl)
            if _close_and_record(client, stats, killswitch, db_pos, upnl, "time_stop"):
                already_closed.add(db_pos.symbol)
                if db_pos.pair_tag:
                    _close_pair_legs(client, stats, killswitch, db_pos.pair_tag, db_pos.symbol, api_map)
            continue

        # 4. Trailing stop: при прибыли > 0.7 ATR подтянуть через Bybit API (один раз за цикл)
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


def _update_htf_slopes(
    scalp_vwap: VwapCryptoStrategy,
    client: BybitClient,
    settings: Settings,
) -> None:
    """Загрузить 1h бары через Bybit API и рассчитать EMA(50) slope для VWAP HTF фильтра."""
    from bybit_bot.analysis.signals import ema
    from bybit_bot.strategies.scalping.indicators import ema_slope

    slopes: dict[str, float] = {}
    for symbol in settings.scan_symbols:
        try:
            raw = client.get_kline(symbol, interval="60", limit=60)
            if len(raw) < 55:
                continue
            closes = [float(r[4]) for r in raw]
            ema_vals = ema(closes, 50)
            slopes[symbol] = ema_slope(ema_vals, 5)
        except Exception:
            log.debug("HTF kline %s: ошибка загрузки", symbol)
    scalp_vwap.set_htf_slopes(slopes)
    log.debug("HTF slopes: %d символов", len(slopes))


def _in_trading_session(settings: Settings) -> bool:
    """Проверить что текущее время UTC в разрешённом окне для входов."""
    if not settings.session_filter_enabled:
        return True
    hour = datetime.now(tz=UTC).hour
    return settings.session_start_utc <= hour < settings.session_end_utc


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
    if not _in_trading_session(settings):
        log.info("Momentum: вне торговой сессии (%02d:00-%02d:00 UTC), входы заблокированы",
                 settings.session_start_utc, settings.session_end_utc)
        return

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
    scalp_orb: SessionOrbStrategy | None,
    scalp_turtle: TurtleSoupStrategy | None,
    cycle_counter: int = 0,
    tradeable_symbols: set[str] | None = None,
) -> None:
    """Исполнение скальпинг-сигналов: открытие позиций на Bybit."""
    if not _in_trading_session(settings):
        log.info("Скальпинг: вне торговой сессии (%02d:00-%02d:00 UTC), входы заблокированы",
                 settings.session_start_utc, settings.session_end_utc)
        return

    try:
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения данных Bybit для скальпинга")
        return

    open_symbols = {p.symbol for p in positions}
    scalp_strategies = {
        "scalp_vwap", "scalp_statarb", "scalp_funding", "scalp_volume",
        "scalp_orb", "scalp_turtle",
    }
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
            if sa.symbol_a in open_symbols or sa.symbol_b in open_symbols:
                continue
            if tradeable_symbols and (sa.symbol_a not in tradeable_symbols or sa.symbol_b not in tradeable_symbols):
                log.debug("Stat-Arb %s/%s: символ недоступен на бирже, пропускаю",
                          sa.symbol_a, sa.symbol_b)
                continue
            sig_a = Signal(
                direction=sa.direction_a, strength=0.7,
                reasons=(f"statarb_z={sa.z_score:.2f}",),
                sl_atr_mult=None, tp_atr_mult=None,
                pair_tag=sa.pair_tag, strategy_name="scalp_statarb",
            )
            sig_b = Signal(
                direction=sa.direction_b, strength=0.7,
                reasons=(f"statarb_z={sa.z_score:.2f}",),
                sl_atr_mult=None, tp_atr_mult=None,
                pair_tag=sa.pair_tag, strategy_name="scalp_statarb",
            )
            scalp_trades.append((sa.symbol_a, sig_a, bars_map[sa.symbol_a], "scalp_statarb"))
            scalp_trades.append((sa.symbol_b, sig_b, bars_map[sa.symbol_b], "scalp_statarb"))

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

    if scalp_orb and bars_map:
        for orb in scalp_orb.scan(bars_map):
            if orb.symbol in open_symbols:
                continue
            # TP привязан к размеру коробки: TP_BOX_MULT × box_range в ATR.
            tp_atr_mult = (orb.box_range * 2.0) / orb.atr_value if orb.atr_value > 0 else 2.0
            tp_atr_mult = max(1.0, min(tp_atr_mult, 4.0))
            sig = Signal(
                direction=orb.direction, strength=0.75,
                reasons=(f"orb_{orb.session}_vol={orb.volume_ratio:.1f}x",),
                sl_atr_mult=2.0, tp_atr_mult=tp_atr_mult,
                strategy_name="scalp_orb",
            )
            scalp_trades.append((orb.symbol, sig, bars_map[orb.symbol], "scalp_orb"))

    if scalp_turtle and bars_map:
        for ts in scalp_turtle.scan(bars_map):
            if ts.symbol in open_symbols:
                continue
            sig = Signal(
                direction=ts.direction, strength=0.7,
                reasons=(f"turtle_depth={ts.break_depth_atr:.2f}ATR rsi={ts.rsi_at_break:.0f}",),
                sl_atr_mult=1.5, tp_atr_mult=2.5,
                strategy_name="scalp_turtle",
            )
            scalp_trades.append((ts.symbol, sig, bars_map[ts.symbol], "scalp_turtle"))

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
    if settings.scalping_orb_enabled:
        active.append("ORB")
    if settings.scalping_turtle_enabled:
        active.append("Turtle")
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
