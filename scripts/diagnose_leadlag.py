"""Диагностика почему scalp_leadlag даёт всего 2 сделки за 90 дней.

Проходит по 90 дней M5-баров и на каждом шаге фиксирует какой фильтр
"срезает" потенциальный сигнал:

  1. BTC move >= 1% за 15 мин
  2. BTC move >= 1.5 ATR
  3. BTC ADX >= 15
  4. alt corr(returns) >= 0.5
  5. alt lag <= 0.3% (и не >= 70% BTC-move в ту же сторону)

Для каждого бара считается 3 значения BTC (pct, atr, adx) и лог распределений.
Финальный отчёт показывает: на скольких барах прошёл каждый фильтр.

Усилить страту можно ослабив самый жёсткий фильтр.
"""
from __future__ import annotations

import csv
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bybit_bot.analysis.signals import atr
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx

CACHE_DIR = Path("data/backtest_klines")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"]
LIVE_WINDOW = 1440

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("diagnose_leadlag")


def _load(sym: str) -> list[Bar]:
    p = CACHE_DIR / f"{sym}_5m.csv"
    bars: list[Bar] = []
    with open(p, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            bars.append(Bar(
                symbol=sym,
                ts=datetime.fromisoformat(row["ts"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    bars.sort(key=lambda b: b.ts)
    return bars


def _log_returns(closes: list[float]) -> list[float]:
    if len(closes) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            out.append(0.0)
        else:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def _pearson(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n < 10:
        return 0.0
    xs = x[-n:]
    ys = y[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((v - mx) ** 2 for v in xs)
    vy = sum((v - my) ** 2 for v in ys)
    if vx == 0 or vy == 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / math.sqrt(vx * vy)


def main() -> None:
    bars_map = {s: _load(s) for s in SYMBOLS}
    n_bars = min(len(b) for b in bars_map.values())
    btc_bars_all = bars_map["BTCUSDT"]

    log.info("Символов: %d, баров: %d (≈%.1f дней)", len(SYMBOLS), n_bars, n_bars * 5 / 60 / 24)

    # Счётчики прохода фильтров (level 1 — только BTC)
    total_scans = 0
    pass_btc_pct = 0
    pass_btc_atr = 0
    pass_btc_adx = 0
    pass_all_btc = 0

    # Распределения
    btc_pct_vals: list[float] = []
    btc_atr_vals: list[float] = []
    btc_adx_vals: list[float] = []

    # Счётчики по альтам (на фоне прошедших BTC-фильтров)
    alt_scans = 0
    pass_corr = 0
    pass_lag = 0
    pass_all = 0  # alt-сигнал сгенерирован

    min_start = 60  # MIN_BARS
    step = 1  # каждый бар

    for i in range(min_start, n_bars, step):
        total_scans += 1
        lo = max(0, i + 1 - LIVE_WINDOW)
        btc_bars = btc_bars_all[lo : i + 1]
        if len(btc_bars) < 60:
            continue

        btc_closes = [b.close for b in btc_bars]
        btc_lb = btc_closes[-4:]  # -BTC_LOOKBACK_BARS-1 == -4 (lookback=3)
        btc_move = btc_lb[-1] - btc_lb[0]
        btc_move_pct = btc_move / btc_lb[0] if btc_lb[0] else 0.0
        btc_atr_v = atr(btc_bars, period=14)
        if btc_atr_v <= 0:
            continue
        btc_move_atr = abs(btc_move) / btc_atr_v

        btc_pct_vals.append(abs(btc_move_pct))
        btc_atr_vals.append(btc_move_atr)

        p1 = abs(btc_move_pct) >= 0.01
        p2 = btc_move_atr >= 1.5
        p3 = False
        adx_v = 0.0
        if p1 and p2:
            adx_v = compute_adx(btc_bars, period=14)
            btc_adx_vals.append(adx_v)
            p3 = adx_v >= 15.0

        if p1:
            pass_btc_pct += 1
        if p2:
            pass_btc_atr += 1
        if p3:
            pass_btc_adx += 1

        if not (p1 and p2 and p3):
            continue
        pass_all_btc += 1

        # Переходим к альтам
        for sym in SYMBOLS:
            if sym == "BTCUSDT":
                continue
            alt_bars_full = bars_map[sym]
            if i >= len(alt_bars_full):
                continue
            alt_bars = alt_bars_full[lo : i + 1]
            if len(alt_bars) < 60:
                continue
            alt_scans += 1
            alt_closes = [b.close for b in alt_bars]
            corr = _pearson(
                _log_returns(alt_closes[-51:]),
                _log_returns(btc_closes[-51:]),
            )
            if corr < 0.5:
                continue
            pass_corr += 1

            alt_lb = alt_closes[-4:]
            alt_move = alt_lb[-1] - alt_lb[0]
            alt_move_pct = alt_move / alt_lb[0] if alt_lb[0] else 0.0
            same_side = (btc_move > 0 and alt_move >= 0) or (btc_move < 0 and alt_move <= 0)
            if same_side and abs(alt_move_pct) >= abs(btc_move_pct) * 0.7:
                continue
            if abs(alt_move_pct) > 0.003 and same_side:
                continue
            pass_lag += 1
            pass_all += 1

    def _pct(x: int, total: int) -> str:
        return f"{x/total*100:.3f}%" if total else "—"

    def _dist(vals: list[float], name: str, unit: str = "") -> None:
        if not vals:
            return
        vs = sorted(vals)
        print(f"  {name:<12} n={len(vs)}  min={vs[0]:.4f}  p25={vs[len(vs)//4]:.4f}  "
              f"p50={vs[len(vs)//2]:.4f}  p75={vs[len(vs)*3//4]:.4f}  "
              f"p90={vs[len(vs)*9//10]:.4f}  p95={vs[len(vs)*95//100]:.4f}  "
              f"p99={vs[len(vs)*99//100]:.4f}  max={vs[-1]:.4f}{unit}")

    print(f"\n{'=' * 80}")
    print("LEADLAG FILTER FUNNEL")
    print(f"{'=' * 80}")
    print(f"Всего сканов (шаг {step}): {total_scans}")
    print(f"  BTC move >=1%  :   {pass_btc_pct:>6} ({_pct(pass_btc_pct, total_scans)})")
    print(f"  BTC move >=1.5ATR: {pass_btc_atr:>6} ({_pct(pass_btc_atr, total_scans)})")
    print(f"  BTC ADX >=15    : {pass_btc_adx:>6} ({_pct(pass_btc_adx, total_scans)})")
    print(f"  ВСЕ BTC фильтры : {pass_all_btc:>6} ({_pct(pass_all_btc, total_scans)})")

    print(f"\nАльт-скан (только после прохождения BTC): {alt_scans}")
    print(f"  corr >= 0.5     : {pass_corr:>6} ({_pct(pass_corr, alt_scans)})")
    print(f"  lag OK          : {pass_lag:>6} ({_pct(pass_lag, alt_scans)})")
    print(f"  ВСЕ фильтры → signal: {pass_all:>6} ({_pct(pass_all, alt_scans)})")

    print("\n=== РАСПРЕДЕЛЕНИЯ ===")
    _dist(btc_pct_vals, "BTC |move%|", " (доля)")
    _dist(btc_atr_vals, "BTC move/ATR")
    _dist(btc_adx_vals, "BTC ADX (после |pct|+atr)")


if __name__ == "__main__":
    main()
