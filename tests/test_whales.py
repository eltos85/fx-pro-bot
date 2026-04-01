"""Тесты для whale-трекера: COT, sentiment, store source, tracker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.whales.cot import (
    FUTURES_TO_SYMBOL,
    INVERTED_PAIRS,
    CotSignal,
    _find_col,
)
from fx_pro_bot.whales.sentiment import (
    CONTRARIAN_THRESHOLD,
    SentimentSignal,
    fetch_sentiment_signals,
)


# ── COT ──────────────────────────────────────────────────────


def test_futures_to_symbol_mapping() -> None:
    assert "EURO FX" in FUTURES_TO_SYMBOL
    assert FUTURES_TO_SYMBOL["EURO FX"] == "EURUSD=X"
    assert FUTURES_TO_SYMBOL["GOLD"] == "GC=F"


def test_inverted_pairs() -> None:
    assert "USDJPY=X" in INVERTED_PAIRS
    assert "USDCAD=X" in INVERTED_PAIRS
    assert "EURUSD=X" not in INVERTED_PAIRS


def test_find_col_exact() -> None:
    cols = ["Market and Exchange Names", "Open Interest", "Some Other"]
    assert _find_col(cols, ["Market and Exchange Names"]) == "Market and Exchange Names"


def test_find_col_normalized() -> None:
    cols = ["Market_and_Exchange_Names", "Open_Interest"]
    assert _find_col(cols, ["Market and Exchange Names"]) == "Market_and_Exchange_Names"


def test_find_col_not_found() -> None:
    cols = ["foo", "bar"]
    assert _find_col(cols, ["baz", "qux"]) is None


def test_cot_signal_dataclass() -> None:
    sig = CotSignal(
        symbol="EURUSD=X",
        direction=TrendDirection.LONG,
        net_position=50000,
        net_change=5000,
        long_pct=62.5,
        report_date="2026-03-24",
    )
    assert sig.symbol == "EURUSD=X"
    assert sig.direction == TrendDirection.LONG
    assert sig.long_pct == 62.5


# ── Sentiment ────────────────────────────────────────────────


def test_sentiment_signal_dataclass() -> None:
    sig = SentimentSignal(
        symbol="EURUSD=X",
        direction=TrendDirection.SHORT,
        retail_long_pct=75.0,
        retail_short_pct=25.0,
        total_positions=5000,
    )
    assert sig.direction == TrendDirection.SHORT
    assert sig.retail_long_pct == 75.0


def test_sentiment_no_credentials() -> None:
    result = fetch_sentiment_signals(email="", password="")
    assert result == []


def test_sentiment_contrarian_logic() -> None:
    assert CONTRARIAN_THRESHOLD == 70.0


@patch("fx_pro_bot.whales.sentiment.MyfxbookClient")
def test_sentiment_fetch_with_mock(mock_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.get_community_outlook.return_value = [
        {
            "name": "EURUSD",
            "longPercentage": 75,
            "shortPercentage": 25,
            "totalPositions": 5000,
        },
        {
            "name": "GBPUSD",
            "longPercentage": 50,
            "shortPercentage": 50,
            "totalPositions": 3000,
        },
        {
            "name": "USDJPY",
            "longPercentage": 20,
            "shortPercentage": 80,
            "totalPositions": 4000,
        },
    ]
    mock_cls.return_value = mock_client

    signals = fetch_sentiment_signals(email="test@test.com", password="pass")
    assert len(signals) == 3

    eurusd = next(s for s in signals if s.symbol == "EURUSD=X")
    assert eurusd.direction == TrendDirection.SHORT

    gbpusd = next(s for s in signals if s.symbol == "GBPUSD=X")
    assert gbpusd.direction == TrendDirection.FLAT

    usdjpy = next(s for s in signals if s.symbol == "USDJPY=X")
    assert usdjpy.direction == TrendDirection.LONG


# ── Store: source column ─────────────────────────────────────


def test_store_source_column(tmp_path) -> None:
    db = tmp_path / "t.sqlite"
    store = StatsStore(db)

    sid1 = store.record_suggestion(
        instrument="EURUSD=X",
        direction="long",
        advice_text="ensemble test",
        reasons=("ma_cross_up",),
        price_at_signal=1.1,
        events_context=None,
        source="ensemble",
    )

    sid2 = store.record_suggestion(
        instrument="EURUSD=X",
        direction="long",
        advice_text="COT whale signal",
        reasons=("COT net=+50000",),
        price_at_signal=1.1,
        events_context=None,
        source="whale_cot",
    )

    recent = store.list_recent(limit=5)
    sources = {r.id: r.source for r in recent}
    assert sources[sid1] == "ensemble"
    assert sources[sid2] == "whale_cot"


def test_store_source_default(tmp_path) -> None:
    db = tmp_path / "t.sqlite"
    store = StatsStore(db)

    sid = store.record_suggestion(
        instrument="GBPUSD=X",
        direction="short",
        advice_text="default source",
        reasons=("test",),
        price_at_signal=1.3,
        events_context=None,
    )

    recent = store.list_recent(limit=1)
    assert recent[0].source == "ensemble"


def test_store_verification_summary_by_source(tmp_path) -> None:
    db = tmp_path / "t.sqlite"
    store = StatsStore(db)

    sid1 = store.record_suggestion(
        instrument="EURUSD=X",
        direction="long",
        advice_text="ens",
        reasons=("test",),
        price_at_signal=1.1,
        events_context=None,
        source="ensemble",
    )
    store.record_verification(
        suggestion_id=sid1,
        horizon_minutes=15,
        price_at_check=1.12,
        profit_pips=20.0,
        verdict="right",
    )

    sid2 = store.record_suggestion(
        instrument="EURUSD=X",
        direction="short",
        advice_text="cot",
        reasons=("COT",),
        price_at_signal=1.1,
        events_context=None,
        source="whale_cot",
    )
    store.record_verification(
        suggestion_id=sid2,
        horizon_minutes=15,
        price_at_check=1.08,
        profit_pips=20.0,
        verdict="right",
    )

    by_source = store.verification_summary_by_source()
    assert len(by_source) == 2
    sources = {r["source"] for r in by_source}
    assert "ensemble" in sources
    assert "whale_cot" in sources


def test_store_migrate_existing_db(tmp_path) -> None:
    """Проверяем что миграция добавляет source к старой БД."""
    import sqlite3

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE suggestions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            instrument TEXT NOT NULL,
            direction TEXT NOT NULL,
            advice_text TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            price_at_signal REAL,
            events_context TEXT,
            verdict TEXT NOT NULL DEFAULT 'pending',
            verdict_at TEXT,
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE verifications (
            id TEXT PRIMARY KEY,
            suggestion_id TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            price_at_check REAL NOT NULL,
            profit_pips REAL NOT NULL,
            verdict TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            UNIQUE(suggestion_id, horizon_minutes)
        )
        """
    )
    conn.execute(
        "INSERT INTO suggestions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("old-id", "2026-01-01T00:00:00+00:00", "EURUSD=X", "long", "old",
         '["test"]', 1.1, None, "pending", None, None),
    )
    conn.commit()
    conn.close()

    store = StatsStore(db)
    recent = store.list_recent(limit=1)
    assert recent[0].source == "ensemble"
    assert recent[0].id == "old-id"
