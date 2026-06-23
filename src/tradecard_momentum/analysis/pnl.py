"""Сводка P&L (broker deal-list = ground truth, stats-collection.mdc, §3.2).

Источник правды по P&L — cTrader ``get_deal_list`` net (gross+swap+commission).
У momentum-бота нет paper-режима с закрытыми сделками (paper только логирует
решения, ничего не открывает), поэтому режим один — **live** (реальные ордера
на брокере). Разбивка по символам — для срезов в отчёте.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tradecard_momentum.analysis.trade import (MomentumTrade, decided,
                                               expectancy_r, net_pnl, win_rate)


@dataclass
class BrokerPnl:
    n_decided: int
    wins: int
    wr: float
    net: float                # Σ net (gross+swap+commission), ground truth
    gross: float
    swap: float
    commission: float
    exp_r: float | None
    n_with_r: int             # сколько сделок с реконструированным R


def summarize(trades: list[MomentumTrade]) -> BrokerPnl:
    dd = decided(trades)
    return BrokerPnl(
        n_decided=len(dd), wins=sum(1 for t in dd if t.is_win),
        wr=win_rate(dd), net=net_pnl(dd),
        gross=sum(t.gross_usd for t in dd),
        swap=sum(t.swap_usd for t in dd),
        commission=sum(t.commission_usd for t in dd),
        exp_r=expectancy_r(dd),
        n_with_r=sum(1 for t in dd if t.r_multiple is not None))


@dataclass
class SymbolPnl:
    symbol: str
    n_decided: int
    wr: float
    net: float
    exp_r: float | None


def summarize_by_symbol(trades: list[MomentumTrade]) -> list[SymbolPnl]:
    """P&L раздельно по символам. Сортировка по net по возрастанию (худшие сверху)."""
    by: dict[str, list[MomentumTrade]] = defaultdict(list)
    for t in trades:
        if t.is_decided:
            by[t.symbol].append(t)
    out = [
        SymbolPnl(symbol=sym, n_decided=len(grp), wr=win_rate(grp),
                  net=net_pnl(grp), exp_r=expectancy_r(grp))
        for sym, grp in by.items()
    ]
    out.sort(key=lambda s: s.net)
    return out
