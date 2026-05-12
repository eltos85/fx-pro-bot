"""Paper-mode reconcile для FX AI Trader.

В paper-mode позиции живут в БД без реального ордера на cTrader.
Реальный broker не отрабатывает SL/TP — это делаем мы по последним M1
свечам:
- За каждый closed-период (с opened_at до now) тянем M1 свечи.
- Проверяем для каждой свечи touch SL или TP по логике LONG/SHORT.
- При touch → закрываем позицию в БД с exit_price = SL или TP.

В live-mode эта функция вызывается тоже, но фильтрует только paper-позиции.

NB: на cTrader paper-симуляция всё равно полагается на ОФИЦИАЛЬНЫЕ
M1 свечи broker'а — это уменьшает «фантомные» touch'и из-за spreads/wicks
yfinance. Также честно учитываем weekend gaps (между Fri close и Mon open
свечей нет → бары просто пропускают этот промежуток).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.client_adapter import Bar, CTraderFxAdapter
from fx_ai_trader.trading.executor import _calc_pnl_usd, _pip_size_for

log = logging.getLogger(__name__)


def _touched(
    bar: Bar,
    side: str,
    sl: float | None,
    tp: float | None,
) -> tuple[str, float] | None:
    """Проверяет, тронул ли бар уровень SL/TP. Возвращает (reason, exit_price) или None.

    LONG (BUY): SL ниже entry → low ≤ SL → stop. TP выше entry → high ≥ TP → tp.
    SHORT (SELL): SL выше entry → high ≥ SL → stop. TP ниже entry → low ≤ TP → tp.

    Если БАР пробил И SL И TP одновременно (gap-day) — предпочитаем SL
    (консервативная оценка: assume worst execution).
    """
    side_up = side.upper()
    if side_up == "BUY":
        if sl is not None and bar.low <= sl:
            return ("sl_hit", sl)
        if tp is not None and bar.high >= tp:
            return ("tp_hit", tp)
    elif side_up == "SELL":
        if sl is not None and bar.high >= sl:
            return ("sl_hit", sl)
        if tp is not None and bar.low <= tp:
            return ("tp_hit", tp)
    return None


def reconcile_paper_positions(
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
) -> int:
    """Проходит по всем paper-позициям и закрывает достигшие SL/TP по M1 барам.

    Возвращает количество закрытых позиций.
    """
    open_positions = [p for p in store.get_open_positions() if p.is_paper]
    if not open_positions:
        return 0

    closed = 0
    by_symbol: dict[str, list] = {}
    for p in open_positions:
        by_symbol.setdefault(p.symbol, []).append(p)

    for symbol, positions in by_symbol.items():
        # Берём M1 свечи с самой ранней open-ts. Запрашиваем большой
        # период (count=2000 ≈ ~33 часа) — достаточно на dual-timer
        # 5–15 мин, паузы в работе бота, weekends.
        bars = adapter.get_bars(symbol, period_minutes=1, count=2000)
        if not bars:
            log.warning("paper reconcile %s: нет M1 баров, skipping", symbol)
            continue

        for pos in positions:
            opened_ts = _parse_iso_to_unix(pos.opened_at)
            if opened_ts is None:
                continue
            relevant = [b for b in bars if b.ts >= opened_ts]
            triggered = None
            for b in relevant:
                hit = _touched(b, pos.side, pos.sl_price, pos.tp_price)
                if hit is not None:
                    triggered = (b, hit)
                    break
            if triggered is None:
                continue
            bar, (reason, exit_price) = triggered
            pnl = _calc_pnl_usd(
                side=pos.side, entry=pos.entry_price, exit_price=exit_price,
                volume_lots=pos.volume_lots, symbol=pos.symbol,
            )
            store.close_position(
                pos.id, exit_price=exit_price, realized_pnl_usd=pnl,
                close_reason=f"paper_{reason}",
            )
            closed += 1
            pip_size = _pip_size_for(pos.symbol)
            pip_diff = (
                (exit_price - pos.entry_price) / pip_size
                if pos.side.upper() == "BUY"
                else (pos.entry_price - exit_price) / pip_size
            )
            log.info(
                "PAPER RECONCILE: id=%d %s %s lots=%s entry=$%.6g exit=$%.6g "
                "(%s) pnl=$%+.2f (%.1f pips) at bar_ts=%d",
                pos.id, pos.side, pos.symbol, pos.volume_lots,
                pos.entry_price, exit_price, reason, pnl, pip_diff, bar.ts,
            )
    return closed


def _parse_iso_to_unix(iso_ts: str) -> int | None:
    try:
        dt = datetime.fromisoformat(iso_ts)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return None
