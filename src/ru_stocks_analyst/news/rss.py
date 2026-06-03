"""RSS-агрегатор новостей по рынку РФ (публичные ленты, без auth).

Источники — официальные RSS СМИ; парсинг feedparser (как fx_ai_trader/news).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from ru_stocks_analyst.news.article import NewsArticle
from ru_stocks_analyst.news.ticker_map import (
    STATIC_ALIASES,
    STATIC_TOP_TICKERS,
    build_ticker_index,
    is_market_wide,
    match_tickers,
)

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore[assignment]

log = logging.getLogger("ru_stocks.news")


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str
    weight: float = 1.0


DEFAULT_FEEDS: tuple[FeedSource, ...] = (
    FeedSource("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    FeedSource("Интерфакс", "https://www.interfax.ru/rss"),
    FeedSource("Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml"),
    FeedSource("Ведомости", "https://www.vedomosti.ru/rss/news"),
    FeedSource("ТАСС", "https://tass.ru/rss/v2.xml"),
)


def _norm_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t)


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()[:500]


def _entry_published_dt(entry) -> datetime | None:
    pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pp:
        return None
    try:
        return datetime(*pp[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class RuNewsAggregator:
    def __init__(
        self,
        feeds: tuple[FeedSource, ...] = DEFAULT_FEEDS,
        *,
        cache_ttl_sec: int = 600,
        max_age_hours: int = 36,
        max_per_ticker: int = 4,
        max_market: int = 8,
    ) -> None:
        self.feeds = feeds
        self.cache_ttl_sec = cache_ttl_sec
        self.max_age_hours = max_age_hours
        self.max_per_ticker = max_per_ticker
        self.max_market = max_market
        self._raw: list[NewsArticle] = []
        self._cache_ts: float = 0.0

    def _fetch_all(self) -> list[NewsArticle]:
        if feedparser is None:
            log.error("feedparser не установлен")
            return []
        out: list[NewsArticle] = []
        for src in self.feeds:
            try:
                feed = feedparser.parse(src.url)
            except Exception:
                log.exception("RSS fail %s", src.name)
                continue
            for e in getattr(feed, "entries", None) or []:
                title = (getattr(e, "title", "") or "").strip()
                if not title:
                    continue
                summary = _strip_html(getattr(e, "summary", "") or "")
                pub = _entry_published_dt(e)
                out.append(
                    NewsArticle(
                        title=title,
                        summary=summary,
                        source=src.name,
                        url=getattr(e, "link", "") or "",
                        published_iso=pub.isoformat() if pub else "",
                    )
                )
        log.info("RSS: загружено %d записей из %d лент", len(out), len(self.feeds))
        return out

    def _refresh(self) -> None:
        now = time.time()
        if self._raw and (now - self._cache_ts) < self.cache_ttl_sec:
            return
        self._raw = self._fetch_all()
        self._cache_ts = now

    def collect(
        self,
        portfolio_tickers: list[str],
        *,
        watch_tickers: list[str] | None = None,
    ) -> tuple[list[NewsArticle], dict[str, list[NewsArticle]], list[NewsArticle]]:
        """Возвращает (все свежие, по тикеру, общий рынок)."""
        self._refresh()
        cutoff = datetime.now(tz=UTC) - timedelta(hours=self.max_age_hours)
        index = build_ticker_index(
            portfolio_tickers,
            watch_tickers or (),
        )
        # расширяем индекс топ-ликвидов для широкого скринера
        for t in STATIC_TOP_TICKERS:
            if t not in index:
                index[t] = list(STATIC_ALIASES.get(t, (t.lower(),)))

        seen: set[str] = set()
        by_ticker: dict[str, list[NewsArticle]] = {t: [] for t in index}
        market: list[NewsArticle] = []
        fresh: list[NewsArticle] = []

        for art in self._raw:
            norm = _norm_title(art.title)
            if not norm or norm in seen:
                continue
            if art.published_iso:
                try:
                    if datetime.fromisoformat(art.published_iso) < cutoff:
                        continue
                except ValueError:
                    pass
            seen.add(norm)
            text = f"{art.title}\n{art.summary}"
            tickers = match_tickers(text, index)
            art.tickers = tickers
            art.is_market_wide = is_market_wide(text) and not tickers
            fresh.append(art)
            if tickers:
                for t in tickers:
                    if t in by_ticker and len(by_ticker[t]) < self.max_per_ticker:
                        by_ticker[t].append(art)
            elif art.is_market_wide and len(market) < self.max_market:
                market.append(art)

        # сортировка по дате
        def _sort_key(a: NewsArticle) -> str:
            return a.published_iso

        for t in by_ticker:
            by_ticker[t].sort(key=_sort_key, reverse=True)
        market.sort(key=_sort_key, reverse=True)
        return fresh, by_ticker, market
