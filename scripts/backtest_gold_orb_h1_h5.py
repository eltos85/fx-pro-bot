#!/usr/bin/env python3
"""Backtest gold_orb на 365d M5 GC=F + post-hoc мета-поля для H1-H5.

Compliance:
- BUILDLOG 2026-05-04 «research(2026-knowledge): запускаем H1-H5».
- Параметры фильтров (P30/P70 для H2, 0.6×ATR для H3 и т.д.) ФИКСИРОВАНЫ
  по research-источникам, не подбираются под данные.
- Никакого изменения торговой логики baseline'а — только запись
  meta-полей. Применение фильтров как hard-rule делает
  `scripts/test_h1_h5_filters.py`.

Гипотезы (post-hoc мета-поля для каждой сделки):
- H1 ORB Internal Direction: tradingstats.net/orb-strategy-research,
  «Context Filter #4». 77-80% first-break alignment с close-направлением
  ORB-свечи; +6.5p continuation на trades aligned with direction.
- H2 ATR percentile regime: mql5/769030 «Regime Mismatch» +
  XAU SENTINEL v2.2. Compression = ATR < P30, expansion = ATR > P70.
- H3 ORB tier: tradingstats.net Section «ORB Tier Analysis».
  narrow < 0.3×ATR, normal 0.3-0.6, wide > 0.6× ATR.
  Wide ORB → 77.5% continuation vs 62.9% narrow.
- H4 5-min close confirmation: tradingstats.net «How Confirmation
  Level Changes the Picture» — отдельный sub-run (--mode close_confirm).
- H5 Liquidity sweep pre-break: ICT/SMC 2026 paradigm. Equal highs/lows
  выкошены за N баров до пробоя. Реализован отдельным скриптом.

Использование:
    PYTHONPATH=src python3 -m scripts.backtest_gold_orb_h1_h5 \\
        --csv data/fxpro_klines/GC_F_M5.csv \\
        --is-fraction 0.70 \\
        --out data/gold_orb_h1_h5_enriched.csv

    # Альтернативный entry trigger (H4):
    PYTHONPATH=src python3 -m scripts.backtest_gold_orb_h1_h5 \\
        --mode close_confirm \\
        --out data/gold_orb_h4_close_confirm_enriched.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx
from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import ema_slope

from scripts.backtest_fxpro_all import (
    LIVE_WINDOW_BARS,
    MAX_HOLD_BARS,
    Trade,
    simulate_position,
)

log = logging.getLogger("backtest_gold_orb_h1_h5")

# ── Baseline gold_orb параметры (синхронно с live `gold_orb.py`) ─────
ORB_BARS = 3
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0
ADX_MAX = 100.0  # без ADX-фильтра, как в `backtest_fxpro_candidates`

LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)
LONDON_CLOSE = time(12, 0)
NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)
NY_CLOSE = time(17, 0)

# ── H2 ATR percentile regime ─────────────────────────────────────────
H2_PERCENTILE_WINDOW_DAYS = 30
H2_LOW_REGIME_PCT = 30.0   # < P30 = compression
H2_HIGH_REGIME_PCT = 70.0  # > P70 = volatile expansion

# ── H3 ORB tier ──────────────────────────────────────────────────────
H3_NARROW_THRESHOLD = 0.3   # < 0.3× ATR
H3_WIDE_THRESHOLD = 0.6     # > 0.6× ATR

# ── H5 Liquidity sweep ──────────────────────────────────────────────
# Окно lookback и разделение prior/recent — стандарт ICT/SMC: prior =
# «зрелый» уровень которому ритейл доверяет; recent = свежий выкос
# этого уровня; signal-бар = возврат внутрь диапазона ⇒ true direction.
H5_LOOKBACK_BARS = 50         # ~4 часа M5
H5_PRIOR_END_OFFSET = 10      # последние 10 баров считаются «recent»


@dataclass
class EnrichedTrade:
    # ── Baseline ──
    direction: str
    session: str
    entry_ts: str
    entry_price: float
    box_high: float
    box_low: float
    atr: float
    sl: float
    tp: float
    exit_ts: str
    exit_price: float
    exit_reason: str
    bars_held: int
    pnl_pips: float
    net_pips: float
    # ── Partition ──
    is_oos: bool
    # ── H1 ORB internal direction ──
    orb_internal_dir: str       # "up" / "down"
    h1_aligned: bool             # trade-direction совпал с orb-direction
    # ── H2 ATR percentile regime ──
    atr_percentile: float        # 0..100 на 30-day rolling window
    h2_regime: str               # "compression" / "normal" / "expansion"
    # ── H3 ORB tier ──
    orb_size_atr: float          # ORB range / ATR
    h3_tier: str                 # "narrow" / "normal" / "wide"
    # ── H5 placeholder (заполнит отдельный скрипт) ──
    h5_swept_pre: bool = False


# ────────────────────── Загрузка / split ──────────────────────


def load_bars(csv_path: Path, instrument: str = "GC=F") -> list[Bar]:
    bars: list[Bar] = []
    instr = InstrumentId(symbol=instrument)
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_ms = int(row["timestamp"])
            bars.append(Bar(
                instrument=instr,
                ts=datetime.fromtimestamp(ts_ms / 1000, UTC),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    bars.sort(key=lambda b: b.ts)
    return bars


def compute_split_ts(bars: list[Bar], is_fraction: float) -> datetime:
    """Возвращает timestamp границы IS/OOS (по индексу баров, не по времени)."""
    n = len(bars)
    idx = int(n * is_fraction)
    return bars[idx].ts


# ────────────────────── ORB box helpers ──────────────────────


def session_box(window: list[Bar], ts: datetime) -> tuple[float, float, list[Bar], str] | None:
    """Возвращает (box_high, box_low, box_bars_sorted, session_tag) или None."""
    t = ts.time()
    if LONDON_ORB_END <= t < LONDON_CLOSE:
        start_t, end_t, tag = LONDON_OPEN, LONDON_ORB_END, "london"
    elif NY_ORB_END <= t < NY_CLOSE:
        start_t, end_t, tag = NY_OPEN, NY_ORB_END, "ny"
    else:
        return None
    today = ts.date()
    box: list[Bar] = []
    for b in reversed(window):
        bt = b.ts
        if bt.date() != today:
            break
        if start_t <= bt.time() < end_t:
            box.append(b)
    if len(box) < ORB_BARS:
        return None
    box.sort(key=lambda x: x.ts)
    return (max(b.high for b in box), min(b.low for b in box), box, tag)


# ────────────────────── H1: ORB internal direction ──────────────────────


def compute_orb_internal_dir(box_bars: list[Bar]) -> str:
    """H1: aggregate ORB-окно как одну свечу (open=first.open, close=last.close).
    Direction = "up" если close >= midpoint, иначе "down".

    Source: tradingstats.net «Context Filter #4: ORB Internal Direction».
    Используется aggregate ORB candle, не последний M5 в окне.
    """
    if not box_bars:
        return "up"
    first_open = box_bars[0].open
    last_close = box_bars[-1].close
    high = max(b.high for b in box_bars)
    low = min(b.low for b in box_bars)
    midpoint = (high + low) / 2
    return "up" if last_close >= midpoint else "down"


# ────────────────────── H2: ATR percentile regime ──────────────────────


def build_daily_bars(bars: list[Bar]) -> list[Bar]:
    """Аггрегируем M5 → daily Bar (high=max, low=min, open=first, close=last).

    Даёт правильный «daily true range» для ATR-14d по канону Crabel /
    tradingstats.net («ATR calculation on daily RTH bars»).
    """
    by_day: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        by_day[b.ts.date()].append(b)
    daily: list[Bar] = []
    for d in sorted(by_day):
        day_bars = by_day[d]
        if len(day_bars) < 30:  # фильтруем малые дни (праздники)
            continue
        day_bars.sort(key=lambda x: x.ts)
        daily.append(Bar(
            instrument=day_bars[0].instrument,
            ts=day_bars[0].ts,
            open=day_bars[0].open,
            high=max(b.high for b in day_bars),
            low=min(b.low for b in day_bars),
            close=day_bars[-1].close,
            volume=sum(b.volume for b in day_bars),
        ))
    return daily


def build_daily_atr_index(bars: list[Bar]) -> dict[date, float]:
    """ATR-14d (daily true range) для каждого торгового дня.

    Возвращает dict day → ATR-14d прошедших 14 дней до этого (не включая
    сегодняшний). Используется и для H2 (regime percentile) и для H3
    (ORB tier ratio).
    """
    daily_bars = build_daily_bars(bars)
    out: dict[date, float] = {}
    # _atr требует len(bars) >= period + 1 (нужен prev_close для первого TR).
    for i in range(15, len(daily_bars)):
        prev_15 = daily_bars[i - 15: i]
        atr14 = _atr(prev_15, period=14)
        if atr14 > 0:
            out[daily_bars[i].ts.date()] = atr14
    return out


def compute_atr_percentile(
    daily_atrs: dict[date, float],
    today: date,
    window_days: int = H2_PERCENTILE_WINDOW_DAYS,
) -> float:
    """Percentile rank сегодняшнего ATR на rolling 30-day окне.

    Учитываем только торговые дни (XAUUSD ~5/7), окно — последние
    N календарных дней до today (не включая today).
    """
    today_atr = daily_atrs.get(today)
    if today_atr is None:
        return 50.0
    window: list[float] = []
    for d, atr_v in daily_atrs.items():
        if d >= today:
            continue
        if (today - d).days <= window_days:
            window.append(atr_v)
    if len(window) < 5:
        return 50.0
    below = sum(1 for a in window if a < today_atr)
    return 100.0 * below / len(window)


def regime_label(percentile: float) -> str:
    if percentile < H2_LOW_REGIME_PCT:
        return "compression"
    if percentile > H2_HIGH_REGIME_PCT:
        return "expansion"
    return "normal"


# ────────────────────── H3: ORB tier ──────────────────────


def compute_orb_tier(orb_range: float, atr: float) -> tuple[float, str]:
    """ORB tier по Crabel/tradingstats: narrow/normal/wide."""
    if atr <= 0:
        return 0.0, "narrow"
    ratio = orb_range / atr
    if ratio < H3_NARROW_THRESHOLD:
        return ratio, "narrow"
    if ratio > H3_WIDE_THRESHOLD:
        return ratio, "wide"
    return ratio, "normal"


# ────────────────────── H5: Liquidity sweep ──────────────────────


def compute_h5_swept(
    bars: list[Bar], signal_idx: int, direction: str,
) -> bool:
    """ICT/SMC liquidity sweep detection перед ORB-пробоем.

    Long-signal swept (bullish sweep low → reversal up):
      - prior_window_low = min(low) over bars[-50:-10]
      - в recent_window bars[-10:-1] был хотя бы один бар с low <
        prior_window_low (выкос ритейлных stop-losses)
      - signal-бар close >= prior_window_low (цена уже вернулась
        внутрь prior-range)

    Short-signal swept — зеркально для high.

    Возвращает True если sweep-паттерн обнаружен. Используется как
    post-hoc мета-поле; решение применять как hard-rule делает
    `test_h1_h5_filters.py`.
    """
    if signal_idx < H5_LOOKBACK_BARS:
        return False
    lookback_start = signal_idx - H5_LOOKBACK_BARS
    prior_end = signal_idx - H5_PRIOR_END_OFFSET
    if prior_end <= lookback_start:
        return False
    prior = bars[lookback_start: prior_end]
    recent = bars[prior_end: signal_idx]
    if not prior or not recent:
        return False
    signal_bar = bars[signal_idx]

    if direction == "long":
        prior_low = min(b.low for b in prior)
        recent_low = min(b.low for b in recent)
        # Sweep: recent выкосил уровень И signal вернулся внутрь.
        return recent_low < prior_low and signal_bar.close >= prior_low
    if direction == "short":
        prior_high = max(b.high for b in prior)
        recent_high = max(b.high for b in recent)
        return recent_high > prior_high and signal_bar.close <= prior_high
    return False


# ────────────────────── Backtest core ──────────────────────


def backtest(
    bars: list[Bar],
    daily_atrs: dict[date, float],
    split_ts: datetime,
    mode: str = "wick",
) -> list[EnrichedTrade]:
    """Прогон baseline gold_orb с записью meta-полей H1-H3.

    mode:
      - "wick" — touch-break (high>box_high), как сейчас в live `gold_orb`
      - "close_confirm" — H4-вариант: вход только если бар ЗАКРЫЛСЯ за box.
    """
    trades: list[EnrichedTrade] = []
    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    open_until = -1
    traded_session: set[tuple[date, str]] = set()

    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= open_until:
            continue
        last = bars[i]
        t = last.ts.time()

        if LONDON_ORB_END <= t < LONDON_CLOSE:
            session_tag = "london"
        elif NY_ORB_END <= t < NY_CLOSE:
            session_tag = "ny"
        else:
            continue
        key = (last.ts.date(), session_tag)
        if key in traded_session:
            continue

        window = bars[i - LIVE_WINDOW_BARS: i + 1]
        atr_v = _atr(window)
        if atr_v <= 0:
            continue
        if compute_adx(window) > ADX_MAX:
            continue

        box = session_box(window, last.ts)
        if box is None:
            continue
        box_high, box_low, box_bars, _ = box

        # ── Entry trigger ──
        direction: str | None = None
        entry_break: float = 0.0
        if mode == "wick":
            if last.high > box_high:
                direction = "long"
                entry_break = box_high
            elif last.low < box_low:
                direction = "short"
                entry_break = box_low
            else:
                continue
        elif mode == "close_confirm":
            # H4: вход только если бар-сигнал ЗАКРЫЛСЯ за boundary.
            if last.close > box_high:
                direction = "long"
                entry_break = last.close   # вход по close (не по boundary)
            elif last.close < box_low:
                direction = "short"
                entry_break = last.close
            else:
                continue
        else:
            raise ValueError(f"unknown mode: {mode}")

        # ── EMA50-slope filter (как в baseline) ──
        closes = [b.close for b in window]
        ema_vals = _ema(closes, 50)
        slope = ema_slope(ema_vals, 5)
        if direction == "long" and slope < 0:
            continue
        if direction == "short" and slope > 0:
            continue

        # ── SL/TP ──
        if direction == "long":
            sl = entry_break - SL_ATR_MULT * atr_v
            tp = entry_break + TP_ATR_MULT * atr_v
        else:
            sl = entry_break + SL_ATR_MULT * atr_v
            tp = entry_break - TP_ATR_MULT * atr_v

        tr = simulate_position(bars, i, direction, entry_break, sl, tp, spread, ps)
        if tr is None:
            continue

        # ── Meta-поля для H1-H5 ──
        orb_dir = compute_orb_internal_dir(box_bars)
        h1_aligned = (
            (direction == "long" and orb_dir == "up")
            or (direction == "short" and orb_dir == "down")
        )

        atr_pct = compute_atr_percentile(daily_atrs, last.ts.date())
        regime = regime_label(atr_pct)

        # H3: ORB range vs DAILY ATR-14d (по Crabel/tradingstats), не M5-ATR.
        # M5-ATR (atr_v) используется только для SL/TP-distance.
        orb_range = box_high - box_low
        daily_atr_today = daily_atrs.get(last.ts.date(), 0.0)
        size_ratio, tier = compute_orb_tier(orb_range, daily_atr_today)

        # H5: liquidity sweep ДО signal-бара.
        h5_swept = compute_h5_swept(bars, i, direction)

        is_oos = last.ts >= split_ts

        trades.append(EnrichedTrade(
            direction=direction,
            session=session_tag,
            entry_ts=tr.entry_ts.isoformat(),
            entry_price=round(entry_break, 5),
            box_high=round(box_high, 5),
            box_low=round(box_low, 5),
            atr=round(atr_v, 5),
            sl=round(sl, 5),
            tp=round(tp, 5),
            exit_ts=tr.exit_ts.isoformat(),
            exit_price=round(tr.exit_price, 5),
            exit_reason=tr.reason,
            bars_held=tr.bars_held,
            pnl_pips=tr.pnl_pips,
            net_pips=tr.net_pips,
            is_oos=is_oos,
            orb_internal_dir=orb_dir,
            h1_aligned=h1_aligned,
            atr_percentile=round(atr_pct, 1),
            h2_regime=regime,
            orb_size_atr=round(size_ratio, 3),
            h3_tier=tier,
            h5_swept_pre=h5_swept,
        ))
        traded_session.add(key)
        open_until = i + tr.bars_held

    return trades


# ────────────────────── Reporting ──────────────────────


def summarize(trades: list[EnrichedTrade], label: str) -> None:
    n = len(trades)
    if n == 0:
        log.info("%s: 0 trades", label)
        return
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    wr = 100.0 * len(wins) / n
    net = sum(t.net_pips for t in trades)
    avg = net / n
    avg_w = statistics.mean([t.net_pips for t in wins]) if wins else 0.0
    avg_l = statistics.mean([t.net_pips for t in losses]) if losses else 0.0
    pf = (sum(t.net_pips for t in wins) / abs(sum(t.net_pips for t in losses))
          if losses and sum(t.net_pips for t in losses) != 0 else float("inf"))
    log.info(
        "%s: n=%d WR=%.1f%% net=%+.0fp avg=%+.1fp avg_w=%+.1f avg_l=%+.1f PF=%.2f",
        label, n, wr, net, avg, avg_w, avg_l, pf,
    )


def write_enriched_csv(path: Path, trades: list[EnrichedTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        log.warning("write_enriched_csv: 0 trades, файл не создан")
        return
    fields = list(asdict(trades[0]).keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))
    log.info("→ %s (%d rows, %d cols)", path, len(trades), len(fields))


def report_meta_distributions(trades: list[EnrichedTrade]) -> None:
    """Распределение мета-полей: контроль что фильтры работают на разных
    срезах данных, а не на пустоте."""
    if not trades:
        return
    log.info("─── Meta-distribution (all %d trades) ───", len(trades))

    # H1
    aligned = sum(1 for t in trades if t.h1_aligned)
    log.info("H1 aligned (trade dir == orb dir): %d/%d (%.1f%%)",
             aligned, len(trades), 100 * aligned / len(trades))

    # H2
    regimes = Counter(t.h2_regime for t in trades)
    log.info("H2 regimes: %s", dict(regimes))

    # H3
    tiers = Counter(t.h3_tier for t in trades)
    log.info("H3 tiers: %s", dict(tiers))

    # H5
    swept = sum(1 for t in trades if t.h5_swept_pre)
    log.info("H5 swept_pre: %d/%d (%.1f%%)",
             swept, len(trades), 100 * swept / len(trades))

    # IS/OOS
    oos = sum(1 for t in trades if t.is_oos)
    log.info("IS/OOS: IS=%d, OOS=%d", len(trades) - oos, oos)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default="data/fxpro_klines/GC_F_M5.csv",
                   help="Путь к M5 CSV")
    p.add_argument("--is-fraction", type=float, default=0.70,
                   help="Доля IS partition (default 0.70)")
    p.add_argument("--out", default="",
                   help="Выходной enriched CSV (по умолчанию авто)")
    p.add_argument("--mode", choices=["wick", "close_confirm"], default="wick",
                   help="Entry trigger: wick (baseline) | close_confirm (H4)")
    args = p.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        log.error("FAIL: %s не найден", csv_path)
        return 1

    out_path = Path(args.out) if args.out else Path(
        f"data/gold_orb_h1_h5_enriched_{args.mode}.csv"
    )

    log.info("Loading bars from %s ...", csv_path)
    bars = load_bars(csv_path)
    log.info("Loaded %d bars %s → %s", len(bars), bars[0].ts.date(), bars[-1].ts.date())

    log.info("Building daily ATR index ...")
    daily_atrs = build_daily_atr_index(bars)
    log.info("Daily ATR index: %d days", len(daily_atrs))

    split_ts = compute_split_ts(bars, args.is_fraction)
    log.info("IS/OOS split: %s (is_fraction=%.2f)", split_ts.isoformat(), args.is_fraction)

    log.info("Running baseline gold_orb mode=%s ...", args.mode)
    trades = backtest(bars, daily_atrs, split_ts, mode=args.mode)
    log.info("Trades produced: %d", len(trades))

    is_trades = [t for t in trades if not t.is_oos]
    oos_trades = [t for t in trades if t.is_oos]

    log.info("─── Performance baseline (no filters) ───")
    summarize(trades, f"ALL [{args.mode}]")
    summarize(is_trades, "  IS")
    summarize(oos_trades, "  OOS")

    report_meta_distributions(trades)

    write_enriched_csv(out_path, trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
