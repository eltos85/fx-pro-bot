#!/usr/bin/env python3
"""Robustness-check: варьируем параметры gold_orb_iso.

Проверяем что стратегия НЕ overfit'нута на одном sweet-spot.
"""

from __future__ import annotations

import logging
from pathlib import Path

from scripts.backtest_fxpro_candidates import backtest_gold_orb
import scripts.backtest_fxpro_candidates as mod
from scripts.backtest_fxpro_all import load_bars, summarize, print_report

log = logging.getLogger("robustness")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    all_bars = {}
    for sym in ("GC=F",):
        b = load_bars(Path("data/fxpro_klines"), sym)
        if b:
            all_bars[sym] = b

    grid = [
        # (sl_atr, tp_atr, adx_max, label)
        (1.0, 2.0, 100.0, "SL1.0_TP2.0"),
        (1.0, 3.0, 100.0, "SL1.0_TP3.0"),
        (1.5, 2.0, 100.0, "SL1.5_TP2.0"),
        (1.5, 3.0, 100.0, "SL1.5_TP3.0"),   # baseline
        (1.5, 4.0, 100.0, "SL1.5_TP4.0"),
        (2.0, 3.0, 100.0, "SL2.0_TP3.0"),
        (2.0, 4.0, 100.0, "SL2.0_TP4.0"),
        (1.5, 3.0, 25.0,  "SL1.5_TP3.0_ADX25"),
        (1.5, 3.0, 40.0,  "SL1.5_TP3.0_ADX40"),
    ]

    reports = []
    for sl_atr, tp_atr, adx_max, label in grid:
        mod.GOLD_ORB_SL_ATR = sl_atr
        mod.GOLD_ORB_TP_ATR = tp_atr
        mod.GOLD_ORB_ADX_MAX = adx_max
        trades = backtest_gold_orb(all_bars)
        for t in trades:
            t.strategy = label
        reports.append(summarize(label, trades))

    print_report(reports)


if __name__ == "__main__":
    main()
