"""US macro rates feed: DXY, UST10Y nominal, TIPS ETF (real-yield proxy).

Закрывает Phase 2 D1 (NEXT_PHASE_AI_FX_TRADER.md): SYSTEM_PROMPT за ~20
мест ссылается на DXY и real yields как **главные драйверы золота**
("hierarchy: real yields → DXY → central banks → geopol → ETF/COT") и
прямо обещает их в context ("We see DXY proxy 24h change in context"),
но `collect_market_context` исторически их не передавал. Этот модуль —
data-feed, не торговая логика; промпт уже умеет с ними работать.

──────────────────────────────────────────────────────────────────────
Research basis (для compliance с `strategy-guard.mdc` и `api-docs.mdc`):

| Тезис | Источник |
|---|---|
| Gold ↔ DXY inverse corr -0.6 … -0.8 | Erb & Harvey (2013) «The Golden |
|                                     | Dilemma», NBER Working Paper 18706 |
| Gold ↔ real yields R² ≈ 0.55 (2003-2024) | World Gold Council (2024)  |
|                                          | «Gold and real interest rates»|
| TIPS yield = real cost of money proxy | Fed Board (2007) «TIPS and the |
|                                       | inflation risk premium»         |
| Oil ↔ DXY corr -0.3 … -0.5 (weaker)    | Akram (2009) Energy Economics  |

──────────────────────────────────────────────────────────────────────
Источники данных (free, без API ключей):

| Ticker (yfinance) | Что это | Источник правды (canonical) |
|---|---|---|
| `DX-Y.NYB` | ICE US Dollar Index futures (DXY) spot | ICE «US Dollar Index Futures», https://www.theice.com/products/194/US-Dollar-Index-Futures |
| `^TNX`     | CBOE 10-Year Treasury Yield Index | CBOE TNX product page; values уже в % (4.31 = 4.31%) |
| `TIP`      | iShares TIPS Bond ETF (real-yield proxy, inverse) | iShares product; price ↑ ↔ real yields ↓ |

Real-yields proxy через TIP ETF — направленческий (direction), не точное
число. Для точного real-yield 10Y нужен FRED `DFII10` (требует FRED API
key — не подключаем, чтобы не плодить секреты ради ±5 bps точности).
LLM получает 24h-direction TIP, чего достаточно для confluence-check
("DXY weakening + TIP rising = real yields easing → gold-bullish").

──────────────────────────────────────────────────────────────────────
Реализация:

- Тянем daily history `period="10d"` (для надёжного 5d-окна с holiday-buffer).
- Из дневных closes считаем: spot, 24h Δ (vs iloc[-2]), 5d Δ (vs iloc[-6]).
- Если истории < N точек → возвращаем `None` для соответствующего Δ
  (graceful degradation, не падаем).
- Cache TTL = 30 мин (rates двигаются медленно, но при FOMC events
  внутри дня бывает 5-10 bps swings — 30 мин даёт enough freshness без
  лишних HTTP-запросов).
- yfinance throws → ловим Exception, возвращаем последний кэш (или None).
- ^TNX legacy quirk: исторически возвращался как `yield × 10` (43.10 для
  4.31%), современный yfinance возвращает уже в % (4.31). Делаем
  normalize: если raw > 25 — делим на 10 (25% nominal yield — за пределы
  любой эмиссии US Treasury в обозримой истории).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Yahoo Finance тикеры. Источники документации см. в docstring модуля.
_TICKER_DXY = "DX-Y.NYB"
_TICKER_UST10Y = "^TNX"
_TICKER_TIPS_ETF = "TIP"


@dataclass
class MacroRatesSnapshot:
    """Snapshot US macro rates на момент fetch.

    Все Δ-поля могут быть None если истории недостаточно (например,
    провайдер вернул < 6 daily closes — частая ситуация после длинного
    weekend / US holiday).
    """

    # DXY (US Dollar Index, ICE futures)
    dxy_last: float | None
    dxy_change_24h_pct: float | None
    dxy_change_5d_pct: float | None
    # UST10Y nominal yield в % (4.31 = 4.31%)
    ust10y_last_pct: float | None
    ust10y_change_24h_bps: float | None  # 1 bp = 0.01%
    ust10y_change_5d_bps: float | None
    # TIPS ETF (iShares TIP), real-yield proxy. Inverse-correlated с real yields.
    tip_last: float | None
    tip_change_24h_pct: float | None
    tip_change_5d_pct: float | None
    # ISO timestamp когда сняли snapshot.
    fetched_at_utc: str


class MacroRatesProvider:
    """Кэширующий yfinance-клиент для DXY / UST10Y / TIP.

    Cache TTL = 30 мин по умолчанию (см. модуль docstring). yfinance
    без API-ключа; графический degrade если symbol недоступен.
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
        tip = _fetch_pct_series(_TICKER_TIPS_ETF)

        # UST10Y normalize (см. модуль docstring): если >25 — это
        # legacy "yield × 10", делим обратно. Иначе уже в %.
        ust10y_last = ust10y_raw["last"]
        ust10y_24h_raw = ust10y_raw["pct_24h"]
        ust10y_5d_raw = ust10y_raw["pct_5d"]
        prev_close_24h = ust10y_raw["prev_close_24h"]
        prev_close_5d = ust10y_raw["prev_close_5d"]
        if ust10y_last is not None and ust10y_last > 25.0:
            ust10y_last = ust10y_last / 10.0
            if prev_close_24h is not None:
                prev_close_24h = prev_close_24h / 10.0
            if prev_close_5d is not None:
                prev_close_5d = prev_close_5d / 10.0

        # 24h/5d Δ в bps = (last_pct - prev_close_pct) * 100.
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

        # Если все три источника пустые — нет смысла возвращать snapshot,
        # пусть formatter увидит None и пропустит блок.
        if (
            dxy["last"] is None
            and ust10y_last is None
            and tip["last"] is None
        ):
            log.warning(
                "MacroRates: ни один из тикеров не вернул данных "
                "(DXY/UST10Y/TIP) — пропускаю snapshot"
            )
            return None

        return MacroRatesSnapshot(
            dxy_last=dxy["last"],
            dxy_change_24h_pct=dxy["pct_24h"],
            dxy_change_5d_pct=dxy["pct_5d"],
            ust10y_last_pct=ust10y_last,
            ust10y_change_24h_bps=ust10y_24h_bps,
            ust10y_change_5d_bps=ust10y_5d_bps,
            tip_last=tip["last"],
            tip_change_24h_pct=tip["pct_24h"],
            tip_change_5d_pct=tip["pct_5d"],
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def _fetch_pct_series(ticker: str) -> dict[str, float | None]:
    """Тянет daily history через yfinance и считает spot + 24h + 5d Δ.

    Возвращает dict с ключами: ``last`` (последний Close), ``pct_24h``
    (% change vs iloc[-2]), ``pct_5d`` (% change vs iloc[-6]),
    ``prev_close_24h`` и ``prev_close_5d`` (нужны для UST10Y bps-расчёта
    вне этой функции).

    Все поля могут быть None если ряд короче ожидаемого (US holiday
    streak, illiquid symbol). Не raise'ит на пустых данных, только
    на network-failure (которое ловит caller).
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

    closes = [float(x) for x in df["Close"].tolist() if x == x]  # filter NaN
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

    Формат — компактный, по одной строке на источник, с явной
    разметкой direction для каждого ряда. LLM получает три цифры по
    DXY/UST10Y/TIP и сам интерпретирует confluence (так и было задумано
    в SYSTEM_PROMPT — мы не делаем за него вывод, только подаём данные).

    Возвращает ``None`` если snapshot пуст (нет ни одного из трёх рядов)
    или snap == None.
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
    if snap.tip_last is not None:
        d24 = (
            f"24h={snap.tip_change_24h_pct:+.2f}%"
            if snap.tip_change_24h_pct is not None
            else "24h=n/a"
        )
        d5 = (
            f"5d={snap.tip_change_5d_pct:+.2f}%"
            if snap.tip_change_5d_pct is not None
            else "5d=n/a"
        )
        lines.append(
            f"TIP (iShares TIPS ETF, real-yields proxy — price↑ ↔ real yields↓): "
            f"${snap.tip_last:.2f} ({d24}, {d5})"
        )

    if not lines:
        return None

    header = (
        "=== US MACRO RATES (gold/oil drivers; gold-canonical hierarchy: "
        "real yields → DXY) ==="
    )
    return "\n".join([header, *lines, f"(fetched {snap.fetched_at_utc} UTC)"])
