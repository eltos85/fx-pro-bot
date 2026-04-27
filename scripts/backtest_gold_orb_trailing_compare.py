#!/usr/bin/env python3
"""Сравнение 3 вариантов exit-логики gold_orb на 2 годах GC=F M5.

Цель: ответить на вопрос "trailingStopLoss противоречит стратегии?".

Варианты:
  A — NO_TRAIL          : чистый SL/TP (как в оригинальном backtest +6146 90d)
  B — BOT_TRAIL_LAG     : peak по close, проверка раз в бар (= live логика
                          с 5-min лагом в _update_broker_trailing_sl)
  C — SERVER_TRAIL_RT   : peak по high/low, проверка intra-bar (= cTrader
                          server-side trailingStopLoss, без задержки)

Параметры trailing (взяты из live-кода, fixed pips):
  TRAIL_TRIGGER_PIPS = 5.0
  TRAIL_DISTANCE_PIPS = 3.0

Сигналы gold_orb генерируем точно как в `backtest_fxpro_candidates.backtest_gold_orb`
(touch-break, EMA50-slope filter, без ADX/volume).

Splits: IS = первые 60% баров, OOS = последние 40%.

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_gold_orb_trailing_compare
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema
from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.strategies.scalping.indicators import ema_slope

from scripts.backtest_fxpro_all import (
    LIVE_WINDOW_BARS,
    MAX_HOLD_BARS,
    load_bars,
)
from scripts.backtest_fxpro_candidates import (
    GOLD_ORB_SL_ATR,
    GOLD_ORB_TP_ATR,
    LONDON_CLOSE,
    LONDON_ORB_END,
    NY_CLOSE,
    NY_ORB_END,
    _session_box_gold,
)

log = logging.getLogger("trailing_compare")

TRAIL_TRIGGER_PIPS = 5.0
TRAIL_DISTANCE_PIPS = 3.0


@dataclass(slots=True)
class Signal:
    entry_idx: int
    direction: str
    entry_price: float
    sl: float
    tp: float
    atr: float


@dataclass(slots=True)
class TradeRes:
    direction: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    reason: str
    net_pips: float


def collect_signals(bars: list[Bar]) -> list[Signal]:
    """Точная копия signal-generation из backtest_gold_orb()."""
    out: list[Signal] = []
    open_until = -1
    traded_session: set[tuple[date, str]] = set()
    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= open_until:
            continue
        last = bars[i]
        t = last.ts.time()
        if LONDON_ORB_END <= t < LONDON_CLOSE:
            tag = "london"
        elif NY_ORB_END <= t < NY_CLOSE:
            tag = "ny"
        else:
            continue
        key = (last.ts.date(), tag)
        if key in traded_session:
            continue
        window = bars[i - LIVE_WINDOW_BARS: i + 1]
        atr_v = _atr(window)
        if atr_v <= 0:
            continue
        box = _session_box_gold(window, last.ts)
        if box is None:
            continue
        box_high, box_low, _ = box
        direction: str | None = None
        if last.high > box_high:
            direction = "long"
            entry = box_high
        elif last.low < box_low:
            direction = "short"
            entry = box_low
        else:
            continue
        closes = [b.close for b in window]
        ema_vals = _ema(closes, 50)
        slope = ema_slope(ema_vals, 5)
        if direction == "long" and slope < 0:
            continue
        if direction == "short" and slope > 0:
            continue
        if direction == "long":
            sl = entry - GOLD_ORB_SL_ATR * atr_v
            tp = entry + GOLD_ORB_TP_ATR * atr_v
        else:
            sl = entry + GOLD_ORB_SL_ATR * atr_v
            tp = entry - GOLD_ORB_TP_ATR * atr_v
        out.append(Signal(i, direction, entry, sl, tp, atr_v))
        traded_session.add(key)
        # bars_held не знаем заранее, но open_until нужен чтобы не дублить
        # ставим cushion = MAX_HOLD_BARS. При исполнении trade обычно меньше.
        open_until = i + MAX_HOLD_BARS
    return out


def _pips(direction: str, entry: float, exit_p: float, ps: float) -> float:
    g = (exit_p - entry) / ps
    return g if direction == "long" else -g


def simulate_no_trail(bars: list[Bar], sig: Signal, ps: float, spread: float) -> TradeRes:
    """A: чистый SL/TP, как backtest."""
    is_long = sig.direction == "long"
    end = min(sig.entry_idx + 1 + MAX_HOLD_BARS, len(bars))
    for j in range(sig.entry_idx + 1, end):
        b = bars[j]
        if is_long:
            hit_sl = b.low <= sig.sl
            hit_tp = b.high >= sig.tp
        else:
            hit_sl = b.high >= sig.sl
            hit_tp = b.low <= sig.tp
        if hit_sl and hit_tp:
            exit_p, reason = sig.sl, "sl"
        elif hit_sl:
            exit_p, reason = sig.sl, "sl"
        elif hit_tp:
            exit_p, reason = sig.tp, "tp"
        else:
            continue
        gross = _pips(sig.direction, sig.entry_price, exit_p, ps)
        return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, exit_p, reason, round(gross - spread, 2))
    j = end - 1
    b = bars[j]
    gross = _pips(sig.direction, sig.entry_price, b.close, ps)
    return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, b.close, "time", round(gross - spread, 2))


def simulate_bot_trail_lag(bars: list[Bar], sig: Signal, ps: float, spread: float,
                           trig_pips: float = TRAIL_TRIGGER_PIPS,
                           dist_pips: float = TRAIL_DISTANCE_PIPS) -> TradeRes:
    """B: peak отслеживается ТОЛЬКО по close (= раз в 5 мин в live).
    SL двигается на следующем баре. Симулирует текущий live с 5-min lag."""
    is_long = sig.direction == "long"
    cur_sl = sig.sl
    peak_close = sig.entry_price
    end = min(sig.entry_idx + 1 + MAX_HOLD_BARS, len(bars))
    for j in range(sig.entry_idx + 1, end):
        b = bars[j]
        # Сначала проверяем SL/TP по high/low внутри бара (брокер закрывает intrabar)
        if is_long:
            hit_sl = b.low <= cur_sl
            hit_tp = b.high >= sig.tp
        else:
            hit_sl = b.high >= cur_sl
            hit_tp = b.low <= sig.tp
        if hit_sl and hit_tp:
            exit_p, reason = cur_sl, "trail" if cur_sl != sig.sl else "sl"
        elif hit_sl:
            exit_p, reason = cur_sl, "trail" if cur_sl != sig.sl else "sl"
        elif hit_tp:
            exit_p, reason = sig.tp, "tp"
        else:
            # обновляем peak по close + двигаем SL
            close_pips = _pips(sig.direction, sig.entry_price, b.close, ps)
            peak_pips_close = _pips(sig.direction, sig.entry_price, peak_close, ps)
            if close_pips > peak_pips_close:
                peak_close = b.close
                peak_pips_close = close_pips
            if peak_pips_close >= trig_pips:
                if is_long:
                    new_sl = peak_close - dist_pips * ps
                    if new_sl > cur_sl:
                        cur_sl = new_sl
                else:
                    new_sl = peak_close + dist_pips * ps
                    if cur_sl <= 0 or new_sl < cur_sl:
                        cur_sl = new_sl
            continue
        gross = _pips(sig.direction, sig.entry_price, exit_p, ps)
        return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, exit_p, reason, round(gross - spread, 2))
    j = end - 1
    b = bars[j]
    gross = _pips(sig.direction, sig.entry_price, b.close, ps)
    return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, b.close, "time", round(gross - spread, 2))


def simulate_server_trail_rt(bars: list[Bar], sig: Signal, ps: float, spread: float,
                             trig_pips: float = TRAIL_TRIGGER_PIPS,
                             dist_pips: float = TRAIL_DISTANCE_PIPS) -> TradeRes:
    """C: server-side trailing — peak по high/low, проверка intra-bar.

    На каждом M5 баре:
      • peak обновляется по high (long) / low (short) если выше предыдущего
      • SL подтягивается до peak ± dist_pips (если ушло за trigger)
      • SL/TP проверяются ВНУТРИ ТОГО ЖЕ бара по high/low

    NB. Для M5 бара мы не знаем последовательность high/low внутри. Используем
    pessimistic правило: если в одном баре new_sl был обновлён ВЫШЕ low (long),
    значит SL мог сработать "после" пика. Для simplicity:
      • если max_pips за бар ≥ trigger → SL подтянут ДО peak ± dist
      • если затем low/high бара пробил new_sl → закрытие на new_sl"""
    is_long = sig.direction == "long"
    cur_sl = sig.sl
    peak_price = sig.entry_price
    end = min(sig.entry_idx + 1 + MAX_HOLD_BARS, len(bars))
    for j in range(sig.entry_idx + 1, end):
        b = bars[j]
        # 1) обновить peak по экстремумам бара
        if is_long:
            if b.high > peak_price:
                peak_price = b.high
        else:
            if b.low < peak_price:
                peak_price = b.low
        peak_pips = _pips(sig.direction, sig.entry_price, peak_price, ps)
        # 2) подтянуть SL если триггер достигнут
        if peak_pips >= trig_pips:
            if is_long:
                new_sl = peak_price - dist_pips * ps
                if new_sl > cur_sl:
                    cur_sl = new_sl
            else:
                new_sl = peak_price + dist_pips * ps
                if cur_sl <= 0 or new_sl < cur_sl:
                    cur_sl = new_sl
        # 3) проверить SL/TP по high/low бара
        if is_long:
            hit_sl = b.low <= cur_sl
            hit_tp = b.high >= sig.tp
        else:
            hit_sl = b.high >= cur_sl
            hit_tp = b.low <= sig.tp
        if hit_sl and hit_tp:
            exit_p, reason = cur_sl, "trail" if cur_sl != sig.sl else "sl"
        elif hit_sl:
            exit_p, reason = cur_sl, "trail" if cur_sl != sig.sl else "sl"
        elif hit_tp:
            exit_p, reason = sig.tp, "tp"
        else:
            continue
        gross = _pips(sig.direction, sig.entry_price, exit_p, ps)
        return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, exit_p, reason, round(gross - spread, 2))
    j = end - 1
    b = bars[j]
    gross = _pips(sig.direction, sig.entry_price, b.close, ps)
    return TradeRes(sig.direction, sig.entry_idx, j, sig.entry_price, b.close, "time", round(gross - spread, 2))


def summarize(label: str, trades: list[TradeRes]) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    total = sum(t.net_pips for t in trades)
    sw = sum(t.net_pips for t in wins)
    sl = -sum(t.net_pips for t in losses)
    pf = sw / sl if sl > 0 else float("inf")
    wr = len(wins) / len(trades) * 100
    avg_w = sw / len(wins) if wins else 0
    avg_l = -sl / len(losses) if losses else 0
    by_reason = {}
    for t in trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    return {
        "label": label, "n": len(trades), "net": round(total, 1),
        "wr": round(wr, 1), "pf": round(pf, 2),
        "avg_w": round(avg_w, 1), "avg_l": round(avg_l, 1),
        "reasons": by_reason,
    }


def split_trades(trades: list[TradeRes], split_idx: int) -> tuple[list[TradeRes], list[TradeRes]]:
    is_ = [t for t in trades if t.entry_idx < split_idx]
    oos = [t for t in trades if t.entry_idx >= split_idx]
    return is_, oos


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    sym = "GC=F"
    log.info("Loading %s M5 bars...", sym)
    bars = load_bars(Path("data/fxpro_klines"), sym)
    if not bars:
        log.error("Нет данных GC=F"); return
    log.info("Bars: %d (%s → %s)", len(bars), bars[0].ts, bars[-1].ts)

    log.info("Generating signals...")
    sigs = collect_signals(bars)
    log.info("Signals: %d", len(sigs))

    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2
    split_idx = int(len(bars) * 0.6)
    log.info("IS/OOS split bar idx = %d (date %s)", split_idx, bars[split_idx].ts)

    log.info("Simulating A=NO_TRAIL, B=BOT_LAG, C=SERVER_RT...")
    A = [simulate_no_trail(bars, s, ps, spread) for s in sigs]
    B = [simulate_bot_trail_lag(bars, s, ps, spread) for s in sigs]
    C = [simulate_server_trail_rt(bars, s, ps, spread) for s in sigs]

    print()
    print("=" * 110)
    print(f"{'Variant':24s} {'period':5s} {'n':>4s} {'net_pips':>10s} {'WR%':>6s} {'PF':>6s} {'avgW':>6s} {'avgL':>6s}  reasons")
    print("-" * 110)
    for label, trades in (("A_NO_TRAIL", A), ("B_BOT_LAG_5/3", B), ("C_SERVER_RT_5/3", C)):
        is_t, oos_t = split_trades(trades, split_idx)
        for period, ts in (("ALL", trades), ("IS", is_t), ("OOS", oos_t)):
            r = summarize(label, ts)
            if r["n"] == 0:
                print(f"{label:24s} {period:5s} {0:>4d}  (нет сделок)")
                continue
            print(f"{label:24s} {period:5s} {r['n']:>4d} {r['net']:>10.1f} {r['wr']:>6.1f} {r['pf']:>6.2f} {r['avg_w']:>6.1f} {r['avg_l']:>6.1f}  {r['reasons']}")
        print()
    print("=" * 110)


if __name__ == "__main__":
    main()
