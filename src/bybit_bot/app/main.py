"""Bybit Crypto Bot V2 — точка входа и главный цикл.

Стратегия: EMA Trend-Following на 1h таймфрейме.
Данные: Bybit API klines (без yfinance).
Ордера: Limit PostOnly (maker 0.02%) с fallback на Market.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

from bybit_bot.analysis.indicators import atr as compute_atr
from bybit_bot.config.settings import Settings, display_name
from bybit_bot.market_data.feed import fetch_bars_batch_bybit
from bybit_bot.market_data.models import Bar
from bybit_bot.stats.store import PositionRow, StatsStore
from bybit_bot.strategies.trend_ema import EmaTrendStrategy
from bybit_bot.trading.client import BybitClient
from bybit_bot.trading.executor import TradeExecutor
from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig

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
    log.info("Bybit Crypto Bot V2 — EMA Trend-Following")
    log.info("Demo: %s | Category: %s", settings.demo, settings.category)
    log.info("Символы: %s", ", ".join(settings.scan_symbols))
    log.info("Таймфрейм: %s | Цикл: %d сек", settings.kline_interval, settings.poll_interval_sec)
    log.info("Leverage: %dx | Max позиций: %d", settings.leverage, settings.max_positions)
    log.info("Торговля: %s", "ВКЛЮЧЕНА" if settings.trading_enabled else "только сигналы")
    log.info("=" * 60)

    stats = StatsStore(settings.stats_db_path)

    strategy = EmaTrendStrategy(
        fast_period=settings.ema_fast,
        slow_period=settings.ema_slow,
        trend_period=settings.ema_trend,
        adx_threshold=settings.adx_threshold,
        volume_ratio=settings.volume_filter_ratio,
        pullback_pct=settings.pullback_pct,
        sl_atr_mult=settings.sl_atr_mult,
        tp_atr_mult=settings.tp_atr_mult,
    )

    client: BybitClient | None = None
    executor: TradeExecutor | None = None
    killswitch: KillSwitch | None = None
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
                    max_loss_per_trade_usd=settings.killswitch_max_loss_per_trade,
                ),
                initial_equity=settings.account_balance,
            )
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
                strategy=strategy,
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

    log.info("Bybit Crypto Bot V2 остановлен")


def _run_cycle(
    *,
    cycle: int,
    settings: Settings,
    stats: StatsStore,
    strategy: EmaTrendStrategy,
    client: BybitClient | None,
    executor: TradeExecutor | None,
    killswitch: KillSwitch | None,
    tradeable_symbols: set[str] | None = None,
) -> None:
    now = datetime.now(tz=UTC)
    log.info("─── Цикл %d │ %s ───", cycle, now.strftime("%H:%M:%S UTC"))

    if not client:
        log.info("Нет подключения к Bybit — пропускаю цикл")
        return

    bars_map = fetch_bars_batch_bybit(
        client,
        settings.scan_symbols,
        interval=settings.kline_interval,
        limit=settings.kline_limit,
    )

    if not bars_map:
        log.warning("Нет данных klines — пропускаю цикл")
        return

    if not executor or not killswitch:
        log.info("Торговля отключена — только мониторинг")
        return

    _process_exits(
        client=client,
        stats=stats,
        killswitch=killswitch,
        settings=settings,
        bars_map=bars_map,
    )

    _process_entries(
        bars_map=bars_map,
        settings=settings,
        stats=stats,
        strategy=strategy,
        client=client,
        executor=executor,
        killswitch=killswitch,
        tradeable_symbols=tradeable_symbols,
    )


def _fetch_entry_price(client: BybitClient, symbol: str, fallback: float) -> float:
    """Получить реальную цену входа из Bybit API после открытия позиции."""
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
    """Закрыть позицию на бирже и записать результат."""
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


def _process_exits(
    *,
    client: BybitClient,
    stats: StatsStore,
    killswitch: KillSwitch,
    settings: Settings,
    bars_map: dict[str, list[Bar]],
) -> None:
    """Проверить открытые позиции и закрыть по exit-условиям."""
    try:
        api_positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения позиций для exit-проверки")
        return

    api_map = {p.symbol: p for p in api_positions}
    db_open = stats.get_open_positions()

    if not db_open:
        return

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
        age_hours = age_sec / 3600

        log.info(
            "EXIT-CHECK: %s %s uPnL=%.2f age=%.1fh",
            db_pos.side, db_pos.symbol, upnl, age_hours,
        )

        # 1. KillSwitch: max_loss_per_trade
        if upnl <= -killswitch._config.max_loss_per_trade_usd:
            log.warning(
                "MAX_LOSS_PER_TRADE: %s uPnL=%.2f < -%.2f",
                db_pos.symbol, upnl, killswitch._config.max_loss_per_trade_usd,
            )
            if _close_and_record(client, stats, killswitch, db_pos, upnl, "max_loss_per_trade"):
                already_closed.add(db_pos.symbol)
            continue

        # 2. Time-stop: 48 часовых свечей = 48 часов без +1%
        time_stop_sec = settings.time_stop_bars * 3600
        if age_sec >= time_stop_sec:
            entry = db_pos.entry_price
            pnl_pct = upnl / (float(db_pos.qty) * entry) * 100 if entry > 0 else 0
            if pnl_pct < 1.0:
                log.info("TIME-STOP: %s held %.1fh (limit %dh), pnl=%.1f%%",
                         db_pos.symbol, age_hours, settings.time_stop_bars, pnl_pct)
                if _close_and_record(client, stats, killswitch, db_pos, upnl, "time_stop"):
                    already_closed.add(db_pos.symbol)
                continue

        # 3. Trailing stop: при прибыли > 1.5 ATR подтянуть через Bybit API
        if upnl > 0 and db_pos.symbol not in trailing_set:
            bars = bars_map.get(db_pos.symbol, [])
            if bars:
                atr_val = compute_atr(bars)
                size = float(db_pos.qty)
                if size > 0 and atr_val > 0:
                    profit_in_atr = upnl / (atr_val * size)
                    if profit_in_atr >= settings.trailing_activation_atr:
                        distance = atr_val * settings.trailing_distance_atr
                        client.set_trailing_stop(db_pos.symbol, distance)
                        trailing_set.add(db_pos.symbol)
                        log.info("TRAILING: %s activated, distance=%.4f (%.1f ATR)",
                                 db_pos.symbol, distance, settings.trailing_distance_atr)

    closed_count = len(already_closed)
    if closed_count > 0:
        log.info("Exit-проверка: закрыто %d позиций", closed_count)


def _process_entries(
    *,
    bars_map: dict[str, list[Bar]],
    settings: Settings,
    stats: StatsStore,
    strategy: EmaTrendStrategy,
    client: BybitClient,
    executor: TradeExecutor,
    killswitch: KillSwitch,
    tradeable_symbols: set[str] | None = None,
) -> None:
    """Сканировать сигналы и открыть позиции."""
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

    tradeable_bars = {
        sym: bars for sym, bars in bars_map.items()
        if sym not in open_symbols and (not tradeable_symbols or sym in tradeable_symbols)
    }

    signals = strategy.scan(tradeable_bars, open_symbols)

    log.info("Стратегия: %d сигналов из %d символов (открыто %d/%d позиций)",
             len(signals), len(tradeable_bars), len(open_symbols), settings.max_positions)

    if len(open_symbols) >= settings.max_positions:
        log.info("Макс позиций (%d) — новые входы заблокированы", settings.max_positions)
        return

    for sig in signals:
        if len(open_symbols) >= settings.max_positions:
            break

        if not killswitch.check_allowed(len(positions), effective_equity):
            if killswitch.is_tripped:
                log.critical("KillSwitch: %s — закрываю все позиции!", killswitch.trip_reason)
                client.close_all_positions()
            break

        executor.set_leverage(sig.symbol)
        params = executor.compute_trade(sig, balance.available_balance)
        if params is None:
            continue

        result = executor.execute(params)
        if result.success:
            entry = _fetch_entry_price(client, params.symbol, sig.price)
            stats.open_position(
                symbol=params.symbol,
                side=params.side,
                qty=params.qty,
                entry_price=entry,
                order_id=result.order_id,
                sl=params.sl,
                tp=params.tp,
                strategy="trend_ema_v2",
                signal_strength=0.8,
                signal_reasons=", ".join(sig.reasons),
            )
            open_symbols.add(sig.symbol)
            log.info(
                "ОТКРЫТА: %s %s %s qty=%s entry=%.4f SL=%.4f TP=%.4f | %s",
                params.side, display_name(sig.symbol), sig.symbol,
                params.qty, entry, params.sl or 0, params.tp or 0,
                ", ".join(sig.reasons),
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
