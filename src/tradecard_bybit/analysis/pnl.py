"""Сводка P&L по иерархии источников (stats-collection.mdc, TASKSPEC §3.2).

Источник правды по P&L — Bybit ``closedPnl`` (net, с fees/funding). В БД он
отражён флагом ``pnl_verified=1`` (true-up). Приоритет: verified > provisional >
db-оценка. paper и live агрегируются **раздельно** (mode). Если доступен Bybit
closedPnl за период — показываем его net как ground truth и сверяем с БД.

Timezone: БД хранит epoch ``ts_*`` (UTC); Bybit API — UTC ms; биржевая выписка —
обычно MSK. Все сравнения ведём в UTC; отображаем UTC (+ MSK при необходимости).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradecard_bybit.analysis.trade import Trade, net_pnl, win_rate


@dataclass
class ModePnl:
    mode: str
    n_decided: int
    wins: int
    wr: float
    net_db: float            # сумма pnl_usd по decided (БД, приоритет verified)
    n_verified: int
    n_provisional: int
    n_db_only: int
    n_non_trade: int
    bybit_net: float | None = None   # Bybit closedPnl net за период (ground truth)
    bybit_trades: int | None = None

    @property
    def discrepancy(self) -> float | None:
        if self.bybit_net is None:
            return None
        return self.bybit_net - self.net_db


def summarize_mode(trades: list[Trade], mode: str) -> ModePnl:
    same = [t for t in trades if t.mode == mode and t.is_closed]
    dd = [t for t in same if t.is_decided]
    return ModePnl(
        mode=mode, n_decided=len(dd), wins=sum(1 for t in dd if t.is_win),
        wr=win_rate(dd), net_db=net_pnl(dd),
        n_verified=sum(1 for t in dd if t.pnl_source == "verified"),
        n_provisional=sum(1 for t in dd if t.pnl_source == "provisional"),
        n_db_only=sum(1 for t in dd if t.pnl_source == "db"),
        n_non_trade=sum(1 for t in same if t.is_non_trade))


def bybit_net(rows: list[dict]) -> tuple[float, int]:
    """Сумма net по Bybit closedPnl-записям (уже net). Возвращает (net, n)."""
    total = 0.0
    n = 0
    for r in rows:
        try:
            total += float(r.get("closedPnl") or 0.0)
            n += 1
        except (ValueError, TypeError):
            continue
    return total, n
