"""Советник: котировки (stub или yfinance) → сигнал → простой текст → учёт в статистике. Без сделок и без входа в кабинет брокера."""

from __future__ import annotations

import logging
import sys

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.signals import TrendDirection, simple_ma_crossover
from fx_pro_bot.config.settings import Settings
from fx_pro_bot.events import events_near, events_to_json_blob, load_events
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.market_data.stub_feed import generate_stub_bars
from fx_pro_bot.stats.store import StatsStore


def _load_bars(settings: Settings) -> list[Bar]:
    src = settings.data_source.strip().lower()
    if src == "stub":
        return generate_stub_bars(settings.yfinance_symbol, n=200, timeframe_sec=3600)
    if src == "yfinance":
        try:
            from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance
        except ImportError as e:
            msg = "Установите: pip install 'fx-pro-bot[quotes]' или pip install yfinance"
            raise RuntimeError(msg) from e
        bars = bars_from_yfinance(
            settings.yfinance_symbol,
            period=settings.yfinance_period,
            interval=settings.yfinance_interval,
        )
        if not bars:
            msg = "yfinance не вернул данных — проверьте тикер (YFINANCE_SYMBOL) и сеть."
            raise RuntimeError(msg)
        return bars
    msg = f"Неизвестный DATA_SOURCE={settings.data_source!r}, ожидается stub или yfinance"
    raise ValueError(msg)


def run_advisor() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )

    store = StatsStore(settings.stats_db_path)
    events = load_events(settings.events_calendar_path)

    try:
        bars = _load_bars(settings)
    except (RuntimeError, ValueError) as e:
        logging.error("%s", e)
        sys.exit(1)

    instrument = bars[-1].instrument.symbol
    window: list[Bar] = []
    max_w = 80
    last_direction: TrendDirection | None = None

    for bar in bars:
        window.append(bar)
        if len(window) > max_w:
            window = window[-max_w:]
        signal = simple_ma_crossover(window, fast=10, slow=30)
        if signal.direction == last_direction:
            continue
        last_direction = signal.direction

        ev_now = events_near(events, now=bar.ts, within_hours=48.0, min_importance="medium")
        text = advice_for_signal(
            display_name=settings.display_name,
            signal=signal,
            last_price=bar.close,
            nearby_events=ev_now,
        )
        logging.info("— совет —\n%s", text)
        store.record_suggestion(
            instrument=instrument,
            direction=signal.direction.value,
            advice_text=text,
            reasons=signal.reasons,
            price_at_signal=bar.close,
            events_context=events_to_json_blob(ev_now) if ev_now else None,
        )

    summ = store.summary()
    logging.info(
        "Статистика в базе: всего записей=%s, оценено=%s, верно=%s, неверно=%s, точность=%s",
        summ["total"],
        summ["judged"],
        summ["right"],
        summ["wrong"],
        summ["accuracy"],
    )


def main() -> None:
    run_advisor()


if __name__ == "__main__":
    main()
