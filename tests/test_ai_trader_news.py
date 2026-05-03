"""Тесты RSS-агрегатора новостей AI-Trader.

Без живых HTTP-запросов: подменяем feedparser.parse на in-memory XML.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from ai_trader.news.rss import (
    DEFAULT_FEEDS,
    FeedSource,
    RssNewsProvider,
    _classify_symbols,
    _is_generic_relevant,
)


ALLOWED = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


def _now_minus(hours: float) -> tuple:
    """Возвращает time.struct_time для published_parsed."""
    dt = datetime.now(tz=UTC) - timedelta(hours=hours)
    return dt.timetuple()


class FakeEntry:
    def __init__(self, title: str, summary: str, link: str, hours_ago: float):
        self.title = title
        self.summary = summary
        self.link = link
        self.published_parsed = _now_minus(hours_ago)


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


class TestClassifySymbols:
    def test_btc_match(self):
        assert "BTCUSDT" in _classify_symbols("Bitcoin hits new high", ALLOWED)

    def test_eth_match_via_alias(self):
        assert "ETHUSDT" in _classify_symbols("Vitalik proposes new feature", ALLOWED)

    def test_no_match(self):
        assert _classify_symbols("Solana surges 20%", ALLOWED) == []

    def test_multiple(self):
        result = _classify_symbols("Bitcoin and Dogecoin rally on news", ALLOWED)
        assert "BTCUSDT" in result and "DOGEUSDT" in result


class TestGenericRelevance:
    def test_etf_relevant(self):
        assert _is_generic_relevant("New crypto ETF approved")

    def test_fed_relevant(self):
        assert _is_generic_relevant("Fed signals rate cut")

    def test_unrelated(self):
        assert not _is_generic_relevant("Cat video goes viral")


class TestRssProvider:
    def _make_feed(self, entries):
        return FakeFeed(entries)

    def test_filter_by_symbol_and_age(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("Test", "http://test/")],
            cache_ttl_sec=0,
            max_items=10,
            max_age_hours=6,
        )
        entries = [
            FakeEntry("Bitcoin price up", "BTC hit 100k", "u1", 1.0),       # match BTC, fresh
            FakeEntry("Solana ecosystem", "SOL grows", "u2", 1.0),          # no match
            FakeEntry("Old Ethereum article", "ETH", "u3", 24.0),           # ETH but too old
            FakeEntry("Dogecoin meme rally", "DOGE up", "u4", 0.5),         # match DOGE
            FakeEntry("Crypto ETF approved", "macro", "u5", 2.0),           # generic
        ]
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.return_value = self._make_feed(entries)
            result = provider.get_recent_news(ALLOWED)

        urls = {it.url for it in result}
        assert "u1" in urls       # BTC, fresh
        assert "u2" not in urls   # no symbol match
        assert "u3" not in urls   # too old
        assert "u4" in urls       # DOGE
        assert "u5" in urls       # generic crypto ETF

    def test_dedup_by_url(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("A", "u1"), FeedSource("B", "u2")],
            cache_ttl_sec=0,
        )
        # Один и тот же URL в двух источниках — должен попасть один раз
        same_entry = FakeEntry("Bitcoin spikes", "BTC", "shared_url", 0.5)
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.return_value = self._make_feed([same_entry])
            result = provider.get_recent_news(ALLOWED)
        assert len([r for r in result if r.url == "shared_url"]) == 1

    def test_max_items_limit(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("Test", "u")],
            cache_ttl_sec=0,
            max_items=3,
        )
        entries = [
            FakeEntry(f"Bitcoin news #{i}", "BTC", f"url{i}", 0.5 + i * 0.1)
            for i in range(10)
        ]
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.return_value = self._make_feed(entries)
            result = provider.get_recent_news(ALLOWED)
        assert len(result) == 3

    def test_cache_avoids_refetch(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("Test", "u")],
            cache_ttl_sec=600,  # большой TTL
        )
        entry = FakeEntry("Bitcoin", "BTC", "u1", 0.5)
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.return_value = self._make_feed([entry])
            provider.get_recent_news(ALLOWED)
            provider.get_recent_news(ALLOWED)
            provider.get_recent_news(ALLOWED)
            # Должен быть ровно 1 fetch (одна обращение на feed × 1 feed)
            assert fp.parse.call_count == 1

    def test_empty_when_feedparser_returns_nothing(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("Test", "u")], cache_ttl_sec=0
        )
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.return_value = FakeFeed([])
            assert provider.get_recent_news(ALLOWED) == []

    def test_handles_fetch_exception(self):
        provider = RssNewsProvider(
            feeds=[FeedSource("Test", "u")], cache_ttl_sec=0
        )
        with patch("ai_trader.news.rss.feedparser") as fp:
            fp.parse.side_effect = RuntimeError("network down")
            # Не должен бросить exception, просто возвращает пустоту
            assert provider.get_recent_news(ALLOWED) == []

    def test_default_feeds_count(self):
        # Sanity: дефолтные источники задаются и их минимум 3
        assert len(DEFAULT_FEEDS) >= 3
