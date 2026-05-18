"""RSS-агрегатор новостей для FX AI Trader (commodity-focus).

Источники (все бесплатные, без auth):
- ForexLive: https://www.forexlive.com/feed/commodities
- Investing.com Energy: https://www.investing.com/rss/commodities_388.rss
- OilPrice.com: https://oilprice.com/rss/main
- Kitco: https://www.kitco.com/rss/KitcoNews.xml

Стратегия:
- Кэш в памяти 10 мин (cycle 15 мин — 1–2 fetch на цикл максимум).
- Парсинг через ``feedparser`` (стандарт для RSS/Atom).
- **Двойной keyword filter**: gold-set и oil-set (отдельные ключевые слова),
  один news может быть релевантен сразу обоим.
- Source weights: ForexLive/Investing = 1.0, OilPrice/Kitco = 0.7
  (вторые — commentary-heavy, выше risk шумных headline'ов; research:
  stock-market.live 2026 «weighted relevance»).
- Дедупликация по нормализованному title (lowercase + remove punctuation;
  не по URL — некоторые feeds дают один и тот же article через разные
  redirect-урлы).
- Top-N (default 5) свежих per-symbol за последние N часов (default 12h).

Research basis:
- stock-market.live 2026 «Build a News-Driven Trade Bot» — selectivity > speed.
- stockalpha.ai sentiment guide — entity extraction + time-window aggregation.
- arxiv 2603.11408 «Beyond Polarity» (2026) — multi-dim sentiment intensity
  & uncertainty значимы для commodity (применяется в LLM-prompt, не здесь).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Sequence

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ─── Маппинг symbol → keyword set ────────────────────────────────────────
# Используются yfinance-нотированные internal-символы из settings.symbols.

GOLD_KEYWORDS = (
    "gold", "xau", "bullion", "yellow metal", "precious metal",
    "fed ", "fomc", "powell",
    "inflation", "cpi", "ppi", "core inflation",
    "real yields", "treasury yields",
    "safe haven", "safe-haven",
    "central bank buying", "central bank gold",
    "dxy", "dollar index",
    "rate cut", "rate hike", "interest rate",
    "etf flow", "gld", "iau",
)

OIL_KEYWORDS = (
    "opec", "opec+",
    "oil", "crude", "brent", "wti", "petroleum",
    "inventory", "inventories", "stockpile",
    "eia", "api report",
    "pipeline", "refinery", "refineries", "refining",
    "strait of hormuz", "red sea", "houthi",
    "iran sanctions", "iran nuclear",
    "russia oil", "russia sanctions", "russian crude",
    "saudi arabia", "uae oil", "kuwait oil",
    "spr ", "strategic petroleum reserve",
)

# Газ-keywords подобраны по research-источникам (см. prompts.py): EIA
# Weekly NatGas Storage Report, NOAA HDD/CDD outlooks, LNG export news,
# Henry Hub vs TTF spread, Baker Hughes rig count.
GAS_KEYWORDS = (
    "natural gas", "nat gas", "nat-gas", " gas ", "lng",
    "henry hub", "ttf", "title transfer facility",
    "freeport lng", "sabine pass", "corpus christi",
    "cheniere", "venture global",
    "storage report", "storage build", "storage draw",
    "working gas in storage", "ng storage", "natgas storage",
    "hdd", "cdd", "heating degree", "cooling degree",
    "cold snap", "heatwave", "heat wave", "polar vortex",
    "noaa", "weather forecast", "winter outlook",
    "pipeline outage", "gas pipeline",
    "rig count", "baker hughes",
    "feedgas", "feed gas", "lng exports", "lng cargo",
)


# Mapping symbol → keyword set. Если в settings.symbols пользователь
# добавит ещё инструменты — придётся расширить эту таблицу.
SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "XAUUSD": GOLD_KEYWORDS,
    "BZ=F": OIL_KEYWORDS,
    "NG=F": GAS_KEYWORDS,
}


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published_iso: str
    url: str
    symbols: list[str] = field(default_factory=list)
    source_weight: float = 1.0


@dataclass
class FeedSource:
    name: str
    url: str
    weight: float = 1.0


DEFAULT_FEEDS: tuple[FeedSource, ...] = (
    FeedSource(
        "ForexLive Commodities",
        "https://www.forexlive.com/feed/commodities",
        weight=1.0,
    ),
    FeedSource(
        "Investing Energy",
        "https://www.investing.com/rss/commodities_388.rss",
        weight=1.0,
    ),
    FeedSource("OilPrice", "https://oilprice.com/rss/main", weight=0.7),
    FeedSource(
        "Kitco News", "https://www.kitco.com/rss/KitcoNews.xml", weight=0.7
    ),
)


_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm_title(title: str) -> str:
    """Нормализованный title для дедупликации (lowercase + strip punctuation)."""
    return _PUNCT_RE.sub("", title.lower()).strip()


def _classify_symbols(text: str, allowed: Sequence[str]) -> list[str]:
    """Возвращает список символов из allowed, упомянутых в тексте."""
    t = text.lower()
    out: list[str] = []
    for sym in allowed:
        keywords = SYMBOL_KEYWORDS.get(sym, ())
        if any(k in t for k in keywords):
            out.append(sym)
    return out


def _entry_published_dt(entry) -> datetime | None:
    """Парсит published_parsed из feedparser; возвращает aware UTC datetime."""
    pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pp:
        return None
    try:
        return datetime(*pp[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class CommodityRssNewsProvider:
    """Кэширующий RSS-агрегатор для gold + oil news.

    ``get_recent_news(symbols)`` — основной метод. Возвращает
    отфильтрованный, отсортированный, дедуплицированный список с
    proper source-weight'ом.
    """

    def __init__(
        self,
        feeds: Sequence[FeedSource] = DEFAULT_FEEDS,
        cache_ttl_sec: int = 600,
        max_items_per_symbol: int = 5,
        max_age_hours: int = 12,
        request_timeout_sec: int = 10,
    ) -> None:
        self.feeds = list(feeds)
        self.cache_ttl_sec = cache_ttl_sec
        self.max_items_per_symbol = max_items_per_symbol
        self.max_age_hours = max_age_hours
        self.request_timeout_sec = request_timeout_sec
        self._cache: list[NewsItem] = []
        self._cache_ts: float = 0.0

    def _fetch_all(self) -> list[NewsItem]:
        if feedparser is None:
            log.error("feedparser не установлен; новости недоступны")
            return []
        items: list[NewsItem] = []
        for src in self.feeds:
            try:
                feed = feedparser.parse(src.url)
            except Exception:
                log.exception("RSS fetch failed: %s", src.name)
                continue
            entries = getattr(feed, "entries", None) or []
            for e in entries:
                title = (getattr(e, "title", "") or "").strip()
                summary = (getattr(e, "summary", "") or "").strip()
                url = getattr(e, "link", "") or ""
                if not title:
                    continue
                pub = _entry_published_dt(e)
                pub_iso = pub.isoformat() if pub else ""
                items.append(
                    NewsItem(
                        title=title,
                        summary=summary,
                        source=src.name,
                        published_iso=pub_iso,
                        url=url,
                        source_weight=src.weight,
                    )
                )
        return items

    def _refresh_if_stale(self) -> None:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.cache_ttl_sec:
            return
        log.info("RSS: refreshing cache (%d feeds)", len(self.feeds))
        self._cache = self._fetch_all()
        self._cache_ts = now
        log.info("RSS: cached %d total items", len(self._cache))

    def get_recent_news(self, symbols: Sequence[str]) -> dict[str, list[NewsItem]]:
        """Возвращает per-symbol список релевантных новостей.

        Key схема: ``{"XAUUSD": [NewsItem, ...], "BZ=F": [NewsItem, ...]}``.
        Каждый symbol получает свой top-N после фильтра + сортировки.
        News может попасть в несколько списков если matches keywords обоих
        (gold news про OPEC-decision, etc.).
        """
        self._refresh_if_stale()
        cutoff = datetime.now(tz=UTC) - timedelta(hours=self.max_age_hours)

        # Дедупликация по нормализованному title через весь pool.
        seen_titles: set[str] = set()
        deduped: list[NewsItem] = []
        for it in self._cache:
            t_norm = _norm_title(it.title)
            if not t_norm or t_norm in seen_titles:
                continue
            seen_titles.add(t_norm)
            # Time-window filter (если есть published_iso).
            if it.published_iso:
                try:
                    pub = datetime.fromisoformat(it.published_iso)
                    if pub < cutoff:
                        continue
                except ValueError:
                    pass
            matched = _classify_symbols(f"{it.title}\n{it.summary}", symbols)
            if not matched:
                continue
            it.symbols = matched
            deduped.append(it)

        # Per-symbol bucketing с сортировкой по (published_iso desc, source_weight desc).
        per_symbol: dict[str, list[NewsItem]] = {sym: [] for sym in symbols}
        for it in deduped:
            for sym in it.symbols:
                if sym in per_symbol:
                    per_symbol[sym].append(it)
        for sym in per_symbol:
            per_symbol[sym].sort(
                key=lambda x: (x.published_iso, x.source_weight),
                reverse=True,
            )
            per_symbol[sym] = per_symbol[sym][: self.max_items_per_symbol]

        return per_symbol
