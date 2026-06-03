"""Phase 1 (2026-05-29): живой поток spot-цены cTrader.

Контекст (BUILDLOG_AI_FX_TRADER.md 2026-05-29):
бот узнавал цену рывками через ProtoOAGetTrendbarsReq (H1/M1-close),
цена могла отставать. Видели «current price unavailable for BZ=F» и
устаревшую цену в решениях. Фаза 1 — подписка на ProtoOASubscribeSpots,
кэш последней живой цены, get_current_price предпочитает spot mid.

Док cTrader: help.ctrader.com/open-api/messages/
- ProtoOASpotEvent.bid/ask в 1/100000 единицы цены.
- Первый event после подписки содержит latest price.

Тесты:
- ``CTraderClient._handle_spot_event`` / ``get_spot_price``: парсинг,
  scaling /100000, merge частичных bid/ask, mid, свежесть, unknown sym.
- ``CTraderFxAdapter.get_current_price``: приоритет spot mid, фолбэк на
  M1-close (нет spot / устарел / live_price disabled).
"""

from __future__ import annotations

import os
import time
import types

import pytest

os.environ.setdefault("AI_FX_TRADER_DEEPSEEK_API_KEY", "test-key")

from fx_pro_bot.trading.client import CTraderClient, _SPOT_PRICE_SCALE  # noqa: E402


class FakeSpotEvent:
    """Минимальный дубль ProtoOASpotEvent (proto2 optional bid/ask)."""

    def __init__(self, symbol_id: int, bid: int | None = None, ask: int | None = None):
        self.symbolId = symbol_id
        self._present: set[str] = set()
        if bid is not None:
            self.bid = bid
            self._present.add("bid")
        if ask is not None:
            self.ask = ask
            self._present.add("ask")

    def HasField(self, name: str) -> bool:
        return name in self._present


def _make_client() -> CTraderClient:
    # __init__ не подключается к сети — безопасно для unit-тестов.
    return CTraderClient(
        client_id="cid",
        client_secret="secret",
        access_token="tok",
        account_id=123,
    )


class TestSpotEventParsing:
    def test_bid_ask_scaling(self):
        c = _make_client()
        # gold 2000.12345 → raw 200012345
        c._handle_spot_event(FakeSpotEvent(41, bid=200012345, ask=200022345))
        spot = c.get_spot_price(41)
        assert spot is not None
        assert spot["bid"] == pytest.approx(2000.12345)
        assert spot["ask"] == pytest.approx(2000.22345)
        assert spot["mid"] == pytest.approx((2000.12345 + 2000.22345) / 2)

    def test_scale_constant_is_100000(self):
        assert _SPOT_PRICE_SCALE == 100_000

    def test_partial_update_merges_sides(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(1, bid=8012000, ask=8013000))
        # следующий event несёт только новый bid — ask сохраняется
        c._handle_spot_event(FakeSpotEvent(1, bid=8011500))
        spot = c.get_spot_price(1)
        assert spot["bid"] == pytest.approx(80.115)
        assert spot["ask"] == pytest.approx(80.13)

    def test_only_bid_present_mid_is_bid(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(7, bid=300000))
        spot = c.get_spot_price(7)
        assert spot["ask"] is None
        assert spot["mid"] == pytest.approx(3.0)

    def test_zero_symbol_id_ignored(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(0, bid=100000))
        assert c.get_spot_price(0) is None

    def test_unknown_symbol_returns_none(self):
        c = _make_client()
        assert c.get_spot_price(999) is None


class TestSpotFreshness:
    def test_fresh_spot_returned(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(41, bid=200000000, ask=200000000))
        assert c.get_spot_price(41, max_age_sec=300) is not None

    def test_stale_spot_filtered(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(41, bid=200000000, ask=200000000))
        # искусственно состарим
        c._spot_prices[41]["ts"] = time.time() - 1000
        assert c.get_spot_price(41, max_age_sec=300) is None
        # без порога — отдаём (свежесть не критична при живом TCP)
        assert c.get_spot_price(41, max_age_sec=None) is not None

    def test_age_sec_reported(self):
        c = _make_client()
        c._handle_spot_event(FakeSpotEvent(41, bid=200000000, ask=200000000))
        c._spot_prices[41]["ts"] = time.time() - 5
        spot = c.get_spot_price(41)
        assert spot["age_sec"] >= 5


class TestSubscriptionState:
    def test_subscribed_symbols_tracked_on_resubscribe_clear(self):
        c = _make_client()
        # эмулируем что подписка была оформлена
        c._subscribed_symbols.update({41, 1117})
        c._handle_spot_event(FakeSpotEvent(41, bid=1, ask=1))
        assert c.get_spot_price(41) is not None
        # _resubscribe_spots должен очистить кэш цен (stale после reconnect).
        # send упадёт (нет реактора/клиента) → ловится внутри, кэш всё равно
        # очищен ДО попытки send.
        c._resubscribe_spots()
        assert c.get_spot_price(41) is None
        # подписки в наборе сохранены для переоформления
        assert c._subscribed_symbols == {41, 1117}


# ─── adapter: приоритет spot mid над M1-close ───────────────────────────

from fx_ai_trader.config.settings import AiFxTraderSettings  # noqa: E402
from fx_ai_trader.trading.client_adapter import Bar, CTraderFxAdapter  # noqa: E402


def _make_adapter(live_enabled: bool = True) -> CTraderFxAdapter:
    settings = AiFxTraderSettings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(settings, "live_price_enabled", live_enabled)
    return CTraderFxAdapter(settings)


def _fake_symbol_info(symbol_id: int):
    return types.SimpleNamespace(symbol_id=symbol_id, name="XAUUSD")


class TestAdapterGetCurrentPrice:
    def test_prefers_live_spot_mid(self, monkeypatch):
        adapter = _make_adapter(live_enabled=True)
        adapter._client = types.SimpleNamespace(
            get_spot_price=lambda sid, max_age_sec=None: {"mid": 2010.5},
        )
        monkeypatch.setattr(adapter, "get_symbol_info", lambda s: _fake_symbol_info(41))
        # get_bars не должен вызываться
        monkeypatch.setattr(
            adapter, "get_bars",
            lambda *a, **k: pytest.fail("get_bars не должен вызываться при live spot"),
        )
        assert adapter.get_current_price("XAUUSD") == pytest.approx(2010.5)

    def test_falls_back_to_m1_close_when_no_spot(self, monkeypatch):
        adapter = _make_adapter(live_enabled=True)
        adapter._client = types.SimpleNamespace(
            get_spot_price=lambda sid, max_age_sec=None: None,
        )
        monkeypatch.setattr(adapter, "get_symbol_info", lambda s: _fake_symbol_info(41))
        monkeypatch.setattr(
            adapter, "get_bars",
            lambda *a, **k: [Bar(ts=1, open=1, high=1, low=1, close=1999.9, volume=1)],
        )
        assert adapter.get_current_price("XAUUSD") == pytest.approx(1999.9)

    def test_live_disabled_uses_m1_close(self, monkeypatch):
        adapter = _make_adapter(live_enabled=False)
        adapter._client = types.SimpleNamespace(
            get_spot_price=lambda sid, max_age_sec=None: pytest.fail(
                "get_spot_price не должен вызываться при live_price disabled"
            ),
        )
        monkeypatch.setattr(adapter, "get_symbol_info", lambda s: _fake_symbol_info(41))
        monkeypatch.setattr(
            adapter, "get_bars",
            lambda *a, **k: [Bar(ts=1, open=1, high=1, low=1, close=1980.0, volume=1)],
        )
        assert adapter.get_current_price("XAUUSD") == pytest.approx(1980.0)

    def test_none_when_no_spot_and_no_bars(self, monkeypatch):
        adapter = _make_adapter(live_enabled=True)
        adapter._client = types.SimpleNamespace(
            get_spot_price=lambda sid, max_age_sec=None: None,
        )
        monkeypatch.setattr(adapter, "get_symbol_info", lambda s: _fake_symbol_info(41))
        monkeypatch.setattr(adapter, "get_bars", lambda *a, **k: [])
        assert adapter.get_current_price("XAUUSD") is None


class _LivePriceAdapter:
    """Adapter c расходящимися live spot и H1-close — для context-теста."""

    def __init__(self, *, live_price: float | None, h1_close: float) -> None:
        self._live = live_price
        self._h1 = h1_close

    def get_bars(self, sym, period_minutes, count):
        n = max(count, 30)
        return [
            Bar(ts=i, open=self._h1, high=self._h1, low=self._h1,
                close=self._h1, volume=1)
            for i in range(n)
        ]

    def get_current_price(self, internal_symbol):
        return self._live


class TestContextUsesLivePrice:
    """2026-06-02: current_price в промпте = get_current_price (live spot),
    а не bars_1h[-1].close. Закрывает разрыв Phase 1 ↔ collect_market_context."""

    def test_full_context_uses_live_spot_over_h1_close(self):
        from unittest.mock import MagicMock

        from fx_ai_trader.trading.context import collect_market_context

        adapter = _LivePriceAdapter(live_price=2055.7, h1_close=2010.0)
        store = MagicMock()
        store.get_open_positions.return_value = []

        ctx = collect_market_context(adapter, store, ("XAUUSD",), 500.0)
        assert ctx.snapshots[0].current_price == pytest.approx(2055.7)

    def test_full_context_falls_back_to_h1_when_no_live(self):
        from unittest.mock import MagicMock

        from fx_ai_trader.trading.context import collect_market_context

        adapter = _LivePriceAdapter(live_price=None, h1_close=2010.0)
        store = MagicMock()
        store.get_open_positions.return_value = []

        ctx = collect_market_context(adapter, store, ("XAUUSD",), 500.0)
        assert ctx.snapshots[0].current_price == pytest.approx(2010.0)


class _PriceAvailAdapter:
    """Adapter с заданной картой цен (None = рынок закрыт)."""

    def __init__(self, prices: dict[str, float | None]) -> None:
        self._prices = prices

    def get_current_price(self, internal_symbol: str) -> float | None:
        return self._prices.get(internal_symbol)


class TestTradableSymbolsGuard:
    """Market-hours guard (2026-06-03): tradable = есть живая цена."""

    def test_all_open_returns_all(self):
        from fx_ai_trader.app.main import _tradable_symbols

        a = _PriceAvailAdapter({"XAUUSD": 2400.0, "BZ=F": 80.0, "NG=F": 3.1})
        assert _tradable_symbols(a, ["XAUUSD", "BZ=F", "NG=F"]) == [
            "XAUUSD", "BZ=F", "NG=F"
        ]

    def test_weekend_all_closed_returns_empty(self):
        from fx_ai_trader.app.main import _tradable_symbols

        a = _PriceAvailAdapter({"XAUUSD": None, "BZ=F": None, "NG=F": None})
        assert _tradable_symbols(a, ["XAUUSD", "BZ=F", "NG=F"]) == []

    def test_partial_open_returns_only_tradable(self):
        from fx_ai_trader.app.main import _tradable_symbols

        a = _PriceAvailAdapter({"XAUUSD": 2400.0, "BZ=F": None, "NG=F": None})
        assert _tradable_symbols(a, ["XAUUSD", "BZ=F", "NG=F"]) == ["XAUUSD"]

    def test_exception_is_swallowed_symbol_skipped(self):
        from fx_ai_trader.app.main import _tradable_symbols

        class _Boom:
            def get_current_price(self, s):
                if s == "BZ=F":
                    raise RuntimeError("client down")
                return 2400.0

        assert _tradable_symbols(_Boom(), ["XAUUSD", "BZ=F"]) == ["XAUUSD"]
