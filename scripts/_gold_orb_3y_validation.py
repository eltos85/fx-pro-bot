"""gold_orb на 3 годах M5 XAUUSD с cTrader — проверка вне периода подбора (research-only).

Параметры стратегии подбирались на 90d (янв–апр 2026) и 365d (май 2025 –
май 2026) — BUILDLOG 2026-04-23 / 2026-05-04. Период 2023-09 → 2025-04 не
участвовал в подборе ни разу — это честный out-of-sample.

Логика — как в scripts/backtest_gold_orb_h1_h5.py (baseline, без H-фильтров):
коробка 3 бара M5 (London 08:00–08:15, NY 14:30–14:45 UTC), touch-break,
EMA50-slope, SL 1.5×ATR / TP 3.0×ATR (M5 ATR14), time-stop 6ч.

Отличия от старого бэктеста (в старом — оптимистично):
  1. Цена входа. Старый: ровно граница коробки. Live: рыночный ордер
     по текущей цене после касания (gold_orb.process_signals, entry_price=price).
     Здесь три варианта: boundary (старый) | close сигнального бара |
     open следующего бара. SL/TP от фактической цены входа (как live).
  2. Комиссия FxPro: 35 USD / 1M USD номинала за сторону
     (ProtoOASymbol.commission=3500, commissionType=USD_PER_MILLION).
  3. Своп при переносе через 17:00 New York: long −69.25 / short +17.15
     пипсов цены (0.01) за ночь, тройной в среду (swapRollover3Days=3) —
     снимок demo 2026-09-25.
  Спред — как в старом бэктесте: spread_cost_pips(GC=F)×1.2.
Режимы входа: canonical (1 сделка на сессию) и multi (повторный вход после
закрытия, как в live-коде).
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, "src")
sys.path.insert(0, ".")
from fx_pro_bot.analysis.signals import _atr, _ema  # noqa: E402
from fx_pro_bot.config.settings import pip_size, spread_cost_pips  # noqa: E402
from fx_pro_bot.strategies.scalping.indicators import ema_slope  # noqa: E402
from scripts.backtest_gold_orb_h1_h5 import (  # noqa: E402
    LONDON_CLOSE, LONDON_ORB_END, NY_CLOSE, NY_ORB_END, SL_ATR_MULT,
    TP_ATR_MULT, load_bars, session_box,
)

CSV = Path("data/fxpro_klines/GC_F_M5_3y.csv")
WINDOW = 1440
MAX_HOLD = 72
PS = pip_size("GC=F")
SPREAD = spread_cost_pips("GC=F") * 1.2
COMM_PER_SIDE = 35e-6
SWAP_LONG, SWAP_SHORT = -69.25 * 0.01, 17.15 * 0.01  # USD/oz за ночь
NY = ZoneInfo("America/New_York")
PERIODS = [
    ("A 2023-09→2025-04 (не видела)", datetime(2023, 9, 1, tzinfo=UTC), datetime(2025, 5, 1, tzinfo=UTC)),
    ("B 2025-05→2026-04 (подбор)", datetime(2025, 5, 1, tzinfo=UTC), datetime(2026, 4, 23, tzinfo=UTC)),
    ("C 2026-04-23→ (после запуска)", datetime(2026, 4, 23, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)),
]


def rollovers(t0: datetime, t1: datetime) -> int:
    """Число списаний свопа между t0 и t1 (17:00 NY; среда ×3)."""
    n = 0
    d = t0.astimezone(NY).date()
    while True:
        roll = datetime.combine(d, time(17, 0), tzinfo=NY).astimezone(UTC)
        if roll > t1:
            break
        if roll > t0 and d.weekday() < 5:
            n += 3 if d.weekday() == 2 else 1
        d += timedelta(days=1)
    return n


def run(bars, entry_mode: str, multi: bool) -> list[dict]:
    out: list[dict] = []
    traded: set = set()
    busy_until = -1
    highs = np.array([b.high for b in bars])
    lows = np.array([b.low for b in bars])
    for i in range(WINDOW, len(bars) - 2):
        if i <= busy_until:
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
        if not multi and key in traded:
            continue
        window = bars[i - WINDOW: i + 1]
        box = session_box(window, last.ts)
        if box is None:
            continue
        bh, bl = box[0], box[1]
        if last.high > bh:
            side, boundary = "long", bh
        elif last.low < bl:
            side, boundary = "short", bl
        else:
            continue
        atr = _atr(window)
        if atr <= 0:
            continue
        slope = ema_slope(_ema([b.close for b in window], 50), 5)
        if (side == "long" and slope < 0) or (side == "short" and slope > 0):
            continue

        if entry_mode == "boundary":
            entry, start = boundary, i + 1
        elif entry_mode == "close":
            entry, start = last.close, i + 1
        else:
            entry, start = bars[i + 1].open, i + 1
        lg = side == "long"
        sl = entry - SL_ATR_MULT * atr if lg else entry + SL_ATR_MULT * atr
        tp = entry + TP_ATR_MULT * atr if lg else entry - TP_ATR_MULT * atr
        if entry_mode == "next_open":
            start = i + 1
        exit_px, reason, j = None, "time", min(start + MAX_HOLD - 1, len(bars) - 1)
        for k in range(start, min(start + MAX_HOLD, len(bars))):
            if entry_mode == "next_open" and k == i + 1:
                hit_sl = lows[k] <= sl if lg else highs[k] >= sl
                hit_tp = highs[k] >= tp if lg else lows[k] <= tp
            else:
                hit_sl = lows[k] <= sl if lg else highs[k] >= sl
                hit_tp = highs[k] >= tp if lg else lows[k] <= tp
            if hit_sl:
                exit_px, reason, j = sl, "sl", k
                break
            if hit_tp:
                exit_px, reason, j = tp, "tp", k
                break
        if exit_px is None:
            exit_px = bars[j].close
        gross = ((exit_px - entry) if lg else (entry - exit_px)) / PS
        comm = COMM_PER_SIDE * (entry + exit_px) / PS
        nroll = rollovers(bars[i].ts, bars[j].ts)
        swap = nroll * (SWAP_LONG if lg else SWAP_SHORT) / PS
        net = gross - SPREAD - comm + swap
        risk = SL_ATR_MULT * atr / PS
        out.append({"ts": last.ts, "session": tag, "side": side, "reason": reason,
                    "gross": gross, "spread": SPREAD, "comm": comm, "swap": swap,
                    "net": net, "risk_pips": risk, "R_net": net / risk, "R_gross": gross / risk,
                    "slip_pips": ((entry - boundary) if lg else (boundary - entry)) / PS})
        traded.add(key)
        busy_until = j
    return out


def line(label: str, d: pd.DataFrame) -> str:
    if d.empty:
        return f"  {label:<34} n=0"
    r = d.R_net
    w, l = d.net[d.net > 0].sum(), -d.net[d.net <= 0].sum()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 else float("nan")
    return (f"  {label:<34} n={len(d):>4} WR={(d.net > 0).mean():.0%} PF={w / l if l else float('inf'):.2f} "
            f"avgR={r.mean():+.3f} t={t:+.2f} net={d.net.sum():+8.0f}p")


def main() -> int:
    bars = load_bars(CSV)
    print(f"Баров {len(bars)}: {bars[0].ts} → {bars[-1].ts}, pip={PS}, spread={SPREAD:.1f}p\n")
    for multi in (False, True):
        for mode in ("boundary", "close", "next_open"):
            df = pd.DataFrame(run(bars, mode, multi))
            print(f"=== {'multi' if multi else 'canonical'} × вход {mode}")
            print(f"  издержки на сделку: спред {df.spread.mean():.1f}p, комиссия {df.comm.mean():.1f}p, "
                  f"своп {df.swap.mean():+.2f}p; проскальзывание от границы {df.slip_pips.mean():+.1f}p; "
                  f"риск (SL) {df.risk_pips.median():.0f}p")
            print(line("ВСЕ gross→R", df.assign(R_net=df.R_gross, net=df.gross)))
            print(line("ВСЕ net", df))
            for name, a, b in PERIODS:
                print(line(name, df[(df.ts >= a) & (df.ts < b)]))
            if mode == "close" and not multi:
                df["q"] = df.ts.dt.tz_localize(None).dt.to_period("Q")
                for q, g in df.groupby("q"):
                    print(line(f"    {q}", g))
                for s, g in df.groupby("session"):
                    print(line(f"    сессия {s}", g))
                for s, g in df.groupby("side"):
                    print(line(f"    {s}", g))
                live = df[(df.ts >= datetime(2026, 4, 23, 21, tzinfo=UTC)) & (df.ts < datetime(2026, 5, 22, tzinfo=UTC))]
                print(line("    окно live 04-23→05-22", live))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
