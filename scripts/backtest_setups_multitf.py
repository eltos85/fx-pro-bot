"""Мультитаймфрейм-бэктест двух сетапов: BREAKOUT vs MEAN-REVERSION.

Вопрос (чат 2026-05-30): «у нас реалтайм — почему бэктест на 1H?». Проверяем,
есть ли edge на быстрых ТФ (5м/15м) и у какого сетапа. Сравниваем на одних
данных Bybit (public, mainnet):

- BREAKOUT: close пробил max/min прошлых DONCHIAN баров (long/short).
- MEAN-REVERT: close ≤ BB_lower И RSI ≤ 25 (long) / close ≥ BB_upper И
  RSI ≥ 75 (short) — MFP rules 2+3 (bb-z + rsi extreme), fade.

Forward-исход: SL = 2·ATR (=1R), TP = 1.5R, time-stop = horizon баров.
WR > 40% = положительное матожидание при R:R 1.5.

Запуск:
    python3 scripts/backtest_setups_multitf.py
Артефакт: data/backtest_setups_multitf_out.txt
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from math import sqrt

from pybit.unified_trading import HTTP

SYMBOLS = ["LTCUSDT", "ATOMUSDT", "BTCUSDT", "SUIUSDT"]
ATR_PERIOD = 14
RSI_PERIOD = 14
BB_PERIOD = 20
BB_SIGMA = 2.0
DONCHIAN = 24
# (interval, days) — 1H за ~2 года (главный вопрос про edge), быстрые ТФ
# удлинены умеренно (вывод «быстрее=хуже» уже устойчив; 5м за 2г = сотни
# тысяч баров, нецелесообразно по времени/лимитам).
TIMEFRAMES = [("5", 180), ("15", 365), ("60", 730)]


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float


def fetch_klines(session: HTTP, symbol: str, interval: str, days: int) -> list[Bar]:
    want_ms = days * 24 * 60 * 60 * 1000
    now_ms = int(time.time() * 1000)
    oldest_wanted = now_ms - want_ms
    end = now_ms
    by_ts: dict[int, Bar] = {}
    while True:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=interval, limit=1000, end=end)
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
        time.sleep(0.15)
    bars = sorted(by_ts.values(), key=lambda b: b.ts)
    return [b for b in bars if b.ts >= oldest_wanted]


def atr_series(bars: list[Bar], period: int) -> list[float | None]:
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
    seed = sum(trs[:period]) / period
    rma = seed
    out[period] = rma
    for j in range(period, len(trs)):
        rma = (rma * (period - 1) + trs[j]) / period
        out[j + 1] = rma
    return out


def rsi_series(bars: list[Bar], period: int) -> list[float | None]:
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        diff = bars[i].close - bars[i - 1].close
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period

    def rsi_val(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100 - 100 / (1 + rs)

    out[period] = rsi_val(ag, al)
    for j in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[j]) / period
        al = (al * (period - 1) + losses[j]) / period
        out[j + 1] = rsi_val(ag, al)
    return out


def bb_at(bars: list[Bar], i: int, period: int, sigma: float) -> tuple[float, float] | None:
    if i - period + 1 < 0:
        return None
    window = [b.close for b in bars[i - period + 1: i + 1]]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = sqrt(var)
    return (mid + sigma * sd, mid - sigma * sd)  # upper, lower


@dataclass
class Stats:
    name: str
    wins: int = 0
    losses: int = 0
    undecided: int = 0

    def add(self, o: str) -> None:
        if o == "win":
            self.wins += 1
        elif o == "loss":
            self.losses += 1
        else:
            self.undecided += 1

    @property
    def signals(self) -> int:
        return self.wins + self.losses + self.undecided

    @property
    def decided(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.decided if self.decided else 0.0

    def exp_r(self, tp_r: float = 1.5) -> float:
        if not self.decided:
            return 0.0
        return self.wr * tp_r - (1 - self.wr) * 1.0


def simulate(bars: list[Bar], i: int, side: str, atr: float,
             horizon: int, tp_r: float = 1.5) -> str:
    entry = bars[i].close
    risk = 2.0 * atr
    if risk <= 0:
        return "undecided"
    if side == "long":
        sl, tp = entry - risk, entry + tp_r * risk
        for k in range(i + 1, min(i + 1 + horizon, len(bars))):
            if bars[k].low <= sl:
                return "loss"
            if bars[k].high >= tp:
                return "win"
    else:
        sl, tp = entry + risk, entry - tp_r * risk
        for k in range(i + 1, min(i + 1 + horizon, len(bars))):
            if bars[k].high >= sl:
                return "loss"
            if bars[k].low <= tp:
                return "win"
    return "undecided"


def run(bars: list[Bar], horizon: int) -> dict[str, Stats]:
    atrs = atr_series(bars, ATR_PERIOD)
    rsis = rsi_series(bars, RSI_PERIOD)
    res = {
        "breakout": Stats("breakout"),
        "mean_revert": Stats("mean_revert"),
    }
    start = max(DONCHIAN, ATR_PERIOD, RSI_PERIOD, BB_PERIOD) + 1
    for i in range(start, len(bars)):
        atr = atrs[i]
        rsi = rsis[i]
        if atr is None or rsi is None or bars[i].close <= 0:
            continue
        c = bars[i].close
        # breakout
        ph = max(b.high for b in bars[i - DONCHIAN:i])
        pl = min(b.low for b in bars[i - DONCHIAN:i])
        if c > ph:
            res["breakout"].add(simulate(bars, i, "long", atr, horizon))
        elif c < pl:
            res["breakout"].add(simulate(bars, i, "short", atr, horizon))
        # mean-revert
        bb = bb_at(bars, i, BB_PERIOD, BB_SIGMA)
        if bb is not None:
            upper, lower = bb
            if c <= lower and rsi <= 25:
                res["mean_revert"].add(simulate(bars, i, "long", atr, horizon))
            elif c >= upper and rsi >= 75:
                res["mean_revert"].add(simulate(bars, i, "short", atr, horizon))
    return res


def fmt(s: Stats) -> str:
    return (f"    {s.name:12s} sig={s.signals:4d}  WR={s.wr * 100:5.1f}%  "
            f"exp={s.exp_r():+.3f}R  (W{s.wins}/L{s.losses}/U{s.undecided})")


def main() -> None:
    session = HTTP(testnet=False)
    horizon = 24
    print(f"=== Multi-TF setup backtest === horizon={horizon} bars  "
          f"SL=2ATR(1R) TP=1.5R  donchian={DONCHIAN} BB={BB_PERIOD}/{BB_SIGMA} RSI={RSI_PERIOD}")
    print("WR>40% = положительное матожидание при R:R 1.5\n")

    for interval, days in TIMEFRAMES:
        tf_name = {"5": "5m", "15": "15m", "60": "1H"}[interval]
        print(f"################  TF = {tf_name}  (≈{days}d)  ################")
        tot = {"breakout": Stats("breakout"), "mean_revert": Stats("mean_revert")}
        for sym in SYMBOLS:
            bars = fetch_klines(session, sym, interval, days)
            if len(bars) < 300:
                print(f"  [{sym}] мало баров ({len(bars)}) — пропуск")
                continue
            r = run(bars, horizon)
            print(f"  [{sym}] bars={len(bars)}")
            for key in ["breakout", "mean_revert"]:
                print(fmt(r[key]))
                tot[key].wins += r[key].wins
                tot[key].losses += r[key].losses
                tot[key].undecided += r[key].undecided
        print(f"  --- TOTAL {tf_name} ---")
        for key in ["breakout", "mean_revert"]:
            print(fmt(tot[key]))
        print()


if __name__ == "__main__":
    main()
