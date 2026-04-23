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
from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
from bybit_bot.strategies.scalping.crypto_overbought_fader import CryptoOverboughtFaderStrategy
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
# Pair TP снижен 2.00 → 1.00 (2026-04-21): за Wave 5 (6 пар) порог $2 не
# сработал ни разу, max pair uPnL был ~$1.12. Тюнинг "мёртвого" порога —
# не изменение логики, не curve-fitting. Подробнее см. BUILDLOG_BYBIT.md.
STATARB_PAIR_TP_USD = 1.00  # take-profit по суммарному uPnL пары (комиссии пары ~$0.70 → нетто ~$0.30)

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

    if settings.scalping_cof_verbose:
        logging.getLogger(
            "bybit_bot.strategies.scalping.crypto_overbought_fader"
        ).setLevel(logging.DEBUG)
        log.info("COF verbose: DEBUG-логи включены для crypto_overbought_fader")

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
    scalp_orb = _build_scalp_orb(settings) if settings.scalping_orb_enabled else None
    scalp_turtle = TurtleSoupStrategy() if settings.scalping_turtle_enabled else None
    scalp_leadlag = BtcLeadLagStrategy() if settings.scalping_leadlag_enabled else None
    scalp_cof = CryptoOverboughtFaderStrategy() if settings.scalping_cof_enabled else None
    scalp_cof_symbols = _parse_csv_env(settings.scalping_cof_symbols)

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
                    enabled=settings.killswitch_enabled,
                ),
                initial_equity=settings.account_balance,
            )
            if not settings.killswitch_enabled:
                log.warning("KillSwitch ОТКЛЮЧЁН (BYBIT_BOT_KS_ENABLED=false)")
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
                scalp_leadlag=scalp_leadlag,
                scalp_cof=scalp_cof,
                scalp_cof_symbols=scalp_cof_symbols,
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
    scalp_leadlag: BtcLeadLagStrategy | None,
    scalp_cof: CryptoOverboughtFaderStrategy | None = None,
    scalp_cof_symbols: set[str] | None = None,
    client: BybitClient | None,
    executor: TradeExecutor | None,
    killswitch: KillSwitch | None,
    tradeable_symbols: set[str] | None = None,
) -> None:
    now = datetime.now(tz=UTC)
    log.info("─── Цикл %d │ %s ───", cycle, now.strftime("%H:%M:%S UTC"))

    # Batch-загрузка: один запрос yfinance.download() вместо 38 отдельных.
    # Если включён BTC Lead-Lag — добавляем reference-символ (BTC) в список загрузки,
    # даже если он не в scan_symbols (BTC используется только как ЛИДЕР, не торгуется).
    fetch_symbols = list(settings.scan_symbols)
    ref_symbol = settings.leadlag_reference_symbol
    if scalp_leadlag and ref_symbol not in fetch_symbols:
        fetch_symbols.append(ref_symbol)
    bars_map = fetch_bars_batch(
        tuple(fetch_symbols),
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
    if scalp_cof and client:
        _update_htf_slopes(scalp_cof, client, settings)

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
        scalp_leadlag=scalp_leadlag,
        scalp_cof=scalp_cof,
        scalp_cof_symbols=scalp_cof_symbols,
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


_PNL_RECONCILE_SLEEP_SEC = 2.0


def _close_only(client: BybitClient, db_pos: PositionRow) -> bool:
    """Отправить market+reduceOnly close для позиции, без ожидания PnL.

    Возвращает True если ордер принят биржей. Real-PnL подтягивается
    отдельно через `_reconcile_close` после небольшой паузы.
    """
    result = client.close_position(db_pos.symbol, db_pos.side, db_pos.qty)
    if not result.success:
        log.error("Не удалось закрыть %s: %s", db_pos.symbol, result.message)
        return False
    return True


def _reconcile_close(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    db_pos: PositionRow,
    api_pnl: float,
    reason: str,
) -> None:
    """Подтянуть real-PnL из API и записать в БД + KillSwitch.

    Должна вызываться ПОСЛЕ паузы (~2с) чтобы Bybit closed-pnl API успел
    отдать запись. Без паузы fetch_realized_pnl часто возвращает пусто и
    мы фоллбэчимся на uPnL, теряя точность.
    """
    since_ms = int((datetime.now(tz=UTC) - timedelta(minutes=5)).timestamp() * 1000)
    real_pnl = api_pnl
    exit_price = 0.0
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


def _close_and_record(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    db_pos: PositionRow,
    api_pnl: float,
    reason: str,
) -> bool:
    """Закрыть одну позицию синхронно (legacy-обёртка над _close_only + reconcile).

    Используется только в случаях, где нужно закрыть строго одну позицию
    (например, time-stop одиночного scalp_vwap). Для пакетных закрытий
    (stat-arb пары, batch exit одиночных) использовать
    `_close_batch_with_reconcile` — он экономит до 3-6 секунд за счёт
    параллельного исполнения и единого sleep.
    """
    if not _close_only(client, db_pos):
        return False
    time.sleep(_PNL_RECONCILE_SLEEP_SEC)
    _reconcile_close(client, stats, killswitch, db_pos, api_pnl, reason)
    return True


def _close_batch_with_reconcile(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    items: list[tuple[PositionRow, float, str]],
) -> set[str]:
    """Параллельно закрыть несколько позиций и единым sleep подтянуть real-PnL.

    items: список (db_pos, api_pnl, reason).
    Возвращает: set символов которые успешно закрылись (для already_closed).

    Стратегия:
    1. Шлём все market+reduceOnly параллельно через ThreadPool — gap <500мс.
    2. Один sleep на все ноги (вместо N × 1.5с).
    3. Последовательный fetch_realized_pnl + запись в БД для каждой ноги.

    Если одна нога зафейлится при отправке — остальные всё равно идут,
    повисшие позиции попадут в обычный sync_pending механизм.
    """
    if not items:
        return set()

    legs = [(it[0].symbol, it[0].side, it[0].qty) for it in items]
    results = client.close_positions_parallel(legs)

    submitted: list[tuple[PositionRow, float, str]] = []
    for (db_pos, api_pnl, reason), order_result in zip(items, results, strict=True):
        if order_result.success:
            submitted.append((db_pos, api_pnl, reason))
        else:
            log.error("Не удалось закрыть %s: %s", db_pos.symbol, order_result.message)

    if not submitted:
        return set()

    time.sleep(_PNL_RECONCILE_SLEEP_SEC)

    closed: set[str] = set()
    for db_pos, api_pnl, reason in submitted:
        _reconcile_close(client, stats, killswitch, db_pos, api_pnl, reason)
        closed.add(db_pos.symbol)
    return closed


def _close_pair_legs(
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    pair_tag: str,
    exclude_symbol: str,
    api_positions: dict[str, object],
) -> None:
    """Закрыть все ноги Stat-Arb пары кроме уже закрытого символа.

    Использует _close_batch_with_reconcile для синхронной отправки, чтобы
    при экстренном закрытии второй ноги не было дополнительного gap.
    """
    pair_positions = stats.get_open_by_pair_tag(pair_tag)
    items: list[tuple[PositionRow, float, str]] = []
    for pp in pair_positions:
        if pp.symbol == exclude_symbol:
            continue
        api_pos = api_positions.get(pp.symbol)
        pnl = api_pos.unrealised_pnl if api_pos else 0.0
        items.append((pp, pnl, "pair_close"))
    _close_batch_with_reconcile(client, stats, killswitch, items)


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

    # 1. Stat-Arb z-score exit (батч-параллель: gap между ногами <500мс)
    if scalp_statarb and bars_map:
        open_pair_tags = stats.get_open_pair_tags()
        if open_pair_tags:
            tags_to_close = scalp_statarb.check_exits(bars_map, open_pair_tags)
            for tag in tags_to_close:
                pair_positions = stats.get_open_by_pair_tag(tag)
                items: list[tuple[PositionRow, float, str]] = []
                for pp in pair_positions:
                    if pp.symbol in already_closed:
                        continue
                    api_pos = api_map.get(pp.symbol)
                    pnl = api_pos.unrealised_pnl if api_pos else 0.0
                    items.append((pp, pnl, "statarb_zscore_exit"))
                if items:
                    log.info("STAT-ARB EXIT: %s → закрываю %d ног параллельно", tag, len(items))
                    closed_now = _close_batch_with_reconcile(client, stats, killswitch, items)
                    already_closed |= closed_now

    # 1b. Stat-Arb pair take-profit: суммарный uPnL пары >= порог (батч-параллель)
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
            items = []
            for pp in pair_positions:
                if pp.symbol in already_closed:
                    continue
                pp_pnl = api_map[pp.symbol].unrealised_pnl if pp.symbol in api_map else 0.0
                items.append((pp, pp_pnl, "statarb_pair_tp"))
            if items:
                closed_now = _close_batch_with_reconcile(client, stats, killswitch, items)
                already_closed |= closed_now

    now = datetime.now(tz=UTC)

    # Собираем все одиночные закрытия (time-stop, emergency) в батч,
    # чтобы потом одним вызовом параллельно закрыть и единым sleep
    # подтянуть real-PnL. Это сокращает общее время exit-обработки
    # с N × 6с до ~3с независимо от N.
    single_items: list[tuple[PositionRow, float, str]] = []
    pair_legs_to_close: list[tuple[str, str]] = []  # (pair_tag, exclude_symbol)

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

        # 2. Stat-Arb emergency: суммарный uPnL пары < -$25 (батч-параллель)
        if db_pos.pair_tag and db_pos.strategy == "scalp_statarb":
            pair_positions = stats.get_open_by_pair_tag(db_pos.pair_tag)
            pair_upnl = sum(
                api_map[pp.symbol].unrealised_pnl
                for pp in pair_positions
                if pp.symbol in api_map
            )
            if pair_upnl <= -STATARB_EMERGENCY_LOSS:
                log.warning("STAT-ARB EMERGENCY: pair %s uPnL=%.2f", db_pos.pair_tag, pair_upnl)
                items: list[tuple[PositionRow, float, str]] = []
                for pp in pair_positions:
                    if pp.symbol in already_closed:
                        continue
                    pp_pnl = api_map[pp.symbol].unrealised_pnl if pp.symbol in api_map else 0.0
                    items.append((pp, pp_pnl, "statarb_emergency"))
                if items:
                    closed_now = _close_batch_with_reconcile(client, stats, killswitch, items)
                    already_closed |= closed_now
                continue

        # 3. Time-stop 24ч: убирает "dead money" трейды (Finaur/TrendRider 2026)
        if age_sec >= TIME_STOP_SECONDS:
            log.info("TIME-STOP: %s held %.0f min (limit %.0f min) uPnL=%.2f",
                     db_pos.symbol, age_min, TIME_STOP_SECONDS / 60, upnl)
            single_items.append((db_pos, upnl, "time_stop"))
            already_closed.add(db_pos.symbol)
            if db_pos.pair_tag:
                pair_legs_to_close.append((db_pos.pair_tag, db_pos.symbol))
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

    # Финальный батч: одиночные закрытия (time-stop) + парные ноги
    # которые нужно закрыть симметрично с time-stop.
    if single_items:
        _close_batch_with_reconcile(client, stats, killswitch, single_items)

    for pair_tag, exclude_symbol in pair_legs_to_close:
        _close_pair_legs(client, stats, killswitch, pair_tag, exclude_symbol, api_map)

    closed_count = len(already_closed)
    if closed_count > 0:
        log.info("Exit-проверка: закрыто %d позиций", closed_count)


def _update_htf_slopes(
    strategy: VwapCryptoStrategy | CryptoOverboughtFaderStrategy,
    client: BybitClient,
    settings: Settings,
) -> None:
    """Загрузить 1h бары и рассчитать EMA(50) slope для HTF-фильтра.

    Работает для любой страты с методом `set_htf_slopes(dict[str, float])`
    (VWAP, COF).
    """
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
    strategy.set_htf_slopes(slopes)
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
    scalp_leadlag: BtcLeadLagStrategy | None,
    scalp_cof: CryptoOverboughtFaderStrategy | None = None,
    scalp_cof_symbols: set[str] | None = None,
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
        "scalp_orb", "scalp_turtle", "scalp_leadlag", "scalp_cof",
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

    if scalp_cof and bars_map:
        # Опциональный whitelist символов через BYBIT_BOT_SCALP_COF_SYMBOLS
        cof_whitelist = scalp_cof_symbols or set()
        cof_bars_map = (
            {s: bars for s, bars in bars_map.items() if s in cof_whitelist}
            if cof_whitelist else bars_map
        )
        for cs in scalp_cof.scan(cof_bars_map):
            if cs.symbol in open_symbols:
                continue
            sig = Signal(
                direction=cs.direction, strength=0.8,
                reasons=(
                    f"cof_dev={cs.deviation_atr:.1f}ATR "
                    f"turtle_depth={cs.turtle_depth_atr:.2f}ATR "
                    f"rsi={cs.rsi:.0f} atr%={cs.atr_pct:.2f}",
                ),
                sl_atr_mult=1.5, tp_atr_mult=2.5,
                strategy_name="scalp_cof",
            )
            scalp_trades.append((cs.symbol, sig, bars_map[cs.symbol], "scalp_cof"))

    if scalp_leadlag and bars_map:
        ref_symbol = settings.leadlag_reference_symbol
        for ll in scalp_leadlag.scan(bars_map):
            if ll.symbol in open_symbols:
                continue
            # Reference-символ (BTC) — только ЛИДЕР, НЕ торгуем его.
            if ll.symbol == ref_symbol:
                continue
            sig = Signal(
                direction=ll.direction, strength=0.7,
                reasons=(f"leadlag_btc={ll.btc_move_pct:+.2f}% corr={ll.correlation:.2f}",),
                sl_atr_mult=1.5, tp_atr_mult=2.0,
                strategy_name="scalp_leadlag",
            )
            scalp_trades.append((ll.symbol, sig, bars_map[ll.symbol], "scalp_leadlag"))

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


def _parse_csv_env(value: str) -> set[str] | None:
    """CSV-строка из env → set. Пустая строка → None (без ограничений)."""
    cleaned = [v.strip() for v in value.split(",") if v.strip()]
    return set(cleaned) if cleaned else None


def _build_scalp_orb(settings: Settings) -> SessionOrbStrategy:
    """SessionOrbStrategy с whitelist-фильтрами из env (backtest 90д 2026-04-23)."""
    sessions = _parse_csv_env(settings.scalping_orb_sessions)
    symbols = _parse_csv_env(settings.scalping_orb_symbols)
    direction = settings.scalping_orb_direction.strip().lower() or None
    if direction is not None and direction not in ("long", "short"):
        log.warning("BYBIT_BOT_SCALP_ORB_DIRECTION=%r невалидно, игнорирую", direction)
        direction = None
    return SessionOrbStrategy(
        allowed_sessions=sessions,
        allowed_symbols=symbols,
        allowed_direction=direction,
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
        # Показываем ORB-фильтры, если они заданы (backtest-обоснованные).
        orb_parts = ["ORB"]
        if settings.scalping_orb_sessions:
            orb_parts.append(f"sess={settings.scalping_orb_sessions}")
        if settings.scalping_orb_symbols:
            orb_parts.append(f"syms={settings.scalping_orb_symbols}")
        if settings.scalping_orb_direction:
            orb_parts.append(f"dir={settings.scalping_orb_direction}")
        active.append("/".join(orb_parts) if len(orb_parts) > 1 else "ORB")
    if settings.scalping_turtle_enabled:
        active.append("Turtle")
    if settings.scalping_leadlag_enabled:
        active.append(f"LeadLag(ref={settings.leadlag_reference_symbol})")
    if settings.scalping_cof_enabled:
        cof_parts = ["COF"]
        if settings.scalping_cof_symbols:
            cof_parts.append(f"syms={settings.scalping_cof_symbols}")
        active.append("/".join(cof_parts))
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
