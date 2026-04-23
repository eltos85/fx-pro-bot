"""Обогатить data/backtest_all_trades.csv контекстом на момент входа.

Для каждой сделки подгружаем M5-клайны символа из data/backtest_klines/,
находим бар с timestamp == entry_ts, берём окно из 200 предыдущих баров
(только данные ДО входа — без look-ahead) и считаем индикаторы.

Добавляемые колонки:
  - hour_utc, session (asia/london/ny/off), day_of_week (0=Mon..6=Sun)
  - atr_pct (ATR14 / close * 100)
  - rsi14
  - adx14
  - ema20_slope_pct (ema20[-1] vs ema20[-6], в % от close)
  - ema50_slope_pct
  - bb_width_pct (BB20/2std, ширина / close)
  - volume_ratio (vol[-1] / mean(vol[-20:-1]))
  - price_range_pct_24h (1-day high-low range)

Вход:
  data/backtest_all_trades.csv
  data/backtest_klines/{SYMBOL}_5m.csv

Выход:
  data/backtest_trades_enriched.csv
"""
from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TRADES_IN = Path("data/backtest_all_trades.csv")
KLINES_DIR = Path("data/backtest_klines")
OUT = Path("data/backtest_trades_enriched.csv")

LOOKBACK = 200


@dataclass(slots=True)
class Bar:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


def _load_klines(symbol: str) -> list[Bar]:
    path = KLINES_DIR / f"{symbol}_5m.csv"
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            bars.append(Bar(
                ts=datetime.fromisoformat(row["ts"]),
                o=float(row["open"]),
                h=float(row["high"]),
                l=float(row["low"]),
                c=float(row["close"]),
                v=float(row["volume"]),
            ))
    return bars


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, lo, prev_c = bars[i].h, bars[i].l, bars[i-1].c
        tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        trs.append(tr)
    last = trs[-period:]
    return sum(last) / len(last)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    g = sum(gains[-period:]) / period
    l = sum(losses[-period:]) / period
    if l == 0:
        return 100.0
    rs = g / l
    return 100 - 100 / (1 + rs)


def _adx(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period * 2 + 1:
        return 0.0
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(bars)):
        up = bars[i].h - bars[i-1].h
        dn = bars[i-1].l - bars[i].l
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, lo, pc = bars[i].h, bars[i].l, bars[i-1].c
        tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    smoothed_tr = sum(tr[-period:])
    smoothed_plus = sum(plus_dm[-period:])
    smoothed_minus = sum(minus_dm[-period:])
    if smoothed_tr == 0:
        return 0.0
    plus_di = 100.0 * smoothed_plus / smoothed_tr
    minus_di = 100.0 * smoothed_minus / smoothed_tr
    if plus_di + minus_di == 0:
        return 0.0
    dx = 100.0 * abs(plus_di - minus_di) / (plus_di + minus_di)
    return dx


def _bb_width_pct(closes: list[float], period: int = 20) -> float:
    if len(closes) < period:
        return 0.0
    window = closes[-period:]
    mid = sum(window) / period
    if mid == 0:
        return 0.0
    std = statistics.pstdev(window)
    return (4 * std) / mid * 100.0


def _session(h: int) -> str:
    # UTC-часовые блоки (типичное разделение крипто-сессий)
    if 0 <= h < 8:
        return "asia"
    if 8 <= h < 13:
        return "london"
    if 13 <= h < 21:
        return "ny"
    return "off"


def main() -> None:
    print(f"Чтение {TRADES_IN}…")
    with TRADES_IN.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Всего сделок: {len(rows)}")

    # Группировка символов для минимизации перечтений.
    symbols = sorted({r["symbol"] for r in rows})
    klines_cache: dict[str, list[Bar]] = {}
    ts_index: dict[str, dict[datetime, int]] = {}
    for sym in symbols:
        bars = _load_klines(sym)
        if not bars:
            print(f"  WARN: нет данных для {sym}")
            continue
        klines_cache[sym] = bars
        ts_index[sym] = {b.ts: i for i, b in enumerate(bars)}
        print(f"  {sym}: {len(bars)} баров")

    print(f"Обогащение и запись в {OUT}…")
    out_rows: list[dict] = []
    missing = 0
    for row in rows:
        sym = row["symbol"]
        if sym not in klines_cache:
            missing += 1
            continue
        entry_ts = datetime.fromisoformat(row["entry_ts"])
        entry_ts_naive = entry_ts.replace(tzinfo=None)
        idx = ts_index[sym].get(entry_ts_naive)
        if idx is None or idx < LOOKBACK:
            missing += 1
            continue
        bars = klines_cache[sym]
        window = bars[idx - LOOKBACK : idx]  # строго ДО entry (без look-ahead)
        closes = [b.c for b in window]
        vols = [b.v for b in window]

        entry_price = float(row["entry_price"])
        atr_val = _atr(window)
        rsi_val = _rsi(closes)
        adx_val = _adx(window)
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        ema20_slope_pct = (
            (ema20[-1] - ema20[-6]) / entry_price * 100.0 if len(ema20) >= 6 else 0.0
        )
        ema50_slope_pct = (
            (ema50[-1] - ema50[-6]) / entry_price * 100.0 if len(ema50) >= 6 else 0.0
        )
        bb_w = _bb_width_pct(closes)
        avg_vol_20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else 0.0
        vol_ratio = vols[-1] / avg_vol_20 if avg_vol_20 > 0 else 0.0
        highs_24h = [b.h for b in window[-288:]]  # 288 M5-баров = 24ч
        lows_24h = [b.l for b in window[-288:]]
        range_pct = (
            (max(highs_24h) - min(lows_24h)) / entry_price * 100.0
            if highs_24h else 0.0
        )

        hour = entry_ts_naive.hour
        new_row = dict(row)
        new_row.update({
            "hour_utc": hour,
            "session": _session(hour),
            "day_of_week": entry_ts_naive.weekday(),
            "atr_pct": round(atr_val / entry_price * 100.0, 4),
            "rsi14": round(rsi_val, 2),
            "adx14": round(adx_val, 2),
            "ema20_slope_pct": round(ema20_slope_pct, 4),
            "ema50_slope_pct": round(ema50_slope_pct, 4),
            "bb_width_pct": round(bb_w, 4),
            "volume_ratio": round(vol_ratio, 3),
            "range_24h_pct": round(range_pct, 3),
        })
        out_rows.append(new_row)

    print(f"Обогащено: {len(out_rows)}, пропущено (нет данных): {missing}")

    if not out_rows:
        print("Пусто. Выход.")
        return
    fieldnames = list(out_rows[0].keys())
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)
    print(f"Записано {len(out_rows)} строк в {OUT}")


if __name__ == "__main__":
    main()
