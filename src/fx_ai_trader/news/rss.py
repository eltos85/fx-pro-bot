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
    # Узкие EIA / API фразы — без голого "eia", который ловил
    # «EIA Natural Gas Storage Report» (cross-contamination, BUILDLOG
    # 2026-05-22). Контейнер «api report» убран как слишком broad
    # (ловил random "API" в нефтегазе).
    "eia crude", "eia weekly petroleum", "crude inventory",
    "crude inventories", "crude stocks", "api crude",
    "pipeline", "refinery", "refineries", "refining",
    "strait of hormuz", "red sea", "houthi",
    "iran sanctions", "iran nuclear",
    "russia oil", "russia sanctions", "russian crude",
    "saudi arabia", "uae oil", "kuwait oil",
    "spr ", "strategic petroleum reserve",
)

# Exclude-rules: если text содержит exclude-keyword этого symbol — он
# НЕ относится к этому bucket даже при keyword-match. Источник анализа:
# diagnostic 2026-05-22 на 12 RSS items показал 17% cross-contamination
# (например «India Explores Alternative Energy Amid Oil Supply Shock»
# попадал и в BZ=F и в NG=F через respective keywords).
# Принцип: если новость явно про другой instrument, мы не хотим её
# в текущем bucket. Не симметрично: gas-specific exclude для OIL,
# oil-specific exclude для GAS, energy-specific exclude для GOLD.
GOLD_EXCLUDE = (
    "natural gas", "lng", "henry hub",
    "crude oil", "brent", "wti",
    "opec",
    # Для substring false-positives «gold» в словах вроде «Goldman»,
    # «Marigold», «Goldilocks» используем word-boundary в classifier
    # (см. _matches_keyword), не grep по exclude — иначе теряем
    # легитимные Goldman-gold-reports. См. BUILDLOG 2026-05-22.
)
OIL_EXCLUDE = (
    # gas-specific phrases — gas news не должен попадать в BZ=F bucket.
    "natural gas storage", "ng storage", "natgas storage",
    "henry hub", "lng cargo", "lng cargoes", "lng feedgas",
    "feedgas", "feed gas",
    # weather-related — drivers газа, не нефти.
    "noaa", "cpc outlook", "hdd", "cdd",
    "heating degree", "cooling degree",
)
GAS_EXCLUDE = (
    # oil-specific phrases — oil news не должен попадать в NG=F bucket.
    "crude oil", "crude inventory", "crude inventories", "crude stocks",
    "brent", "wti", "petroleum", "opec", "opec+",
    "strait of hormuz", "houthi", "red sea",
)
SYMBOL_EXCLUDE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "XAUUSD": GOLD_EXCLUDE,
    "BZ=F": OIL_EXCLUDE,
    "NG=F": GAS_EXCLUDE,
}

# Газ-keywords подобраны по research-источникам (см. prompts.py): EIA
# Weekly NatGas Storage Report, NOAA HDD/CDD outlooks, LNG export news,
# Henry Hub vs TTF spread, Baker Hughes rig count.
# Расширение 2026-05-21 (BUILDLOG NG enhancement v1.2): добавлены
# basin names (Marcellus, Permian, Haynesville), terminal names
# (Cameron, Freeport, Cove Point, Elba Island, Calcasieu Pass), basis
# hubs (Waha, Algonquin), industry analysts (Reuters Kemp, S&P Platts).
# Цель: расширить охват источников после post-mortem 11 NG-трейдов
# (WR 18%, 9/11 BUY на одной сессии 20.05).
GAS_KEYWORDS = (
    "natural gas", "nat gas", "nat-gas", " gas ", "lng",
    "henry hub", "ttf", "title transfer facility", "dutch ttf",
    "european gas", "us-eu spread", "asia jkm", "jkm price",
    # LNG terminals (Bcf/d feedgas — Bloomberg/Reuters track these daily).
    "freeport lng", "sabine pass", "corpus christi",
    "cameron lng", "cove point", "elba island", "calcasieu pass",
    "plaquemines lng", "rio grande lng",
    "cheniere", "venture global", "next decade", "tellurian",
    # Basins (production-side news).
    "marcellus", "appalachia", "haynesville", "permian", "eagle ford",
    "barnett", "utica",
    # Basis hubs (regional Henry Hub vs cash differentials).
    "waha", "algonquin", "transco zone", "michcon",
    # Storage cycle.
    "storage report", "storage build", "storage draw",
    "working gas in storage", "ng storage", "natgas storage",
    "injection season", "withdrawal season",
    # Weather drivers.
    "hdd", "cdd", "heating degree", "cooling degree",
    "cold snap", "heatwave", "heat wave", "polar vortex",
    "arctic blast", "warm winter", "mild winter",
    "noaa", "weather forecast", "winter outlook", "summer outlook",
    "cpc outlook", "6-10 day", "8-14 day",
    # Pipeline / outage.
    "pipeline outage", "gas pipeline", "force majeure",
    "compressor station", "feedgas",
    # Supply-side.
    "rig count", "baker hughes",
    "feed gas", "lng exports", "lng cargo", "lng cargoes",
    "associated gas", "dry gas production",
    # Industry analysts often cited in NG.
    "john kemp", "jkempenergy", "spglobal natgas",
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


def _matches_keyword(keyword: str, text_lower: str) -> bool:
    """Match keyword в text с word-boundary для single-word коротких keywords.

    Цель: «gold» **не** должно матчиться в «Goldman», «oil» в «boiler»,
    «gas» в «biogas». Для multi-word фраз («natural gas», «strait of
    hormuz») используем substring как и раньше.

    Эвристика: если keyword содержит пробел или дефис — это фраза,
    substring match; иначе одиночное слово — \\b-boundary regex.

    См. BUILDLOG 2026-05-22 (cross-contamination diagnostic).
    """
    keyword = keyword.strip()
    if not keyword:
        return False
    # Multi-word phrase (содержит пробел) → substring match.
    if " " in keyword:
        return keyword in text_lower
    # Single word → word-boundary check.
    pattern = re.compile(rf"\b{re.escape(keyword)}\b")
    return bool(pattern.search(text_lower))


def _classify_symbols(text: str, allowed: Sequence[str]) -> list[str]:
    """Возвращает список символов из allowed, упомянутых в тексте.

    С 2026-05-22 (BUILDLOG): применяется двухэтапная фильтрация:
    1. INCLUDE: text содержит хотя бы один keyword из ``SYMBOL_KEYWORDS[sym]``
       (через ``_matches_keyword`` — word-boundary для single-word).
    2. EXCLUDE: text НЕ содержит ни одного keyword из
       ``SYMBOL_EXCLUDE_KEYWORDS[sym]`` (если он определён).

    Цель: исключить cross-contamination между oil/gas/gold buckets.
    Пример: «EIA Natural Gas Storage Report» больше не попадёт в OIL
    (узкие фразы 'eia crude' etc.); «India Oil Supply Shock» не
    попадёт в NG=F bucket (исключено через 'opec'/'crude oil' в
    GAS_EXCLUDE); «Goldman Sachs» не матчит "gold" (word-boundary).
    """
    t = text.lower()
    out: list[str] = []
    for sym in allowed:
        keywords = SYMBOL_KEYWORDS.get(sym, ())
        if not any(_matches_keyword(k, t) for k in keywords):
            continue
        excludes = SYMBOL_EXCLUDE_KEYWORDS.get(sym, ())
        if excludes and any(_matches_keyword(k, t) for k in excludes):
            continue
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
