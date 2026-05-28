"""US macro rates feed для AI-Trader: DXY + UST10Y nominal.

Port из ``src/fx_ai_trader/data/macro_rates.py`` (v0.30, 2026-05-28).
TIPS ETF (`TIP`) исключён — real-yield proxy релевантен для золота,
для крипты он вторичен (canonical crypto-драйвер — Fed policy через DXY
+ nominal yield, не real yield decomposition).

──────────────────────────────────────────────────────────────────────
Research basis (crypto-adapted, см. research отчёт §8.1-8.2):

| Тезис | Источник |
|---|---|
| BTC ↔ DXY 30-day rolling corr -0.72…-0.90 (extreme | BitMEX «DXY Index   |
| -0.90 on 24 Apr 2026)                              | & Bitcoin» 2026     |
|                                                    | https://www.bitmex.com/blog/dxy-index-bitcoin-crypto |
|                                                    | Intellectia 2026-04 |
|                                                    | https://intellectia.ai/blog/bitcoin-dollar-inverse-correlation-2026 |
| BTC ↔ 10Y nominal yield ≈ -0.55 (modestly inverse, | Convex BTC/10Y     |
| weaker than DXY)                                   | analysis            |
|                                                    | https://convextrade.com/compare/bitcoin-vs-10y-yield |
| Threshold для BTC pressure: 10Y >4.7% = risk-off;  | Cryptoslate Fed-    |
| <4.3% = supportive                                 | flip May 2026       |
|                                                    | https://cryptoslate.com/bitcoins-fed-cut-trade-flips-as-bond-market-turns-into-the-risk/ |

Применим к altcoins с beta multiplier (ETH ≈ 1.0-1.2x reaction, SOL ≈
1.4x по research §3.1).

──────────────────────────────────────────────────────────────────────
Источники данных (free, без API ключей):

| Ticker (yfinance) | Что это | Источник правды (canonical) |
|---|---|---|
| ``DX-Y.NYB``      | ICE US Dollar Index futures (DXY) spot | ICE «US Dollar Index Futures», https://www.theice.com/products/194/US-Dollar-Index-Futures |
| ``^TNX``          | CBOE 10-Year Treasury Yield Index | CBOE TNX product page; values в % (4.31 = 4.31%) |

──────────────────────────────────────────────────────────────────────
Реализация — идентично fx_ai_trader (cache TTL 30 мин, normalize TNX
если >25 → /10, graceful degradation при yfinance failure).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


_TICKER_DXY = "DX-Y.NYB"
_TICKER_UST10Y = "^TNX"


@dataclass
class MacroRatesSnapshot:
    """US macro rates snapshot на момент fetch.

    Все Δ-поля могут быть None если истории недостаточно (например,
    провайдер вернул <6 daily closes после длинного weekend / US holiday).
    """

    dxy_last: float | None
    dxy_change_24h_pct: float | None
    dxy_change_5d_pct: float | None
    ust10y_last_pct: float | None
    ust10y_change_24h_bps: float | None
    ust10y_change_5d_bps: float | None
    fetched_at_utc: str


class MacroRatesProvider:
    """Кэширующий yfinance-клиент для DXY / UST10Y.

    Cache TTL = 30 мин (rates двигаются медленно; при FOMC events внутри
    дня бывает 5-10 bps swings — 30 мин даёт enough freshness без лишних
    HTTP-запросов). yfinance без API-ключа; graceful degrade если ticker
    недоступен (возвращаем последний кэш или None).
    """

    def __init__(self, cache_ttl_sec: int = 1800) -> None:
        self._cache_ttl = cache_ttl_sec
        self._cache: MacroRatesSnapshot | None = None
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshot(self) -> MacroRatesSnapshot | None:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            snap = self._fetch_fresh()
        except Exception:
            log.exception(
                "MacroRates fetch failed (продолжаю с прошлым кэшем)"
            )
            return self._cache
        if snap is not None:
            self._cache = snap
            self._cache_ts = now
        return snap or self._cache

    def _fetch_fresh(self) -> MacroRatesSnapshot | None:
        from datetime import UTC, datetime

        dxy = _fetch_pct_series(_TICKER_DXY)
        ust10y_raw = _fetch_pct_series(_TICKER_UST10Y)

        ust10y_last = ust10y_raw["last"]
        prev_close_24h = ust10y_raw["prev_close_24h"]
        prev_close_5d = ust10y_raw["prev_close_5d"]
        if ust10y_last is not None and ust10y_last > 25.0:
            ust10y_last = ust10y_last / 10.0
            if prev_close_24h is not None:
                prev_close_24h = prev_close_24h / 10.0
            if prev_close_5d is not None:
                prev_close_5d = prev_close_5d / 10.0

        ust10y_24h_bps = (
            (ust10y_last - prev_close_24h) * 100.0
            if ust10y_last is not None and prev_close_24h is not None
            else None
        )
        ust10y_5d_bps = (
            (ust10y_last - prev_close_5d) * 100.0
            if ust10y_last is not None and prev_close_5d is not None
            else None
        )

        if dxy["last"] is None and ust10y_last is None:
            log.warning(
                "MacroRates: ни DXY ни UST10Y не вернули данных — пропускаю snapshot"
            )
            return None

        return MacroRatesSnapshot(
            dxy_last=dxy["last"],
            dxy_change_24h_pct=dxy["pct_24h"],
            dxy_change_5d_pct=dxy["pct_5d"],
            ust10y_last_pct=ust10y_last,
            ust10y_change_24h_bps=ust10y_24h_bps,
            ust10y_change_5d_bps=ust10y_5d_bps,
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def _fetch_pct_series(ticker: str) -> dict[str, float | None]:
    """Тянет daily history через yfinance и считает spot + 24h + 5d Δ.

    Не raise'ит на пустых данных, только на network-failure (которое
    ловит caller).
    """
    import yfinance as yf

    out: dict[str, float | None] = {
        "last": None,
        "pct_24h": None,
        "pct_5d": None,
        "prev_close_24h": None,
        "prev_close_5d": None,
    }
    try:
        t = yf.Ticker(ticker)
        df = t.history(period="10d", interval="1d", auto_adjust=False)
    except Exception:
        log.exception("yfinance failure для %s", ticker)
        return out
    if df is None or df.empty or "Close" not in df.columns:
        log.info("MacroRates: пустой DataFrame для %s", ticker)
        return out

    closes = [float(x) for x in df["Close"].tolist() if x == x]
    if not closes:
        return out

    out["last"] = closes[-1]
    if len(closes) >= 2:
        prev_24h = closes[-2]
        out["prev_close_24h"] = prev_24h
        if prev_24h != 0:
            out["pct_24h"] = (closes[-1] - prev_24h) / prev_24h * 100.0
    if len(closes) >= 6:
        prev_5d = closes[-6]
        out["prev_close_5d"] = prev_5d
        if prev_5d != 0:
            out["pct_5d"] = (closes[-1] - prev_5d) / prev_5d * 100.0
    return out


def format_macro_rates_snapshot(snap: MacroRatesSnapshot | None) -> str | None:
    """Превратить snapshot в text-блок для LLM context.

    Возвращает ``None`` если snapshot пуст или snap == None — caller
    пропускает блок в context.
    """
    if snap is None:
        return None

    lines: list[str] = []
    if snap.dxy_last is not None:
        d24 = (
            f"24h={snap.dxy_change_24h_pct:+.2f}%"
            if snap.dxy_change_24h_pct is not None
            else "24h=n/a"
        )
        d5 = (
            f"5d={snap.dxy_change_5d_pct:+.2f}%"
            if snap.dxy_change_5d_pct is not None
            else "5d=n/a"
        )
        lines.append(
            f"DXY (US Dollar Index, ICE futures DX-Y.NYB): "
            f"{snap.dxy_last:.2f} ({d24}, {d5})"
        )
    if snap.ust10y_last_pct is not None:
        d24 = (
            f"24h={snap.ust10y_change_24h_bps:+.1f}bps"
            if snap.ust10y_change_24h_bps is not None
            else "24h=n/a"
        )
        d5 = (
            f"5d={snap.ust10y_change_5d_bps:+.1f}bps"
            if snap.ust10y_change_5d_bps is not None
            else "5d=n/a"
        )
        lines.append(
            f"UST10Y nominal yield (CBOE TNX): "
            f"{snap.ust10y_last_pct:.2f}% ({d24}, {d5})"
        )

    if not lines:
        return None

    header = (
        "=== US MACRO RATES (crypto drivers; BTC↔DXY corr -0.72..-0.90, "
        "BTC↔10Y -0.55) ==="
    )
    return "\n".join([header, *lines, f"(fetched {snap.fetched_at_utc} UTC)"])
