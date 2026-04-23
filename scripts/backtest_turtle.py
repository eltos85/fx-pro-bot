#!/usr/bin/env python3
"""Backtest scalp_turtle на исторических M5-данных Bybit.

Цель: проверить выводы Armenian Capstone 2025 (TurtleSoupPatternStrategy на
крипто: WR 44.6%, PnL -2.22%, Sharpe -21.86 на n=531) на НАШИХ символах и
НАШИХ параметрах, через нашу же имплементацию `TurtleSoupStrategy`.

Дизайн:
- Klines тянутся через публичный Bybit API (без ключа), GET /v5/market/kline
  category=linear, interval=5m, limit=1000. Пагинация назад по `start`.
- Бары кэшируются в `data/backtest_klines/{SYMBOL}_5m.csv` — повторный прогон
  без сетевых вызовов.
- Для каждого символа bar-by-bar передаём bars[:i+1] в `_scan_symbol()`.
  Entry = close бара i (сигнал детектим ПОСЛЕ закрытия). ATR на этот момент
  фиксируем — он не меняется до exit.
- SL/TP такие же как в live: sl_atr_mult=1.5, tp_atr_mult=2.5 (RR ≈ 1.67).
- Exit на последующих барах: long → SL если low ≤ entry-1.5*ATR, TP если
  high ≥ entry+2.5*ATR. При двойном хите в одном баре считаем SL первым
  (consertive / pessimistic, стандарт академических backtest'ов).
- Комиссия: taker 0.055% × 2 (round-trip) = 0.11% от notional, вычитается
  из pct_return каждой сделки.
- Сессионный фильтр (7-22 UTC, как в live) применяется к entry_time.
- Пока одна позиция открыта по символу — новые сигналы игнорим (как в live
  через `open_symbols` в main.py).

Использование:
    python3 -m scripts.backtest_turtle --days 90 --symbols SOLUSDT,ADAUSDT,...
    python3 -m scripts.backtest_turtle --days 180 --no-session-filter
    python3 -m scripts.backtest_turtle --days 90 --refetch

Источник истины по параметрам страты — `src/bybit_bot/strategies/scalping/
turtle_soup.py`. Не дублируем константы здесь, импортируем страту напрямую.

Изоляция от FxPro: модуль импортирует только `bybit_bot.*`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pybit.unified_trading import HTTP

from bybit_bot.config.settings import DEFAULT_SYMBOLS
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.turtle_soup import (
    SL_ATR_MULT,
    TP_ATR_MULT,
    TurtleSoupStrategy,
)

log = logging.getLogger("backtest_turtle")

KLINE_INTERVAL = "5"           # Bybit v5: "5" = 5m
KLINE_MAX_LIMIT = 1000         # лимит одного запроса
KLINE_BAR_SEC = 5 * 60
TAKER_FEE_PCT = 0.055          # Bybit taker для linear perp (0.055%)
ROUND_TRIP_FEE_PCT = TAKER_FEE_PCT * 2 / 100  # 0.0011 — десятичная доля

SESSION_START_UTC = 7
SESSION_END_UTC = 22

CACHE_DIR = Path("data/backtest_klines")


# ── klines fetch ────────────────────────────────────────────────

def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}_5m.csv"


def _load_cache(symbol: str) -> list[Bar]:
    path = _cache_path(symbol)
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bars.append(Bar(
                    symbol=symbol,
                    ts=datetime.fromisoformat(row["ts"]).replace(tzinfo=UTC),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b.ts)
    return bars


def _save_cache(symbol: str, bars: list[Bar]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([
                b.ts.replace(tzinfo=None).isoformat(),
                b.open, b.high, b.low, b.close, b.volume,
            ])


def _fetch_klines_window(
    session: HTTP, symbol: str, start_ms: int, end_ms: int,
) -> list[Bar]:
    """Забрать бары (start_ms..end_ms) пагинацией назад по 1000."""
    out: list[Bar] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        resp = session.get_kline(
            category="linear", symbol=symbol, interval=KLINE_INTERVAL,
            start=start_ms, end=cursor_end, limit=KLINE_MAX_LIMIT,
        )
        rows = resp.get("result", {}).get("list", []) or []
        if not rows:
            break
        # Bybit возвращает в порядке по убыванию ts
        for r in rows:
            ts_ms = int(r[0])
            if ts_ms < start_ms:
                continue
            out.append(Bar(
                symbol=symbol,
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
            ))
        oldest_ts_ms = int(rows[-1][0])
        if oldest_ts_ms >= cursor_end - KLINE_BAR_SEC * 1000:
            break
        cursor_end = oldest_ts_ms - 1
        time.sleep(0.1)
    out.sort(key=lambda b: b.ts)
    return out


def fetch_klines(symbol: str, days: int, *, refetch: bool = False) -> list[Bar]:
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    cached: list[Bar] = []
    if not refetch:
        cached = _load_cache(symbol)

    if cached:
        cached = [b for b in cached if b.ts.timestamp() * 1000 >= start_ms]
        if cached and (cached[0].ts.timestamp() * 1000 - start_ms) < 86400 * 1000:
            last_ts_ms = int(cached[-1].ts.timestamp() * 1000)
            if now_ms - last_ts_ms > KLINE_BAR_SEC * 1000:
                session = HTTP()
                fresh = _fetch_klines_window(
                    session, symbol, last_ts_ms + 1, now_ms,
                )
                merged = {int(b.ts.timestamp() * 1000): b for b in cached}
                for b in fresh:
                    merged[int(b.ts.timestamp() * 1000)] = b
                bars = sorted(merged.values(), key=lambda b: b.ts)
                _save_cache(symbol, bars)
                log.info("%s: cache+refresh %d бар (из них %d новых)",
                         symbol, len(bars), len(fresh))
                return bars
            log.info("%s: cache hit %d бар", symbol, len(cached))
            return cached

    session = HTTP()
    log.info("%s: fetch %d дней истории…", symbol, days)
    bars = _fetch_klines_window(session, symbol, start_ms, now_ms)
    _save_cache(symbol, bars)
    log.info("%s: загружено %d бар", symbol, len(bars))
    return bars


# ── simulation ──────────────────────────────────────────────────

@dataclass(slots=True)
class SimTrade:
    symbol: str
    direction: str
    entry_ts: datetime
    entry_price: float
    atr_at_entry: float
    sl_price: float
    tp_price: float
    rsi_at_break: float
    break_depth_atr: float
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    gross_pct: float = 0.0
    net_pct: float = 0.0
    hold_bars: int = 0


def _in_session(ts: datetime, *, enabled: bool) -> bool:
    if not enabled:
        return True
    return SESSION_START_UTC <= ts.hour < SESSION_END_UTC


def simulate_symbol(
    symbol: str, bars: list[Bar], *,
    strategy: TurtleSoupStrategy, session_filter: bool,
    max_hold_bars: int,
) -> list[SimTrade]:
    """Bar-by-bar симуляция одного символа. Одна открытая позиция максимум."""
    trades: list[SimTrade] = []
    open_trade: SimTrade | None = None

    for i in range(len(bars)):
        bar = bars[i]

        if open_trade is not None:
            sl_hit = False
            tp_hit = False
            if open_trade.direction == "long":
                if bar.low <= open_trade.sl_price:
                    sl_hit = True
                if bar.high >= open_trade.tp_price:
                    tp_hit = True
            else:
                if bar.high >= open_trade.sl_price:
                    sl_hit = True
                if bar.low <= open_trade.tp_price:
                    tp_hit = True

            close_price: float | None = None
            close_reason = ""
            if sl_hit and tp_hit:
                close_price = open_trade.sl_price
                close_reason = "sl_tp_same_bar"
            elif sl_hit:
                close_price = open_trade.sl_price
                close_reason = "sl"
            elif tp_hit:
                close_price = open_trade.tp_price
                close_reason = "tp"

            hold = i - _bar_index_by_ts(bars, open_trade.entry_ts)
            if close_price is None and hold >= max_hold_bars:
                close_price = bar.close
                close_reason = "time_stop"

            if close_price is not None:
                open_trade.exit_ts = bar.ts
                open_trade.exit_price = close_price
                open_trade.exit_reason = close_reason
                open_trade.hold_bars = hold
                if open_trade.direction == "long":
                    gross = (close_price / open_trade.entry_price) - 1.0
                else:
                    gross = (open_trade.entry_price / close_price) - 1.0
                open_trade.gross_pct = gross
                open_trade.net_pct = gross - ROUND_TRIP_FEE_PCT
                trades.append(open_trade)
                open_trade = None

        if open_trade is not None:
            continue

        if not _in_session(bar.ts, enabled=session_filter):
            continue

        sig = strategy._scan_symbol(symbol, bars[: i + 1])
        if sig is None:
            continue

        atr_val = sig.atr_value
        if atr_val <= 0:
            continue

        entry_price = bar.close
        if sig.direction.value == "long":
            sl_price = entry_price - SL_ATR_MULT * atr_val
            tp_price = entry_price + TP_ATR_MULT * atr_val
        else:
            sl_price = entry_price + SL_ATR_MULT * atr_val
            tp_price = entry_price - TP_ATR_MULT * atr_val

        open_trade = SimTrade(
            symbol=symbol,
            direction=sig.direction.value,
            entry_ts=bar.ts,
            entry_price=entry_price,
            atr_at_entry=atr_val,
            sl_price=sl_price,
            tp_price=tp_price,
            rsi_at_break=sig.rsi_at_break,
            break_depth_atr=sig.break_depth_atr,
        )

    return trades


def _bar_index_by_ts(bars: list[Bar], ts: datetime) -> int:
    lo, hi = 0, len(bars) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid].ts < ts:
            lo = mid + 1
        elif bars[mid].ts > ts:
            hi = mid - 1
        else:
            return mid
    return lo


# ── metrics ─────────────────────────────────────────────────────

@dataclass(slots=True)
class Metrics:
    n: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    gross_pct_sum: float = 0.0
    net_pct_sum: float = 0.0
    win_pct_sum: float = 0.0
    loss_pct_sum: float = 0.0
    max_dd_pct: float = 0.0
    peak_equity: float = 0.0
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def wr(self) -> float:
        if self.n == 0:
            return 0.0
        return 100.0 * self.wins / self.n

    @property
    def avg_win_pct(self) -> float:
        if self.wins == 0:
            return 0.0
        return 100.0 * self.win_pct_sum / self.wins

    @property
    def avg_loss_pct(self) -> float:
        if self.losses == 0:
            return 0.0
        return 100.0 * self.loss_pct_sum / self.losses

    @property
    def profit_factor(self) -> float:
        if self.loss_pct_sum >= 0:
            return 0.0
        return -self.win_pct_sum / self.loss_pct_sum

    @property
    def expectancy_pct(self) -> float:
        if self.n == 0:
            return 0.0
        return 100.0 * self.net_pct_sum / self.n


def compute_metrics(trades: list[SimTrade]) -> Metrics:
    m = Metrics()
    trades_sorted = sorted(trades, key=lambda t: t.entry_ts)
    equity = 0.0
    for t in trades_sorted:
        m.n += 1
        m.gross_pct_sum += t.gross_pct
        m.net_pct_sum += t.net_pct
        if t.net_pct > 0.0001:
            m.wins += 1
            m.win_pct_sum += t.net_pct
        elif t.net_pct < -0.0001:
            m.losses += 1
            m.loss_pct_sum += t.net_pct
        else:
            m.flats += 1
        m.reasons[t.exit_reason] += 1
        equity += t.net_pct
        if equity > m.peak_equity:
            m.peak_equity = equity
        dd = m.peak_equity - equity
        if dd > m.max_dd_pct:
            m.max_dd_pct = dd
        m.equity_curve.append((t.exit_ts or t.entry_ts, equity))
    return m


def sharpe_annualized(trades: list[SimTrade], *, bars_per_year: int = 105120) -> float:
    """Приближённый Sharpe: mean / std per-trade * sqrt(n_trades_per_year).

    Для M5: 105120 баров/год. Не равно trades/год, но даёт относительную
    величину для сравнения с Capstone 2025 (-21.86 на 1h).
    """
    if len(trades) < 2:
        return 0.0
    rets = [t.net_pct for t in trades]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    avg_bars_per_trade = sum(t.hold_bars for t in trades) / max(len(trades), 1)
    if avg_bars_per_trade <= 0:
        return 0.0
    trades_per_year = bars_per_year / avg_bars_per_trade
    return (mean / std) * math.sqrt(trades_per_year)


# ── report ──────────────────────────────────────────────────────

def format_report(
    all_trades: list[SimTrade],
    by_symbol: dict[str, list[SimTrade]],
    days: int,
    session_filter: bool,
) -> str:
    lines: list[str] = []
    overall = compute_metrics(all_trades)
    sharpe = sharpe_annualized(all_trades)
    lines.append("=" * 78)
    lines.append("SCALP_TURTLE BACKTEST")
    lines.append("=" * 78)
    lines.append(f"Период: {days} дней")
    lines.append(f"Session filter (7-22 UTC): {'ON' if session_filter else 'OFF'}")
    lines.append(f"Параметры: SL={SL_ATR_MULT}×ATR TP={TP_ATR_MULT}×ATR "
                 f"round-trip fee={ROUND_TRIP_FEE_PCT*100:.3f}%")
    lines.append("")
    lines.append("── Overall ────────────────────────────────────────────────")
    lines.append(f"Trades:         {overall.n}")
    lines.append(f"Wins:           {overall.wins}")
    lines.append(f"Losses:         {overall.losses}")
    lines.append(f"Flats:          {overall.flats}")
    lines.append(f"Win Rate:       {overall.wr:.2f}%")
    lines.append(f"Avg Win:        {overall.avg_win_pct:+.3f}%")
    lines.append(f"Avg Loss:       {overall.avg_loss_pct:+.3f}%")
    lines.append(f"Profit Factor:  {overall.profit_factor:.3f}")
    lines.append(f"Expectancy:     {overall.expectancy_pct:+.3f}% / trade")
    lines.append(f"Total PnL:      {overall.net_pct_sum*100:+.2f}% (сумма pct)")
    lines.append(f"Max Drawdown:   {overall.max_dd_pct*100:.2f}%")
    lines.append(f"Sharpe (annual): {sharpe:.2f}")
    lines.append("")
    lines.append("Exits by reason:")
    for reason, count in sorted(overall.reasons.items(), key=lambda x: -x[1]):
        lines.append(f"  {reason:20s} {count}")
    lines.append("")
    lines.append("── Per symbol ─────────────────────────────────────────────")
    lines.append(f"{'symbol':10s} {'n':>4s} {'WR%':>7s} {'avgW%':>7s} {'avgL%':>7s} "
                 f"{'PF':>6s} {'exp%':>7s} {'PnL%':>8s}")
    for sym in sorted(by_symbol.keys()):
        m = compute_metrics(by_symbol[sym])
        lines.append(f"{sym:10s} {m.n:4d} {m.wr:7.2f} {m.avg_win_pct:7.3f} "
                     f"{m.avg_loss_pct:7.3f} {m.profit_factor:6.2f} "
                     f"{m.expectancy_pct:7.3f} {m.net_pct_sum*100:8.2f}")
    lines.append("")
    lines.append("── By direction ───────────────────────────────────────────")
    longs = [t for t in all_trades if t.direction == "long"]
    shorts = [t for t in all_trades if t.direction == "short"]
    for label, tr in [("long", longs), ("short", shorts)]:
        if not tr:
            continue
        m = compute_metrics(tr)
        lines.append(f"  {label:6s} n={m.n:4d} WR={m.wr:5.2f}% exp={m.expectancy_pct:+.3f}%"
                     f" PnL={m.net_pct_sum*100:+.2f}%")
    lines.append("")
    lines.append("── By hour UTC (entry) ────────────────────────────────────")
    by_hour: dict[int, list[SimTrade]] = defaultdict(list)
    for t in all_trades:
        by_hour[t.entry_ts.hour].append(t)
    for h in sorted(by_hour.keys()):
        m = compute_metrics(by_hour[h])
        lines.append(f"  {h:02d}:00  n={m.n:3d}  WR={m.wr:5.1f}%  "
                     f"exp={m.expectancy_pct:+.3f}%  PnL={m.net_pct_sum*100:+.2f}%")
    lines.append("")
    lines.append("── By hold (bars on M5) ───────────────────────────────────")
    buckets = [(0, 3), (3, 6), (6, 12), (12, 24), (24, 48), (48, 100), (100, 10**9)]
    for lo, hi in buckets:
        tr = [t for t in all_trades if lo <= t.hold_bars < hi]
        if not tr:
            continue
        m = compute_metrics(tr)
        lo_lbl = f"{lo}-{hi}"
        lines.append(f"  {lo_lbl:8s}bars n={m.n:4d} WR={m.wr:5.1f}% "
                     f"exp={m.expectancy_pct:+.3f}% PnL={m.net_pct_sum*100:+.2f}%")
    lines.append("")
    lines.append("── Сравнение с Capstone 2025 (Armenian AUA) ───────────────")
    lines.append("Capstone: n=531 (1h крипто), WR=44.60%, PnL=-2.22%, Sharpe=-21.86")
    lines.append(f"Наш:      n={overall.n} (5m крипто), WR={overall.wr:.2f}%, "
                 f"PnL={overall.net_pct_sum*100:+.2f}%, Sharpe={sharpe:.2f}")
    lines.append("")
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="Глубина истории в днях (default: 90)")
    parser.add_argument("--symbols", type=str, default="",
                        help="Через запятую. По умолчанию DEFAULT_SYMBOLS из settings.")
    parser.add_argument("--no-session-filter", action="store_true",
                        help="Отключить 7-22 UTC фильтр")
    parser.add_argument("--max-hold-bars", type=int, default=288,
                        help="Time stop в барах M5 (288 = 24h). Safety net.")
    parser.add_argument("--refetch", action="store_true",
                        help="Игнорировать кэш и забрать заново")
    parser.add_argument("--output", type=str, default="data/backtest_turtle_report.txt",
                        help="Путь для текстового отчёта")
    parser.add_argument("--trades-csv", type=str, default="data/backtest_turtle_trades.csv",
                        help="Путь для дампа всех сделок")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    symbols_tuple: tuple[str, ...]
    if args.symbols:
        symbols_tuple = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    else:
        symbols_tuple = DEFAULT_SYMBOLS

    strategy = TurtleSoupStrategy(max_signals_per_scan=10)
    session_filter = not args.no_session_filter

    all_trades: list[SimTrade] = []
    by_symbol: dict[str, list[SimTrade]] = {}

    for symbol in symbols_tuple:
        bars = fetch_klines(symbol, args.days, refetch=args.refetch)
        if len(bars) < 200:
            log.warning("%s: мало баров (%d) — пропускаю", symbol, len(bars))
            continue
        trades = simulate_symbol(
            symbol, bars, strategy=strategy,
            session_filter=session_filter,
            max_hold_bars=args.max_hold_bars,
        )
        log.info("%s: %d сигналов → %d сделок", symbol, len(trades), len(trades))
        by_symbol[symbol] = trades
        all_trades.extend(trades)

    report = format_report(all_trades, by_symbol, args.days, session_filter)
    print(report)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    log.info("report → %s", out_path)

    trades_path = Path(args.trades_csv)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    with trades_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "direction", "entry_ts", "entry_price", "atr",
            "sl", "tp", "rsi_at_break", "break_depth_atr",
            "exit_ts", "exit_price", "exit_reason",
            "gross_pct", "net_pct", "hold_bars",
        ])
        for t in all_trades:
            writer.writerow([
                t.symbol, t.direction,
                t.entry_ts.isoformat(), f"{t.entry_price:.6f}",
                f"{t.atr_at_entry:.6f}",
                f"{t.sl_price:.6f}", f"{t.tp_price:.6f}",
                f"{t.rsi_at_break:.2f}", f"{t.break_depth_atr:.2f}",
                t.exit_ts.isoformat() if t.exit_ts else "",
                f"{t.exit_price:.6f}" if t.exit_price is not None else "",
                t.exit_reason, f"{t.gross_pct:.6f}", f"{t.net_pct:.6f}",
                t.hold_bars,
            ])
    log.info("trades → %s", trades_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
