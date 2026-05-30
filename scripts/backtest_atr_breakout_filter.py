"""Бэктест фильтра подтверждения пробоя: absolute ATR%>1.0 vs relative ATR-expansion.

Контекст (BUILDLOG_AI_TRADER.md v0.40.1+): MFP rule 4 (BREAKOUT) требует
`ATR% > 1.0%`. Проф-источники (PyQuantLab/Backtrader, tradeos 2026,
Pipmaster, coinxsight, DEV/Pine) показывают, что канонический фильтр
волатильностной экспансии — ОТНОСИТЕЛЬНЫЙ: `ATR_now > SMA(ATR, N) * k`,
k≈1.2–1.3, а `ATR% < 1.0%` — это просто LOW-режим волы для часовых альтов
(DEV regime table; ETH 1H ATR% 0.88% = LOW).

Скрипт тянет 1H-бары с Bybit (public market data, mainnet), на каждом
пробое Donchian/24h-high считает обе семьи фильтров и forward-исход
(SL=2·ATR, TP=1.5R, time-stop H баров), печатает частоту сигналов + WR +
expectancy(R) по каждому фильтру и символу.

Запуск:
    python3 scripts/backtest_atr_breakout_filter.py
    python3 scripts/backtest_atr_breakout_filter.py --days 180 --horizon 24

Артефакт для no-data-fitting.mdc: вывод сохраняется в
data/backtest_atr_filter_out.txt (перенаправлением).
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from pybit.unified_trading import HTTP

SYMBOLS = ["LTCUSDT", "ATOMUSDT", "BTCUSDT", "SUIUSDT"]
ATR_PERIOD = 14
DONCHIAN = 24  # rule 4: «broke 24h high/low on 1H close»
BASELINE_N = 30  # SMA(ATR, N) baseline (PyQuantLab 30; tradeos 50; coinxsight 20)


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float


def fetch_klines(session: HTTP, symbol: str, days: int) -> list[Bar]:
    """Пагинированный фетч 1H-баров за `days` дней (Bybit limit=1000/call)."""
    want_ms = days * 24 * 60 * 60 * 1000
    now_ms = int(time.time() * 1000)
    oldest_wanted = now_ms - want_ms
    end = now_ms
    by_ts: dict[int, Bar] = {}
    while True:
        resp = session.get_kline(
            category="linear", symbol=symbol, interval="60",
            limit=1000, end=end,
        )
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            break
        page: list[Bar] = []
        for row in items:
            try:
                page.append(Bar(int(row[0]), float(row[1]), float(row[2]),
                                float(row[3]), float(row[4])))
            except (ValueError, IndexError, TypeError):
                continue
        if not page:
            break
        for b in page:
            by_ts[b.ts] = b
        oldest = min(b.ts for b in page)
        if oldest <= oldest_wanted or len(page) < 2:
            break
        end = oldest - 1
        time.sleep(0.2)  # вежливый rate-limit
    bars = sorted(by_ts.values(), key=lambda b: b.ts)
    return [b for b in bars if b.ts >= oldest_wanted]


def atr_series(bars: list[Bar], period: int) -> list[float | None]:
    """Wilder RMA(TR) как running-серия, выровненная по индексам баров."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, n):
        hl = bars[i].high - bars[i].low
        hc = abs(bars[i].high - bars[i - 1].close)
        lc = abs(bars[i].low - bars[i - 1].close)
        trs.append(max(hl, hc, lc))
    # trs[j] соответствует bars[j+1]
    seed = sum(trs[:period]) / period
    rma = seed
    out[period] = rma  # bars index = period (после period TR)
    for j in range(period, len(trs)):
        rma = (rma * (period - 1) + trs[j]) / period
        out[j + 1] = rma
    return out


def sma_at(values: list[float | None], idx: int, n: int) -> float | None:
    if idx - n + 1 < 0:
        return None
    window = values[idx - n + 1: idx + 1]
    if any(v is None for v in window):
        return None
    return sum(v for v in window) / n  # type: ignore[misc]


@dataclass
class FilterStats:
    name: str
    signals: int = 0
    wins: int = 0
    losses: int = 0
    undecided: int = 0

    def add(self, outcome: str) -> None:
        self.signals += 1
        if outcome == "win":
            self.wins += 1
        elif outcome == "loss":
            self.losses += 1
        else:
            self.undecided += 1

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.decided if self.decided else 0.0

    def expectancy_r(self, tp_r: float = 1.5) -> float:
        if not self.decided:
            return 0.0
        return self.wr * tp_r - (1 - self.wr) * 1.0


def simulate_forward(bars: list[Bar], i: int, side: str, atr: float,
                     horizon: int, tp_r: float = 1.5) -> str:
    """SL=2·ATR (1R), TP=tp_r·R. Что наступит первым за horizon баров."""
    entry = bars[i].close
    risk = 2.0 * atr
    if risk <= 0:
        return "undecided"
    if side == "long":
        sl = entry - risk
        tp = entry + tp_r * risk
        for k in range(i + 1, min(i + 1 + horizon, len(bars))):
            if bars[k].low <= sl:
                return "loss"
            if bars[k].high >= tp:
                return "win"
    else:
        sl = entry + risk
        tp = entry - tp_r * risk
        for k in range(i + 1, min(i + 1 + horizon, len(bars))):
            if bars[k].high >= sl:
                return "loss"
            if bars[k].low <= tp:
                return "win"
    return "undecided"


def run_symbol(bars: list[Bar], horizon: int) -> dict[str, FilterStats]:
    atrs = atr_series(bars, ATR_PERIOD)
    filters = {
        "breakout_only": FilterStats("breakout_only"),
        "abs_atr%>1.0": FilterStats("abs_atr%>1.0"),
        "rel_k1.2": FilterStats("rel_k1.2"),
        "rel_k1.25": FilterStats("rel_k1.25"),
        "rel_k1.3": FilterStats("rel_k1.3"),
    }
    atr_pcts: list[float] = []
    start = max(DONCHIAN, ATR_PERIOD + BASELINE_N) + 1
    for i in range(start, len(bars)):
        atr = atrs[i]
        if atr is None or bars[i].close <= 0:
            continue
        prior_high = max(b.high for b in bars[i - DONCHIAN:i])
        prior_low = min(b.low for b in bars[i - DONCHIAN:i])
        long_break = bars[i].close > prior_high
        short_break = bars[i].close < prior_low
        if not (long_break or short_break):
            continue
        side = "long" if long_break else "short"
        outcome = simulate_forward(bars, i, side, atr, horizon)

        atr_pct = atr / bars[i].close * 100.0
        atr_pcts.append(atr_pct)
        base = sma_at(atrs, i, BASELINE_N)
        expansion = (atr / base) if base else 0.0

        filters["breakout_only"].add(outcome)
        if atr_pct > 1.0:
            filters["abs_atr%>1.0"].add(outcome)
        if expansion >= 1.2:
            filters["rel_k1.2"].add(outcome)
        if expansion >= 1.25:
            filters["rel_k1.25"].add(outcome)
        if expansion >= 1.3:
            filters["rel_k1.3"].add(outcome)

    if atr_pcts:
        atr_pcts.sort()
        med = atr_pcts[len(atr_pcts) // 2]
        filters["_meta_median_atrpct"] = med  # type: ignore[assignment]
    return filters


def fmt_row(fs: FilterStats) -> str:
    return (f"  {fs.name:16s} signals={fs.signals:4d}  "
            f"WR={fs.wr * 100:5.1f}%  exp={fs.expectancy_r():+.3f}R  "
            f"(W{fs.wins}/L{fs.losses}/U{fs.undecided})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--horizon", type=int, default=24)
    args = ap.parse_args()

    session = HTTP(testnet=False)  # public market data, без ключей
    print(f"=== ATR breakout-filter backtest === days={args.days} "
          f"horizon={args.horizon} donchian={DONCHIAN} baseline_atr_sma={BASELINE_N}")
    print("SL=2·ATR (1R), TP=1.5R, time-stop=horizon баров. "
          "Forward-исход на закрытых барах.\n")

    totals: dict[str, FilterStats] = {
        k: FilterStats(k) for k in
        ["breakout_only", "abs_atr%>1.0", "rel_k1.2", "rel_k1.25", "rel_k1.3"]
    }
    for sym in SYMBOLS:
        bars = fetch_klines(session, sym, args.days)
        if len(bars) < 200:
            print(f"[{sym}] недостаточно баров ({len(bars)}) — пропуск\n")
            continue
        res = run_symbol(bars, args.horizon)
        med = res.pop("_meta_median_atrpct", None)  # type: ignore[arg-type]
        span_days = (bars[-1].ts - bars[0].ts) / 86_400_000
        print(f"[{sym}] bars={len(bars)} (~{span_days:.0f}d)  "
              f"median ATR%={med:.2f}%" if med is not None else f"[{sym}]")
        for key in ["breakout_only", "abs_atr%>1.0", "rel_k1.2", "rel_k1.25", "rel_k1.3"]:
            fs = res[key]
            print(fmt_row(fs))
            t = totals[key]
            t.signals += fs.signals
            t.wins += fs.wins
            t.losses += fs.losses
            t.undecided += fs.undecided
        print()

    print("=== TOTAL (все символы) ===")
    for key in ["breakout_only", "abs_atr%>1.0", "rel_k1.2", "rel_k1.25", "rel_k1.3"]:
        print(fmt_row(totals[key]))


if __name__ == "__main__":
    main()
