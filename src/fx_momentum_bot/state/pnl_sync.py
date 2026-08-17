"""Синхронизация broker-net P&L в SQLite на живом коннекте momentum-бота.

Не открывает вторую Open API-сессию (лимит 2 connections / app —
https://help.ctrader.com/open-api/connection/). Источник правды —
``ProtoOADealListReq`` / ``closePositionDetail`` (gross+swap+commission),
https://help.ctrader.com/open-api/messages/. Historical-запросы ≤5/s;
здесь один запрос на цикл (~300s).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fx_momentum_bot.state.store import MomentumStore
from fx_pro_bot.trading.executor import TradeExecutor

log = logging.getLogger("fx_momentum_bot")

_OVERLAP_MS = 6 * 3600 * 1000  # повторно читаем 6ч — запоздалые deal'ы


def _iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _money(cpd: Any, field: str) -> float:
    raw = getattr(cpd, field, 0) or 0
    md = int(getattr(cpd, "moneyDigits", 0) or 0) or 2
    return float(raw) / (10 ** md)


def _has_close(deal: Any) -> bool:
    try:
        return bool(deal.HasField("closePositionDetail"))
    except Exception:  # noqa: BLE001
        return getattr(deal, "closePositionDetail", None) is not None


def sync_broker_pnl(
    executor: TradeExecutor,
    store: MomentumStore,
    *,
    sid_to_symbol: dict[int, str],
    baseline_ms: int,
    open_position_ids: set[int],
) -> int:
    """Дописать новые close-deal'ы. Возвращает число новых строк."""
    if not sid_to_symbol:
        return 0
    last = store.last_closed_deal_ts_ms()
    from_ms = baseline_ms if last is None else max(baseline_ms, last - _OVERLAP_MS)
    to_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    try:
        resp = executor.client.get_deal_list(
            from_ts=from_ms, to_ts=to_ms, max_rows=2000
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("P&L sync: deal-list не ответил (%s) — пропуск цикла", exc)
        return 0
    deals = list(getattr(resp, "deal", []) or [])
    n_new = 0
    for deal in deals:
        if not _has_close(deal):
            continue
        sid = int(getattr(deal, "symbolId", 0) or 0)
        symbol = sid_to_symbol.get(sid)
        if symbol is None:
            continue  # чужая вселенная (XAUUSD / fx_ai_trader)
        cpd = deal.closePositionDetail
        ts_ms = int(getattr(deal, "executionTimestamp", 0) or 0)
        if ts_ms <= 0:
            continue
        side_val = int(getattr(deal, "tradeSide", 0) or 0)
        side = "long" if side_val == 1 else "short"
        gross = _money(cpd, "grossProfit")
        swap = _money(cpd, "swap")
        comm = _money(cpd, "commission")
        inserted = store.insert_closed_deal(
            deal_id=int(getattr(deal, "dealId", 0) or 0),
            broker_position_id=int(getattr(deal, "positionId", 0) or 0),
            symbol=symbol,
            side=side,
            closed_at=_iso_utc(ts_ms),
            closed_ts_ms=ts_ms,
            net_usd=gross + swap + comm,
            gross_usd=gross,
            swap_usd=swap,
            commission_usd=comm,
            volume=int(getattr(deal, "filledVolume", 0) or 0),
            execution_price=float(getattr(deal, "executionPrice", 0) or 0) or None,
            entry_price=float(getattr(cpd, "entryPrice", 0) or 0) or None,
        )
        if inserted:
            n_new += 1
    if n_new:
        snap = store.pnl_snapshot(
            since=_iso_utc(baseline_ms), open_position_ids=open_position_ids
        )
        log.info(
            "P&L sync: +%d deal(s) | since %s closed=%d WR=%.0f%% net=$%+.2f "
            "(open_partials=%d)",
            n_new,
            _iso_utc(baseline_ms),
            snap["n_closed_positions"],
            100.0 * snap["wr"],
            snap["net_usd"],
            snap["n_open_with_partials"],
        )
    return n_new
