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
from datetime import UTC, datetime

from bybit_bot.analysis.scanner import ScanResult, active_signals, scan_instruments
from bybit_bot.analysis.signals import Direction
from bybit_bot.config.settings import Settings, display_name
from bybit_bot.market_data.feed import fetch_bars, fetch_bars_batch
from bybit_bot.market_data.models import Bar
from bybit_bot.stats.store import StatsStore
from bybit_bot.strategies.momentum import MomentumStrategy
from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
from bybit_bot.trading.client import BybitClient, InstrumentInfo
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
            valid_symbols = tuple(s for s in settings.scan_symbols if s in instruments)
            skipped = set(settings.scan_symbols) - set(valid_symbols)
            if skipped:
                log.warning("Символы НЕ доступны на Bybit (%s): %s",
                            "demo" if settings.demo else "live", ", ".join(sorted(skipped)))
            settings.scan_symbols = valid_symbols
            log.info("Торгуемые символы: %d/%d", len(valid_symbols),
                     len(valid_symbols) + len(skipped))

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
    )


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

    if not killswitch.check_allowed(len(positions), balance.total_equity):
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
            stats.open_position(
                symbol=params.symbol,
                side=params.side,
                qty=params.qty,
                entry_price=sr.last_price,
                order_id=result.order_id,
                sl=params.sl,
                tp=params.tp,
                strategy="momentum",
                signal_strength=sr.signal.strength,
                signal_reasons=", ".join(sr.signal.reasons),
            )
            log.info(
                "ОТКРЫТА: %s %s %s qty=%s SL=%.4f TP=%.4f",
                params.side, display_name(params.symbol), params.symbol,
                params.qty, params.sl or 0, params.tp or 0,
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
) -> None:
    """Исполнение скальпинг-сигналов: открытие позиций на Bybit."""
    try:
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception:
        log.exception("Ошибка получения данных Bybit для скальпинга")
        return

    open_symbols = {p.symbol for p in positions}
    scalp_opened = sum(1 for p in positions if getattr(p, "strategy", "") in
                       ("scalp_vwap", "scalp_statarb", "scalp_funding", "scalp_volume"))

    if scalp_opened >= settings.scalping_max_positions:
        log.debug("Скальпинг: макс позиций (%d/%d)", scalp_opened, settings.scalping_max_positions)
        return

    from bybit_bot.analysis.signals import Signal

    scalp_trades: list[tuple[str, Signal, list[Bar], str]] = []

    if scalp_vwap and bars_map:
        for vs in scalp_vwap.scan(bars_map):
            if vs.symbol not in open_symbols:
                sig = Signal(direction=vs.direction, strength=0.7, reasons=(f"vwap_dev={vs.deviation_atr:.1f}",))
                scalp_trades.append((vs.symbol, sig, bars_map[vs.symbol], "scalp_vwap"))

    if scalp_statarb and bars_map:
        for sa in scalp_statarb.scan(bars_map):
            if sa.symbol_a not in open_symbols:
                sig = Signal(direction=sa.direction_a, strength=0.7, reasons=(f"statarb_z={sa.z_score:.2f}",))
                scalp_trades.append((sa.symbol_a, sig, bars_map[sa.symbol_a], "scalp_statarb"))
            if sa.symbol_b not in open_symbols:
                sig = Signal(direction=sa.direction_b, strength=0.7, reasons=(f"statarb_z={sa.z_score:.2f}",))
                scalp_trades.append((sa.symbol_b, sig, bars_map[sa.symbol_b], "scalp_statarb"))

    if scalp_funding and bars_map:
        for fs in scalp_funding.scan(settings.scan_symbols, bars_map):
            if fs.symbol not in open_symbols:
                sig = Signal(direction=fs.direction, strength=fs.strength, reasons=(f"funding={fs.funding_rate:.4f}%",))
                scalp_trades.append((fs.symbol, sig, bars_map.get(fs.symbol, []), "scalp_funding"))

    if scalp_volume and bars_map:
        for vs in scalp_volume.scan(bars_map):
            if vs.symbol not in open_symbols:
                sig = Signal(direction=vs.direction, strength=0.8, reasons=(f"vol_spike={vs.volume_ratio:.1f}x",))
                scalp_trades.append((vs.symbol, sig, bars_map[vs.symbol], "scalp_volume"))

    log.info("Скальпинг: найдено %d сигналов (max позиций=%d, открыто=%d)",
             len(scalp_trades), settings.scalping_max_positions, scalp_opened)

    for symbol, sig, bars, strategy in scalp_trades:
        if not killswitch.check_allowed(len(positions), balance.total_equity):
            if killswitch.is_tripped:
                log.critical("KillSwitch: %s — стоп", killswitch.trip_reason)
            break

        if not bars:
            continue

        executor.set_leverage(symbol)
        params = executor.compute_trade(symbol, sig, bars, balance.available_balance)
        if params is None:
            continue

        result = executor.execute(params)
        if result.success:
            stats.open_position(
                symbol=params.symbol,
                side=params.side,
                qty=params.qty,
                entry_price=bars[-1].close,
                order_id=result.order_id,
                sl=params.sl,
                tp=params.tp,
                strategy=strategy,
                signal_strength=sig.strength,
                signal_reasons=", ".join(sig.reasons),
            )
            open_symbols.add(symbol)
            log.info(
                "СКАЛЬП ОТКРЫТ: %s %s %s qty=%s [%s]",
                params.side, display_name(symbol), symbol, params.qty, strategy,
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
