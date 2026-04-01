"""Сканер-советник: непрерывный цикл — сканирует инструменты, даёт советы, проверяет старые сигналы."""

from __future__ import annotations

import logging
import time

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.scanner import active_signals, scan_instruments
from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.config.settings import Settings, display_name, pip_value_usd, spread_cost_pips
from fx_pro_bot.copytrading.ctrader import CTraderCopyClient, format_top_strategies
from fx_pro_bot.events import events_near, events_to_json_blob, load_events
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.stats.verifier import run_verification
from fx_pro_bot.whales.tracker import WhaleTracker

log = logging.getLogger(__name__)

CTRADER_POLL_CYCLES = 12
WHALE_POLL_CYCLES = 6


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
        "  💰 Счёт $%.0f, лот %.2f → брутто $%+.2f, комиссии -$%.2f, чистыми $%+.2f (%+.1f%%)",
        balance, lot, gross_usd, spread_usd, net_usd,
        (net_usd / balance * 100) if balance else 0,
    )


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
    cycle_count = 0

    log.info(
        "Запуск сканера v0.4: ансамбль 5 стратегий + whale-трекер, %d инструментов, "
        "интервал %s, проверка через %s мин, цикл %d сек",
        len(settings.scan_symbols),
        settings.yfinance_interval,
        ",".join(str(h) for h in settings.verify_horizons),
        settings.poll_interval_sec,
    )

    while True:
        try:
            _run_cycle(
                settings, store, events, last_directions,
                ctrader_client, whale_tracker, cycle_count,
            )
            cycle_count += 1
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            break
        except Exception:
            log.exception("Ошибка в цикле сканера, повтор через %d сек", settings.poll_interval_sec)

        time.sleep(settings.poll_interval_sec)


def _run_cycle(
    settings: Settings,
    store: StatsStore,
    events: tuple,
    last_directions: dict[str, TrendDirection],
    ctrader_client: CTraderCopyClient,
    whale_tracker: WhaleTracker,
    cycle_count: int,
) -> None:
    log.info("── Сканирование (ансамбль 5 стратегий) ──")

    results = scan_instruments(
        settings.scan_symbols,
        period=settings.yfinance_period,
        interval=settings.yfinance_interval,
    )

    active = active_signals(results)

    if not active:
        log.info("Активных сигналов нет, стратегии не пришли к согласию")
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
            strategies = ", ".join(r for r in r.signal.reasons if not r[0].isdigit() and "/" not in r)
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

    log.info("── Проверка старых сигналов ──")
    verified = run_verification(store, settings.verify_horizons)
    if verified:
        log.info("Проверено %d сигналов", verified)
    else:
        log.info("Нет созревших сигналов для проверки")

    if cycle_count % WHALE_POLL_CYCLES == 0:
        try:
            whale_tracker.run()
        except Exception:
            log.exception("Ошибка whale-трекера")

    log.info("── Статистика ──")
    _log_stats(store, settings.verify_horizons, settings)
    whale_tracker.log_whale_stats()

    if cycle_count % CTRADER_POLL_CYCLES == 0:
        _log_ctrader_top(ctrader_client)


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
