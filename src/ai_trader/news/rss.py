"""RSS-агрегатор новостей для AI-Trader.

Источники (все бесплатные, без auth):
- CoinDesk: https://www.coindesk.com/arc/outboundfeeds/rss/
- CoinTelegraph: https://cointelegraph.com/rss
- Decrypt: https://decrypt.co/feed
- The Block: https://www.theblock.co/rss.xml

Стратегия:
- Кэш в памяти на 10 минут (cycle 15 минут — 1-2 fetch на цикл максимум).
- Парсинг через `feedparser` (стандарт для RSS/Atom).
- Фильтр по символам: ищем ключевые слова в title/summary.
  Маппинг — SYMBOL_KEYWORDS dict ниже. Текущий whitelist v0.14
  (LTC/ATOM/BTC/SUI/LINK + историч. ETH/BNB/XRP/DOGE для backward
  compat при переключении). Если монета не в SYMBOL_KEYWORDS, она
  получит только GENERIC-новости.
- Дедупликация по URL.
- Top-N (default 8) свежих за последние N часов (default 6h).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Sequence

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ─── Маппинг символ → ключевые слова ─────────────────────────────────────

SYMBOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BTCUSDT": ("bitcoin", "btc", "satoshi"),
    "ETHUSDT": ("ethereum", "eth", "ether ", "vitalik"),
    "BNBUSDT": ("binance coin", "bnb", "binance "),
    "XRPUSDT": ("xrp", "ripple"),
    "DOGEUSDT": ("dogecoin", "doge"),
    # 2026-05-20: добавлены пары для нового whitelist (v0.14).
    # Без этих ключей `filter_for_symbols` не подхватит специфичные новости
    # по монете и она получит только GENERIC-новости (макро/Fed/ETF) —
    # это потеря context для LLM. Ключи проверены вручную против
    # CoinDesk/CoinTelegraph/Decrypt typical headline patterns.
    "LTCUSDT": ("litecoin", "ltc", "charlie lee", "mweb"),
    "ATOMUSDT": ("cosmos", "atom", "ibc ", "tendermint", "interchain"),
    "SUIUSDT": ("sui ", "sui network", "mysten", "move language"),
    "LINKUSDT": ("chainlink", "link ", "sergey nazarov", "ccip", "oracle network"),
}

# Generic crypto keywords — статья про "crypto market" релевантна всем.
# v0.3 (AUDIT_2026.md): расширено по 2026 macro-фокусу: BTC dominance,
# spot Bitcoin ETF tickers (IBIT — >60% market share), DXY/Fed как
# главные macro-driver'ы (Bybit Outlook 2026, Galaxy Research 2026).
GENERIC_KEYWORDS = (
    "crypto market", "crypto regulation", "sec ", "etf", "spot etf",
    "ibit", "fbtc", "etha", "fethereum",  # ключевые ETF-тикеры
    "fed ", "fomc", "interest rate", "rate cut", "rate hike",
    "powell", "yellen", "treasury",
    "macro", "dxy", "dollar index",
    "stablecoin", "tether", "usdt", "usdc", "circle",
    "btc dominance", "bitcoin dominance", "altseason", "alt-season",
    "halving", "etf flow", "etf inflows", "etf outflows",
    "liquidation", "deleveraging", "open interest", "funding rate",
)


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published_iso: str
    url: str
    symbols: list[str] = field(default_factory=list)


@dataclass
class FeedSource:
    name: str
    url: str


DEFAULT_FEEDS: tuple[FeedSource, ...] = (
    FeedSource("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    FeedSource("CoinTelegraph", "https://cointelegraph.com/rss"),
    FeedSource("Decrypt", "https://decrypt.co/feed"),
)


def _classify_symbols(text: str, allowed: Sequence[str]) -> list[str]:
    """Возвращает список символов из allowed, упомянутых в тексте."""
    t = text.lower()
    out: list[str] = []
    for sym in allowed:
        keywords = SYMBOL_KEYWORDS.get(sym, ())
        if any(k in t for k in keywords):
            out.append(sym)
    return out


def _is_generic_relevant(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in GENERIC_KEYWORDS)


def _entry_published_dt(entry) -> datetime | None:
    """Парсит published_parsed из feedparser; возвращает aware UTC datetime."""
    pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pp:
        return None
    try:
        return datetime(*pp[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class RssNewsProvider:
    """Кэширующий RSS-агрегатор.

    `news_provider.get_recent_news(symbols)` — основной метод.
    Возвращает отфильтрованный, отсортированный, дедуплицированный список.
    """

    def __init__(
        self,
        feeds: Sequence[FeedSource] = DEFAULT_FEEDS,
        cache_ttl_sec: int = 600,
        max_items: int = 8,
        max_age_hours: int = 6,
        request_timeout_sec: int = 10,
    ) -> None:
        self.feeds = list(feeds)
        self.cache_ttl_sec = cache_ttl_sec
        self.max_items = max_items
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
                # feedparser скачивает сам; таймаут через socket.setdefaulttimeout
                # эмулировать сложно, надеемся на быструю отдачу.
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

    def get_recent_news(self, symbols: Sequence[str]) -> list[NewsItem]:
        self._refresh_if_stale()
        cutoff = datetime.now(tz=UTC) - timedelta(hours=self.max_age_hours)

        filtered: list[NewsItem] = []
        seen_urls: set[str] = set()
        for it in self._cache:
            if it.url in seen_urls:
                continue
            seen_urls.add(it.url)

            blob = f"{it.title}\n{it.summary}"
            matched = _classify_symbols(blob, symbols)
            generic = _is_generic_relevant(blob)
            if not matched and not generic:
                continue

            if it.published_iso:
                try:
                    pub = datetime.fromisoformat(it.published_iso)
                    if pub < cutoff:
                        continue
                except ValueError:
                    pass  # без даты — оставляем, но в конце сортировки

            it.symbols = matched
            filtered.append(it)

        # Сортируем по дате убыванию (новые сверху)
        filtered.sort(key=lambda x: x.published_iso, reverse=True)
        return filtered[: self.max_items]
