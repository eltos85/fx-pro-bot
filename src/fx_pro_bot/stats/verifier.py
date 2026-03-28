"""Автоматическая проверка сигналов: через N минут сравнивает цену с ценой на момент сигнала."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance
from fx_pro_bot.stats.store import StatsStore

log = logging.getLogger(__name__)


def _fetch_current_price(symbol: str) -> float | None:
    """Последняя цена close для инструмента (1 свеча за 1 минуту)."""
    try:
        bars = bars_from_yfinance(symbol, period="1d", interval="1m")
        if bars:
            return bars[-1].close
    except Exception:
        log.warning("Не удалось получить цену для %s", symbol)
    return None


def _calc_profit_pips(direction: str, entry_price: float, current_price: float, symbol: str) -> float:
    ps = pip_size(symbol)
    if direction == "long":
        return round((current_price - entry_price) / ps, 1)
    elif direction == "short":
        return round((entry_price - current_price) / ps, 1)
    return 0.0


def run_verification(store: StatsStore, horizons: tuple[int, ...]) -> int:
    """Проверяет все созревшие сигналы. Возвращает количество новых верификаций."""
    now = datetime.now(tz=UTC)
    verified_count = 0
    price_cache: dict[str, float | None] = {}

    for horizon in horizons:
        pending = store.pending_for_verification(horizon, now)
        if not pending:
            continue

        for suggestion in pending:
            symbol = suggestion.instrument
            if suggestion.price_at_signal is None:
                continue

            if symbol not in price_cache:
                price_cache[symbol] = _fetch_current_price(symbol)
            current_price = price_cache[symbol]
            if current_price is None:
                continue

            profit = _calc_profit_pips(
                suggestion.direction,
                suggestion.price_at_signal,
                current_price,
                symbol,
            )
            verdict = "right" if profit > 0 else "wrong"

            store.record_verification(
                suggestion_id=suggestion.id,
                horizon_minutes=horizon,
                price_at_check=current_price,
                profit_pips=profit,
                verdict=verdict,
            )

            log.info(
                "Проверка [%dм] %s %s: вход %.5f → сейчас %.5f → %+.1f пунктов %s",
                horizon,
                display_name(symbol),
                suggestion.direction.upper(),
                suggestion.price_at_signal,
                current_price,
                profit,
                "✓" if verdict == "right" else "✗",
            )
            verified_count += 1

    return verified_count
