"""Тесты для AI-Trader: парсинг ответа LLM, killswitch, БД."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from ai_trader.state.db import AiTraderStore
from ai_trader.trading.executor import ParsedAction, parse_action


ALLOWED = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


# ─── parse_action ────────────────────────────────────────────────────────


class TestParseAction:
    def test_hold(self):
        text = '{"action": "hold", "reason": "no clear setup"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "hold"

    def test_open_buy_valid(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, '
            '"confidence": 0.65, '
            '"invalidation_condition": "1H closes below 59500 (EMA50 lost)", '
            '"risk_usd": 6.5, '
            '"reason": "breakout"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "open"
        assert result.raw["symbol"] == "BTCUSDT"
        assert result.raw["leverage"] == 3
        assert result.raw["confidence"] == 0.65
        assert result.raw["risk_usd"] == 6.5

    def test_open_with_markdown_fence(self):
        """LLM иногда оборачивает в ```json ... ``` несмотря на инструкцию."""
        text = (
            '```json\n'
            '{"action": "hold", "reason": "wait"}\n'
            '```'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "hold"

    def test_open_with_extra_commentary(self):
        text = (
            "Here is my decision:\n"
            '{"action": "hold", "reason": "consolidation"}\n'
            "Hope this helps!"
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "hold"

    def test_close_valid(self):
        text = '{"action": "close", "position_id": 7, "reason": "tp hit"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "close"
        assert result.raw["position_id"] == 7

    def test_invalid_action(self):
        text = '{"action": "buy_now", "reason": "x"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "invalid action" in result

    def test_unknown_symbol(self):
        text = (
            '{"action": "open", "symbol": "SOLUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 100, '
            '"stop_loss": 100, "take_profit": 110, "reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "not in allowed list" in result

    def test_open_negative_leverage(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": -1, "position_size_usd": 100, '
            '"stop_loss": 60000, "take_profit": 65000, "reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "leverage" in result.lower()

    def test_close_string_id(self):
        text = '{"action": "close", "position_id": "seven", "reason": "x"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "int position_id" in result

    def test_no_json(self):
        text = "I think we should buy BTC."
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "no JSON" in result

    def test_malformed_json(self):
        text = '{"action": "hold", reason: missing-quotes}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)


# ─── KillSwitch ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> AiTraderStore:
    return AiTraderStore(tmp_path / "test.sqlite")


@pytest.fixture
def ks_config() -> KillSwitchConfig:
    return KillSwitchConfig(
        max_daily_loss_usd=50.0,
        max_total_loss_usd=200.0,
        max_open_positions=3,
        max_leverage=5,
    )


class TestKillSwitch:
    def test_allowed_at_zero_pnl(self, store, ks_config):
        ks = KillSwitch(ks_config, store)
        assert ks.check_can_trade().allowed
        assert ks.check_can_open_position(leverage=3).allowed

    def test_daily_loss_blocks(self, store, ks_config):
        # Симулируем -$60 за сегодня → лимит -$50, должен заблокировать
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.01, entry_price=60000,
            sl_price=58000, tp_price=63000, leverage=3,
            order_link_id="ai_test1", llm_reason="test",
        )
        store.close_position(pos_id, exit_price=54000, realized_pnl_usd=-60.0, close_reason="test")
        ks = KillSwitch(ks_config, store)
        result = ks.check_can_trade()
        assert not result.allowed
        assert "daily loss" in result.reason

    def test_max_positions_blocks(self, store, ks_config):
        for i in range(3):
            store.open_position(
                symbol="BTCUSDT", side="Buy", qty=0.01, entry_price=60000,
                sl_price=58000, tp_price=63000, leverage=3,
                order_link_id=f"ai_test{i}", llm_reason="test",
            )
        ks = KillSwitch(ks_config, store)
        assert ks.check_can_trade().allowed
        result = ks.check_can_open_position(leverage=3)
        assert not result.allowed
        assert "max positions" in result.reason

    def test_max_leverage_blocks(self, store, ks_config):
        ks = KillSwitch(ks_config, store)
        result = ks.check_can_open_position(leverage=10)
        assert not result.allowed
        assert "leverage" in result.reason


# ─── Store ───────────────────────────────────────────────────────────────


class TestStore:
    def test_open_close_pnl_aggregation(self, store):
        pid = store.open_position(
            symbol="ETHUSDT", side="Sell", qty=0.5, entry_price=3000,
            sl_price=3100, tp_price=2800, leverage=3,
            order_link_id="ai_aaa", llm_reason="short setup",
        )
        assert len(store.get_open_positions()) == 1
        store.close_position(pid, exit_price=2900, realized_pnl_usd=50.0, close_reason="tp")
        assert len(store.get_open_positions()) == 0
        assert store.get_today_pnl() == pytest.approx(50.0)
        assert store.get_total_pnl() == pytest.approx(50.0)

    def test_decision_audit_trail(self, store):
        did = store.log_decision(
            cycle=1,
            prompt_system="sys",
            prompt_user="user",
            response_raw='{"action":"hold"}',
            parsed_action={"action": "hold"},
            executed=False,
            error=None,
            tokens_input=100,
            tokens_output=20,
            cost_usd=0.0001,
        )
        assert did > 0


# ─── DeepSeekClient empty-response fallback ──────────────────────────────


class TestDeepSeekClientFallback:
    """Fallback-цепочка при пустых thinking-only ответах."""

    @staticmethod
    def _ensure_anthropic_stub():
        """Заглушка `anthropic` для случая когда SDK не установлен локально."""
        import sys
        import types

        if "anthropic" not in sys.modules:
            stub = types.ModuleType("anthropic")
            stub.Anthropic = lambda **_kw: None  # type: ignore[attr-defined]
            sys.modules["anthropic"] = stub

    @classmethod
    def _make_client(cls, monkeypatch, anthropic_factory):
        cls._ensure_anthropic_stub()
        from ai_trader.llm import client as client_mod

        monkeypatch.setattr(
            client_mod.anthropic, "Anthropic",
            lambda **_kw: anthropic_factory(),
        )
        return client_mod.DeepSeekClient(
            api_key="x", retry_on_empty=1, retry_sleep_sec=0.0,
        )

    @staticmethod
    def _make_msg(*texts: str, in_tokens: int = 10, out_tokens: int = 20):
        from types import SimpleNamespace

        blocks = [SimpleNamespace(type="text", text=t) for t in texts]
        return SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
        )

    def test_first_attempt_text_returns_immediately(self, monkeypatch):
        from types import SimpleNamespace

        calls: list[dict] = []
        msg = self._make_msg("OK")

        class FakeClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                calls.append(kwargs)
                return msg

        client = self._make_client(monkeypatch, FakeClient)
        resp = client.ask("sys", "user")
        assert resp.text == "OK"
        assert resp.error is None
        assert len(calls) == 1
        assert "thinking" in calls[0]   # default thinking_enabled=True

    def test_empty_then_no_thinking_fallback(self, monkeypatch):
        """1) thinking — пусто; 2) retry с thinking — пусто;
        3) fallback БЕЗ thinking — есть текст. Должен вернуть его."""
        from types import SimpleNamespace

        calls: list[dict] = []
        empty_msg = self._make_msg()             # 0 text-блоков
        good_msg = self._make_msg("FALLBACK_OK")
        responses = [empty_msg, empty_msg, good_msg]

        class FakeClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                calls.append(kwargs)
                return responses.pop(0)

        client = self._make_client(monkeypatch, FakeClient)
        resp = client.ask("sys", "user")
        assert resp.text == "FALLBACK_OK"
        assert resp.error is None
        assert len(calls) == 3
        assert "thinking" in calls[0]
        assert "thinking" in calls[1]
        assert "thinking" not in calls[2]   # fallback должен быть без

    def test_all_attempts_empty_returns_error(self, monkeypatch):
        """Если даже no-thinking-fallback пуст — error с правильным текстом."""
        from types import SimpleNamespace

        calls: list[dict] = []
        empty_msg = self._make_msg()

        class FakeClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                calls.append(kwargs)
                return empty_msg

        client = self._make_client(monkeypatch, FakeClient)
        resp = client.ask("sys", "user")
        assert resp.text == ""
        assert resp.error is not None
        assert "empty response" in resp.error
        assert len(calls) == 3   # 2 with thinking + 1 fallback no-thinking


# ─── qty/price rounding under Bybit instrument filters ────────────────


class TestQtyRounding:
    """Округление qty/SL/TP под `lotSizeFilter` / `priceFilter` Bybit."""

    def test_floor_to_step_xrp_integer(self):
        from ai_trader.trading.executor import _floor_to_step

        # XRPUSDT linear: qtyStep=1.0 → 341.0343 → 341
        assert _floor_to_step(341.0343, 1.0) == 341.0
        assert _floor_to_step(0.999, 1.0) == 0.0

    def test_floor_to_step_btc_milli(self):
        from ai_trader.trading.executor import _floor_to_step

        # BTCUSDT linear: qtyStep=0.001 → 0.0049 → 0.004
        assert _floor_to_step(0.0049, 0.001) == 0.004
        assert _floor_to_step(0.00049, 0.001) == 0.0

    def test_round_to_step_price_tick(self):
        from ai_trader.trading.executor import _round_to_step

        # XRPUSDT: tickSize=0.0001
        assert _round_to_step(1.38531, 0.0001) == 1.3853
        # BTCUSDT: tickSize=0.1
        assert _round_to_step(80249.83, 0.1) == 80249.8

    def test_apply_open_xrp_qty_floors_to_integer(self, monkeypatch, tmp_path):
        """Регрессия: XRPUSDT qty=341.0343 ⇒ floor(341), а не отказ Bybit."""
        from types import SimpleNamespace

        from ai_trader.config.settings import AiTraderSettings
        from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
        from ai_trader.state.db import AiTraderStore
        from ai_trader.trading import executor as exec_mod
        from ai_trader.trading.client import InstrumentInfo, Ticker

        captured: dict = {}

        class FakeClient:
            def get_ticker(self, symbol):
                return Ticker(
                    symbol=symbol, last_price=1.4154, bid=1.415, ask=1.4158,
                    funding_rate=0.0, volume_24h=0, price_change_pct_24h=0,
                )

            def get_instrument_info(self, symbol):
                return InstrumentInfo(
                    symbol=symbol, qty_step=1.0,
                    min_order_qty=1.0, max_order_qty=1_000_000.0,
                    tick_size=0.0001,
                )

            def set_leverage(self, symbol, leverage):
                return True

            def place_order(self, **kwargs):
                captured["place_order"] = kwargs
                return {"ok": True, "result": {"orderId": "x"}}

        store = AiTraderStore(tmp_path / "ai.db")
        ks_cfg = KillSwitchConfig(
            max_daily_loss_usd=300, max_total_loss_usd=1000,
            max_open_positions=5, max_leverage=10,
        )
        killswitch = KillSwitch(ks_cfg, store)

        settings = SimpleNamespace(
            trading_enabled=True, virtual_capital_usd=1000.0,
        )

        action = exec_mod.ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "XRPUSDT", "side": "Buy",
                "leverage": 2, "position_size_usd": 482.7,
                "stop_loss": 1.3853, "take_profit": 1.4586,
                "confidence": 0.6,
                "invalidation_condition": "test inv",
                "risk_usd": 9.5,
                "reason": "test",
            },
        )
        result = exec_mod._apply_open(
            action, client=FakeClient(), store=store,
            settings=settings, killswitch=killswitch,
        )
        assert result.executed, f"должно успешно: error={result.error}"
        assert captured["place_order"]["qty"] == 341.0, (
            f"qty должна быть округлена вниз до integer step=1.0, "
            f"получили {captured['place_order']['qty']}"
        )

    def test_apply_open_below_min_qty_rejected(self, monkeypatch, tmp_path):
        """Если notional слишком мал → qty < min → отказ с понятной ошибкой,
        без попытки place_order."""
        from types import SimpleNamespace

        from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
        from ai_trader.state.db import AiTraderStore
        from ai_trader.trading import executor as exec_mod
        from ai_trader.trading.client import InstrumentInfo, Ticker

        place_called = []

        class FakeClient:
            def get_ticker(self, symbol):
                return Ticker(
                    symbol=symbol, last_price=80000.0, bid=79999, ask=80001,
                    funding_rate=0.0, volume_24h=0, price_change_pct_24h=0,
                )

            def get_instrument_info(self, symbol):
                # BTCUSDT: minOrderQty=0.001
                return InstrumentInfo(
                    symbol=symbol, qty_step=0.001,
                    min_order_qty=0.001, max_order_qty=100.0,
                    tick_size=0.1,
                )

            def set_leverage(self, symbol, leverage):
                return True

            def place_order(self, **kwargs):
                place_called.append(kwargs)
                return {"ok": True}

        store = AiTraderStore(tmp_path / "ai.db")
        killswitch = KillSwitch(KillSwitchConfig(
            max_daily_loss_usd=300, max_total_loss_usd=1000,
            max_open_positions=5, max_leverage=10,
        ), store)
        settings = SimpleNamespace(trading_enabled=True, virtual_capital_usd=1000.0)

        action = exec_mod.ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "BTCUSDT", "side": "Buy",
                "leverage": 2, "position_size_usd": 50.0,
                # 50 / 80000 = 0.000625 → floor(0.001) = 0.0 → < min 0.001
                "stop_loss": 78000, "take_profit": 82000,
                "confidence": 0.5,
                "invalidation_condition": "test inv",
                "risk_usd": 1.25,
                "reason": "test",
            },
        )
        result = exec_mod._apply_open(
            action, client=FakeClient(), store=store,
            settings=settings, killswitch=killswitch,
        )
        assert not result.executed
        assert "min_order_qty" in (result.error or "")
        assert place_called == [], "place_order не должен быть вызван"


# ─── get_positions: None при API failure (regression 2026-05-07) ─────


class TestGetPositionsApiFailureMarker:
    """Регрессия: при network/DNS-ошибке Bybit `get_positions` должен
    возвращать ``None`` (а не ``[]``), чтобы вызывающий код мог отличить
    «биржа сказала позиций нет» от «биржа не ответила».

    Инцидент 2026-05-07: на VPS отказал DNS, `get_positions` молча
    возвращал `[]`, reconcile решал что позиция закрылась через SL/TP
    и помечал её closed в БД, хотя на бирже она оставалась открытой.
    """

    @staticmethod
    def _make_client_with_session(session):
        from ai_trader.trading.client import AiBybitClient

        client = AiBybitClient.__new__(AiBybitClient)
        client._session = session
        client._category = "linear"
        client._instr_cache = {}
        return client

    def test_network_exception_returns_none(self):
        class FakeSession:
            def get_positions(self, **_kw):
                raise ConnectionError("DNS resolution failed")

        client = self._make_client_with_session(FakeSession())
        assert client.get_positions(symbol="BTCUSDT") is None

    def test_non_zero_retcode_returns_none(self):
        class FakeSession:
            def get_positions(self, **_kw):
                return {"retCode": 10001, "retMsg": "params error", "result": {"list": []}}

        client = self._make_client_with_session(FakeSession())
        assert client.get_positions(symbol="BTCUSDT") is None

    def test_success_empty_list_returns_empty(self):
        class FakeSession:
            def get_positions(self, **_kw):
                return {"retCode": 0, "result": {"list": []}}

        client = self._make_client_with_session(FakeSession())
        assert client.get_positions(symbol="BTCUSDT") == []

    def test_success_with_positions(self):
        class FakeSession:
            def get_positions(self, **_kw):
                return {
                    "retCode": 0,
                    "result": {"list": [
                        {
                            "symbol": "BTCUSDT", "side": "Buy", "size": "0.006",
                            "avgPrice": "82184.9", "leverage": "1",
                            "unrealisedPnl": "-5.46", "positionValue": "493.0",
                        }
                    ]},
                }

        client = self._make_client_with_session(FakeSession())
        out = client.get_positions(symbol="BTCUSDT")
        assert out is not None
        assert len(out) == 1
        assert out[0].symbol == "BTCUSDT"
        assert out[0].size == 0.006


# ─── _reconcile_closed_positions: regression 2026-05-07 ────────────


class _FakeClientReconcile:
    """In-memory fake клиент для reconcile-тестов."""

    def __init__(self, *, positions_by_symbol=None, ticker_by_symbol=None,
                 positions_returns_none=False, ticker_returns_none=False,
                 closed_pnl_by_symbol=None, closed_pnl_returns_none=False):
        self._positions = positions_by_symbol or {}
        self._tickers = ticker_by_symbol or {}
        self._positions_none = positions_returns_none
        self._ticker_none = ticker_returns_none
        # v0.18: get_closed_pnl mock. Default None → fallback на gross
        # (старые тесты не сломаются и работают как раньше).
        self._closed_pnl = closed_pnl_by_symbol or {}
        self._closed_pnl_none = closed_pnl_returns_none

    def get_positions(self, symbol=None):
        if self._positions_none:
            return None
        return list(self._positions.get(symbol, []))

    def get_ticker(self, symbol):
        if self._ticker_none:
            return None
        return self._tickers.get(symbol)

    def get_closed_pnl(self, symbol, *, start_ms=None, end_ms=None, limit=50):
        if self._closed_pnl_none:
            return None
        return list(self._closed_pnl.get(symbol, []))


def _open_btc_position(store):
    return store.open_position(
        symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
        sl_price=80541.0, tp_price=84651.0, leverage=1,
        order_link_id="ai_test_btc",
        llm_reason="test fixture",
    )


class TestReconcileClosedPositions:
    def test_api_failure_does_not_close_position(self, store):
        """Регрессия 2026-05-07: get_positions=None → позиция остаётся
        open в БД, мы откладываем reconcile до восстановления API."""
        from ai_trader.app.main import _reconcile_closed_positions

        pos_id = _open_btc_position(store)
        client = _FakeClientReconcile(positions_returns_none=True)

        _reconcile_closed_positions(client, store, tg=None)

        opens = store.get_open_positions()
        assert len(opens) == 1, "позиция должна остаться открытой"
        assert opens[0].id == pos_id
        assert opens[0].closed_at is None
        assert opens[0].exit_price is None

    def test_ticker_failure_does_not_close_position(self, store):
        """Если позиции на бирже нет, но ticker не получен — не закрываем
        (без exit_price нельзя посчитать корректный PnL)."""
        from ai_trader.app.main import _reconcile_closed_positions

        pos_id = _open_btc_position(store)
        client = _FakeClientReconcile(
            positions_by_symbol={"BTCUSDT": []},  # биржа: нет позиций
            ticker_returns_none=True,
        )

        _reconcile_closed_positions(client, store, tg=None)

        opens = store.get_open_positions()
        assert len(opens) == 1, "без ticker'а закрывать нельзя"
        assert opens[0].id == pos_id

    def test_position_still_open_no_change(self, store):
        from ai_trader.app.main import _reconcile_closed_positions
        from ai_trader.trading.client import Position

        _open_btc_position(store)
        client = _FakeClientReconcile(
            positions_by_symbol={"BTCUSDT": [Position(
                symbol="BTCUSDT", side="Buy", size=0.006, entry_price=82180,
                leverage=1, unrealised_pnl=-5.46, position_value=493.0,
            )]},
        )

        _reconcile_closed_positions(client, store, tg=None)

        opens = store.get_open_positions()
        assert len(opens) == 1
        assert opens[0].closed_at is None

    def test_position_actually_closed_marks_closed(self, store):
        """Happy path: позиция исчезла, ticker есть → close с правильным PnL."""
        from ai_trader.app.main import _reconcile_closed_positions
        from ai_trader.trading.client import Ticker

        pos_id = _open_btc_position(store)
        client = _FakeClientReconcile(
            positions_by_symbol={"BTCUSDT": []},
            ticker_by_symbol={"BTCUSDT": Ticker(
                symbol="BTCUSDT", last_price=84651.0, bid=84650, ask=84652,
                funding_rate=0.0, volume_24h=0, price_change_pct_24h=0,
            )},
        )

        _reconcile_closed_positions(client, store, tg=None)

        opens = store.get_open_positions()
        assert opens == []
        # PnL = (84651 - 82184.9) * 0.006 ≈ 14.7966
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT exit_price, realized_pnl_usd, close_reason "
                "FROM positions WHERE id=?", (pos_id,),
            ).fetchone()
        assert row["exit_price"] == pytest.approx(84651.0)
        assert row["realized_pnl_usd"] == pytest.approx(14.7966)
        assert "exchange_closed" in row["close_reason"]

    def test_partial_api_failure_isolates_failed_symbol(self, store):
        """Если для одного символа API упал, а для другого ОК — закрытая
        на бирже ETH-позиция должна закрыться, а BTC — остаться открытой."""
        from ai_trader.app.main import _reconcile_closed_positions
        from ai_trader.trading.client import Ticker

        btc_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_btc_partial", llm_reason="t",
        )
        eth_id = store.open_position(
            symbol="ETHUSDT", side="Sell", qty=0.5, entry_price=3000,
            sl_price=3100, tp_price=2800, leverage=3,
            order_link_id="ai_eth_partial", llm_reason="t",
        )

        class PartialFailClient:
            def get_positions(self, symbol=None):
                if symbol == "BTCUSDT":
                    return None  # DNS не разрешил конкретно btc-запрос
                if symbol == "ETHUSDT":
                    return []  # на бирже ETH закрылся
                return []

            def get_ticker(self, symbol):
                if symbol == "ETHUSDT":
                    return Ticker(
                        symbol=symbol, last_price=2800.0, bid=2799, ask=2801,
                        funding_rate=0, volume_24h=0, price_change_pct_24h=0,
                    )
                return None

            def get_closed_pnl(self, symbol, *, start_ms=None, end_ms=None, limit=50):
                # v0.18: closed-pnl недоступен → fallback на gross
                # (это эквивалентно старому поведению до v0.18).
                return None

        _reconcile_closed_positions(PartialFailClient(), store, tg=None)

        opens = store.get_open_positions()
        assert {p.id for p in opens} == {btc_id}, (
            "BTC должен остаться open (API failure), "
            "ETH должен быть closed (биржа закрыла + ticker есть)"
        )
        assert eth_id not in {p.id for p in opens}


# ─── Review-cycle (v0.10, 2026-05-10) ────────────────────────────────────


class TestReviewModeParseAction:
    """parse_action(review_mode=True) должен запрещать action='open',
    но пропускать close/hold (то же что full-cycle).
    """

    def test_open_rejected_in_review_mode(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, "reason": "x"}'
        )
        result = parse_action(text, ALLOWED, review_mode=True)
        assert isinstance(result, str)
        assert "review_mode" in result
        assert "forbidden" in result

    def test_close_allowed_in_review_mode(self):
        text = '{"action": "close", "position_id": 5, "reason": "invalidated"}'
        result = parse_action(text, ALLOWED, review_mode=True)
        assert isinstance(result, ParsedAction)
        assert result.action == "close"
        assert result.raw["position_id"] == 5

    def test_hold_allowed_in_review_mode(self):
        text = '{"action": "hold", "reason": "all setups intact"}'
        result = parse_action(text, ALLOWED, review_mode=True)
        assert isinstance(result, ParsedAction)
        assert result.action == "hold"

    def test_open_allowed_when_review_mode_false(self):
        """Дефолтный режим (full cycle) не должен ломаться."""
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, '
            '"confidence": 0.6, '
            '"invalidation_condition": "BTC closes 1H below 59500", '
            '"risk_usd": 4.0, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)  # review_mode=False по умолчанию
        assert isinstance(result, ParsedAction)
        assert result.action == "open"


class TestBuildSystemPromptReview:
    """Промпт review-цикла должен:
    - подставить review_min и full_min из настроек,
    - явно запрещать 'open',
    - не содержать неразрешённых плейсхолдеров.
    """

    @staticmethod
    def _make_settings(monkeypatch, **env):
        for key in list(__import__("os").environ.keys()):
            if key.startswith(("AI_TRADER_", "DEEPSEEK_")):
                monkeypatch.delenv(key, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from ai_trader.config.settings import AiTraderSettings
        return AiTraderSettings()

    def test_default_review_prompt_intervals(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt_review

        settings = self._make_settings(monkeypatch)
        prompt = build_system_prompt_review(settings)

        # Шаблон содержит переносы строк, поэтому ищем без позиционирования.
        # 900s/60 = 15 min, 300s/60 = 5 min
        assert "15 minutes" in prompt
        assert "5 minutes" in prompt
        assert "5 min later" in prompt

    def test_review_prompt_forbids_open(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt_review

        settings = self._make_settings(monkeypatch)
        prompt = build_system_prompt_review(settings)
        assert "FORBIDDEN" in prompt
        assert "\"open\" is FORBIDDEN" in prompt
        # JSON-схема НЕ должна содержать open-вариант
        assert '"action": "close"' in prompt
        assert '"action": "hold"' in prompt
        # Отсутствует full open-схема (без position_size_usd / leverage)
        assert "position_size_usd" not in prompt

    def test_review_prompt_no_unresolved_placeholders(self, monkeypatch):
        import re

        from ai_trader.llm.prompts import build_system_prompt_review

        settings = self._make_settings(monkeypatch)
        prompt = build_system_prompt_review(settings)
        leftovers = re.findall(r"%\([a-zA-Z_]+\)[a-zA-Z.0-9]+", prompt)
        assert leftovers == [], f"unresolved placeholders: {leftovers}"

    def test_review_prompt_custom_intervals(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt_review

        settings = self._make_settings(
            monkeypatch,
            AI_TRADER_POLL_INTERVAL_SEC="600",   # 10 min
            AI_TRADER_REVIEW_INTERVAL_SEC="120",  # 2 min
        )
        prompt = build_system_prompt_review(settings)
        assert "10 minutes" in prompt
        assert "2 minutes" in prompt
        assert "2 min later" in prompt


class TestFormatContextForReview:
    """Lite-контекст должен содержать только то что нужно для exit-decision:
    open positions, ticker, 1H индикаторы, positioning. БЕЗ macro/news/
    options/4H — review-цикл не для нового анализа.
    """

    def test_empty_positions(self):
        from ai_trader.trading.context import MarketContext, format_context_for_review

        ctx = MarketContext(
            snapshots=[], open_positions=[],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        s = format_context_for_review(ctx)
        assert "OPEN POSITIONS: 0" in s
        assert "(none)" in s
        # Не должно быть секций macro/news/options
        assert "GLOBAL MACRO" not in s
        assert "RECENT CRYPTO NEWS" not in s
        assert "OPTIONS MARKET IV" not in s

    def test_with_open_position(self):
        from ai_trader.state.db import AiPosition
        from ai_trader.trading.client import Ticker
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_review,
        )

        pos = AiPosition(
            id=42, symbol="BTCUSDT", side="Sell", qty=0.01,
            entry_price=60000.0, sl_price=61000.0, tp_price=58000.0,
            leverage=3, order_link_id="ai_test",
            opened_at="2026-05-10T00:00:00+00:00",
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None, llm_reason="test",
        )
        ticker = Ticker(
            symbol="BTCUSDT", last_price=60500.0, bid=60490, ask=60510,
            funding_rate=0.0001, volume_24h=10000, price_change_pct_24h=0.5,
        )
        snap = SymbolSnapshot(
            symbol="BTCUSDT", ticker=ticker, bars_1h=[], bars_4h=[],
        )
        ctx = MarketContext(
            snapshots=[snap], open_positions=[pos],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        s = format_context_for_review(ctx)
        assert "OPEN POSITIONS: 1" in s
        assert "BTCUSDT" in s
        assert "id=42" in s
        assert "lite review cycle" in s.lower()
        # 4H блок не должен появиться
        assert "4H INDICATORS" not in s


class TestCollectReviewContext:
    """Lite-сборщик: должен дёргать API только для символов с open positions
    (не для всех 10 пар) и пропускать 4H/news/macro.
    """

    def test_skips_when_no_open_positions(self, tmp_path):
        from ai_trader.state.db import AiTraderStore
        from ai_trader.trading.context import collect_review_context

        store = AiTraderStore(str(tmp_path / "ai.sqlite"))

        class NoCallsClient:
            def get_ticker(self, *a, **kw):
                raise AssertionError("должен пропустить — нет open positions")

            def get_klines(self, *a, **kw):
                raise AssertionError("должен пропустить — нет open positions")

            def get_wallet_balance(self):
                return 500.0

        ctx = collect_review_context(NoCallsClient(), store, 500.0)
        assert ctx.open_positions == []
        assert ctx.snapshots == []
        assert ctx.real_equity_usd == 500.0

    def test_only_fetches_symbols_with_open_positions(self, tmp_path):
        from ai_trader.state.db import AiTraderStore
        from ai_trader.trading.client import Ticker
        from ai_trader.trading.context import collect_review_context

        store = AiTraderStore(str(tmp_path / "ai.sqlite"))
        # Открываем одну BTC-позицию
        store.open_position(
            symbol="BTCUSDT", side="Sell", qty=0.01, entry_price=60000.0,
            sl_price=61000.0, tp_price=58000.0, leverage=3,
            order_link_id="ai_test", llm_reason="test",
        )

        called_symbols: list[str] = []

        class TrackingClient:
            def get_ticker(self, symbol: str):
                called_symbols.append(symbol)
                return Ticker(
                    symbol=symbol, last_price=60500.0, bid=60490, ask=60510,
                    funding_rate=0.0001, volume_24h=10000, price_change_pct_24h=0.5,
                )

            def get_klines(self, symbol: str, *, interval: str, limit: int):
                # Возвращаем минимум баров (под порог 30) — индикаторы не считаем
                return []

            def get_funding_rate_history(self, *a, **kw):
                return []

            def get_long_short_ratio(self, *a, **kw):
                return []

            def get_wallet_balance(self):
                return 500.0

            def get_positions(self, symbol=None):
                return []

        ctx = collect_review_context(TrackingClient(), store, 500.0)
        # Тикер запрашивался ТОЛЬКО для BTCUSDT — других 9 пар не дёргали
        assert called_symbols == ["BTCUSDT"]
        assert len(ctx.snapshots) == 1
        assert ctx.snapshots[0].symbol == "BTCUSDT"
        # 4H должно быть пустым (review не фетчит 4h)
        assert ctx.snapshots[0].bars_4h == []
        assert ctx.snapshots[0].ind_4h is None


class TestReviewIntervalSettings:
    """Дефолт review_interval_sec = 300 (5 мин). Ноль = review отключён."""

    @staticmethod
    def _make_settings(monkeypatch, **env):
        for key in list(__import__("os").environ.keys()):
            if key.startswith(("AI_TRADER_", "DEEPSEEK_")):
                monkeypatch.delenv(key, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from ai_trader.config.settings import AiTraderSettings
        return AiTraderSettings()

    def test_default_review_interval(self, monkeypatch):
        settings = self._make_settings(monkeypatch)
        assert settings.review_interval_sec == 300

    def test_review_interval_override(self, monkeypatch):
        settings = self._make_settings(monkeypatch, AI_TRADER_REVIEW_INTERVAL_SEC="120")
        assert settings.review_interval_sec == 120

    def test_review_disabled_when_zero(self, monkeypatch):
        settings = self._make_settings(monkeypatch, AI_TRADER_REVIEW_INTERVAL_SEC="0")
        assert settings.review_interval_sec == 0


class TestPeakPnlRStats:
    """v0.11-backport: PEAK-DRAWDOWN — код считает peak_pnl_r из 1H баров
    с момента open. Тесты проверяют корректность Buy/Sell и edge-cases.
    """

    @staticmethod
    def _make_bars(highs_lows: list[tuple[float, float]], start_ms: int = 1_700_000_000_000):
        from ai_trader.trading.client import Bar
        bars = []
        for i, (h, l) in enumerate(highs_lows):
            bars.append(Bar(
                ts=start_ms + i * 3_600_000,
                open=(h + l) / 2, high=h, low=l, close=(h + l) / 2, volume=100.0,
            ))
        return bars

    @staticmethod
    def _make_pos(side: str, entry: float, sl: float, opened_at: str = "2023-11-14T00:00:00+00:00"):
        from ai_trader.state.db import AiPosition
        return AiPosition(
            id=1, symbol="BTCUSDT", side=side, qty=0.01,
            entry_price=entry, sl_price=sl, tp_price=entry + (entry - sl) * 2,
            leverage=1, order_link_id="ai_test",
            opened_at=opened_at,
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None, llm_reason="test",
        )

    def test_buy_peak_from_high(self):
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Buy", entry=100.0, sl=95.0)  # risk_dist=5
        bars = self._make_bars([(102, 99), (105, 101), (103, 100)])  # peak high=105
        peak_r, current_r = _compute_position_r_stats(pos, bars, current_price=101.0)
        assert peak_r == 1.0  # (105 - 100) / 5
        assert current_r == 0.2  # (101 - 100) / 5

    def test_sell_peak_from_low(self):
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Sell", entry=100.0, sl=105.0)  # risk_dist=5
        bars = self._make_bars([(101, 98), (100, 95), (102, 99)])  # min low=95
        peak_r, current_r = _compute_position_r_stats(pos, bars, current_price=99.0)
        assert peak_r == 1.0  # (100 - 95) / 5
        assert current_r == 0.2  # (100 - 99) / 5

    def test_returns_none_when_sl_missing(self):
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Buy", entry=100.0, sl=95.0)
        pos.sl_price = None
        peak_r, current_r = _compute_position_r_stats(pos, [], current_price=101.0)
        assert peak_r is None
        assert current_r is None

    def test_returns_none_when_risk_dist_zero(self):
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Buy", entry=100.0, sl=100.0)
        peak_r, current_r = _compute_position_r_stats(pos, [], current_price=101.0)
        assert peak_r is None
        assert current_r is None

    def test_no_bars_uses_current_only(self):
        """Позиция только что открыта (нет ни одного завершённого 1H бара).
        peak_r должен fallback на current_r."""
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Buy", entry=100.0, sl=95.0)
        peak_r, current_r = _compute_position_r_stats(pos, [], current_price=102.0)
        assert peak_r == 0.4  # falls back to current
        assert current_r == 0.4

    def test_peak_never_below_current(self):
        """Safety-инвариант: peak_r всегда >= current_r."""
        from ai_trader.trading.context import _compute_position_r_stats
        pos = self._make_pos("Buy", entry=100.0, sl=95.0)
        bars = self._make_bars([(101, 99)])  # peak high=101 → 0.2R
        # Текущая цена ушла выше → current_r=0.6 (> peak from bars)
        peak_r, current_r = _compute_position_r_stats(pos, bars, current_price=103.0)
        assert peak_r == 0.6
        assert current_r == 0.6

    def test_bars_before_opened_at_ignored(self):
        """Бары до opened_at не учитываются в peak."""
        from ai_trader.trading.context import _compute_position_r_stats
        # opened_at = 2023-11-14T00:00:00 UTC → ts = 1_699_920_000_000 ms
        pos = self._make_pos("Buy", entry=100.0, sl=95.0,
                              opened_at="2023-11-14T00:00:00+00:00")
        # Первый бар сильно до open (high=150 не должен учитываться),
        # второй сильно после (high=105)
        from ai_trader.trading.client import Bar
        bars = [
            Bar(ts=1_690_000_000_000, open=140, high=150, low=130, close=140, volume=10),
            Bar(ts=1_700_000_000_000, open=104, high=105, low=103, close=104, volume=10),
        ]
        peak_r, current_r = _compute_position_r_stats(pos, bars, current_price=101.0)
        assert peak_r == 1.0  # (105 - 100) / 5, бар на 150 проигнорирован

    def test_format_for_prompt_includes_peak_current(self):
        """format_context_for_prompt выводит строку peak_pnl_r/current_pnl_r
        для каждой открытой позиции."""
        from ai_trader.trading.client import Ticker
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_prompt,
        )
        pos = self._make_pos("Sell", entry=100.0, sl=105.0)
        ticker = Ticker(
            symbol="BTCUSDT", last_price=99.0, bid=98.99, ask=99.01,
            funding_rate=0.0, volume_24h=10000, price_change_pct_24h=0.0,
        )
        bars = self._make_bars([(102, 95)])  # min low=95 → peak_r=1.0
        snap = SymbolSnapshot(symbol="BTCUSDT", ticker=ticker, bars_1h=bars, bars_4h=[])
        ctx = MarketContext(
            snapshots=[snap], open_positions=[pos],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        s = format_context_for_prompt(ctx)
        assert "peak_pnl_r=" in s
        assert "current_pnl_r=" in s
        assert "+1.00R" in s  # peak
        assert "+0.20R" in s  # current

    def test_format_for_review_includes_peak_current(self):
        """format_context_for_review также выводит peak/current per position."""
        from ai_trader.trading.client import Ticker
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_review,
        )
        pos = self._make_pos("Buy", entry=100.0, sl=95.0)
        ticker = Ticker(
            symbol="BTCUSDT", last_price=101.0, bid=100.99, ask=101.01,
            funding_rate=0.0, volume_24h=10000, price_change_pct_24h=0.0,
        )
        bars = self._make_bars([(105, 99)])  # peak high=105 → 1.0R
        snap = SymbolSnapshot(symbol="BTCUSDT", ticker=ticker, bars_1h=bars, bars_4h=[])
        ctx = MarketContext(
            snapshots=[snap], open_positions=[pos],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        s = format_context_for_review(ctx)
        assert "peak_pnl_r=" in s
        assert "current_pnl_r=" in s


class TestLivePositionLineFormatter:
    """v0.17 (2026-05-25, Шаг 2a): _format_live_position_line должен
    корректно отображать live данные биржи рядом с каждой open
    position. Покрывает 4 ветки: normal, API unavailable, not found
    on exchange, buffer % calc для Buy/Sell.

    Цель Шага 2a: дать LLM реальные ``mark_price``/``unrealised_pnl``
    /``liq_price`` от Bybit, а не только наш расчётный
    ``current_pnl_r`` (который считается от ``ticker.last_price``,
    отличается от mark_price на чистом spread/funding skew).
    """

    @staticmethod
    def _make_pos(side: str = "Buy", symbol: str = "BTCUSDT"):
        from ai_trader.state.db import AiPosition
        return AiPosition(
            id=1, symbol=symbol, side=side, qty=0.01,
            entry_price=77000.0, sl_price=76000.0, tp_price=79000.0,
            leverage=10, order_link_id="ai_test",
            opened_at="2026-05-25T08:00:00+00:00",
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None, llm_reason="test",
        )

    @staticmethod
    def _make_live(
        symbol: str = "BTCUSDT", side: str = "Buy", *,
        mark: float = 77200.0, unreal: float = 0.39,
        liq: float = 58400.0, lev: float = 10.0, pos_val: float = 2314.1,
    ):
        from ai_trader.trading.client import Position
        return Position(
            symbol=symbol, side=side, size=0.01, entry_price=77000.0,
            leverage=lev, unrealised_pnl=unreal, position_value=pos_val,
            mark_price=mark, liq_price=liq,
        )

    def test_normal_buy_includes_all_fields(self):
        from ai_trader.trading.context import _format_live_position_line
        pos = self._make_pos("Buy")
        live = self._make_live("BTCUSDT", "Buy", mark=77200.0, unreal=0.39,
                               liq=58400.0, lev=10.0, pos_val=2314.1)
        s = _format_live_position_line(pos, {"BTCUSDT": live})
        assert s is not None
        assert "LIVE:" in s
        assert "mark=$77200" in s
        assert "unrealised=+0.39$" in s
        assert "liq=$58400" in s
        assert "buffer" in s
        assert "margin=$231.41" in s

    def test_normal_sell_buffer_is_directional(self):
        """Для Sell позиции liq > mark, buffer считается от
        ``(liq - mark) / mark``, а не наоборот."""
        from ai_trader.trading.context import _format_live_position_line
        pos = self._make_pos("Sell")
        live = self._make_live("BTCUSDT", "Sell", mark=1.029, unreal=-0.09,
                               liq=1.115, lev=10.0, pos_val=226.4)
        s = _format_live_position_line(pos, {"BTCUSDT": live})
        assert s is not None
        assert "unrealised=-0.09$" in s
        # (1.115-1.029)/1.029*100 ≈ 8.4% → "8% buffer"
        assert "8% buffer" in s

    def test_api_unavailable(self):
        from ai_trader.trading.context import _format_live_position_line
        pos = self._make_pos("Buy")
        s = _format_live_position_line(pos, None)
        assert s is not None
        assert "API unavailable" in s

    def test_not_found_on_exchange(self):
        """БД говорит что позиция открыта, биржа не вернула — reconcile
        pending. LLM должен видеть это явно."""
        from ai_trader.trading.context import _format_live_position_line
        pos = self._make_pos("Buy", symbol="BTCUSDT")
        s = _format_live_position_line(pos, {})  # пустой mapping
        assert s is not None
        assert "not found on exchange" in s

    def test_format_for_prompt_includes_live_line(self):
        """Интеграция: format_context_for_prompt выводит LIVE строку
        в OPEN POSITIONS блоке."""
        from ai_trader.trading.client import Bar, Ticker
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_prompt,
        )
        pos = self._make_pos("Buy")
        live = self._make_live("BTCUSDT", "Buy", mark=77205.4, unreal=2.05,
                               liq=58400.0, lev=10.0, pos_val=2314.1)
        ticker = Ticker(
            symbol="BTCUSDT", last_price=77205.0, bid=77204.0, ask=77206.0,
            funding_rate=0.0, volume_24h=10000, price_change_pct_24h=0.0,
        )
        bars = [Bar(
            ts=1_700_000_000_000, open=77200, high=77210, low=77190,
            close=77205, volume=10,
        )]
        snap = SymbolSnapshot(symbol="BTCUSDT", ticker=ticker,
                              bars_1h=bars, bars_4h=[])
        ctx = MarketContext(
            snapshots=[snap], open_positions=[pos],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
            live_positions={"BTCUSDT": live},
        )
        s = format_context_for_prompt(ctx)
        assert "LIVE: mark=$77205.4" in s
        assert "unrealised=+2.05$" in s
        assert "liq=$58400" in s

    def test_format_for_review_includes_live_line(self):
        """Интеграция: format_context_for_review тоже должен показать
        LIVE строку (review-цикл — главное место где LLM решает
        early-close открытой позиции)."""
        from ai_trader.trading.client import Bar, Ticker
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_review,
        )
        pos = self._make_pos("Buy")
        live = self._make_live()
        ticker = Ticker(
            symbol="BTCUSDT", last_price=77200.0, bid=77199.0, ask=77201.0,
            funding_rate=0.0, volume_24h=10000, price_change_pct_24h=0.0,
        )
        bars = [Bar(
            ts=1_700_000_000_000, open=77100, high=77210, low=77090,
            close=77200, volume=10,
        )]
        snap = SymbolSnapshot(symbol="BTCUSDT", ticker=ticker,
                              bars_1h=bars, bars_4h=[])
        ctx = MarketContext(
            snapshots=[snap], open_positions=[pos],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
            live_positions={"BTCUSDT": live},
        )
        s = format_context_for_review(ctx)
        assert "LIVE: mark=" in s
        assert "unrealised=" in s

    def test_no_live_line_when_no_open_positions(self):
        """Если открытых позиций нет — LIVE строка не появляется
        (просто '(none)')."""
        from ai_trader.trading.context import (
            MarketContext, format_context_for_prompt,
        )
        ctx = MarketContext(
            snapshots=[], open_positions=[],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
            live_positions=None,
        )
        s = format_context_for_prompt(ctx)
        assert "LIVE:" not in s
        assert "(none)" in s


class TestPeakDrawdownTriggerInPrompts:
    """Промпты должны явно содержать описание триггера PEAK-DRAWDOWN.

    v0.12: после bug-fix prompts clean-up PEAK-DRAWDOWN стал trigger 4
    (был 5), MACRO REGIME SHIFT (F&G) удалён целиком — F&G отсутствует
    в контексте.
    """

    def test_full_system_prompt_has_peak_drawdown(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "PEAK-DRAWDOWN" in SYSTEM_PROMPT
        assert "peak_pnl_r" in SYSTEM_PROMPT
        assert "current_pnl_r" in SYSTEM_PROMPT
        assert "0.8R" in SYSTEM_PROMPT
        assert "0.45R" in SYSTEM_PROMPT
        assert "1/2/3/4" in SYSTEM_PROMPT

    def test_review_system_prompt_has_peak_drawdown(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT_REVIEW
        assert "PEAK-DRAWDOWN" in SYSTEM_PROMPT_REVIEW
        assert "peak_pnl_r" in SYSTEM_PROMPT_REVIEW
        assert "0.8R" in SYSTEM_PROMPT_REVIEW
        assert "0.45R" in SYSTEM_PROMPT_REVIEW


class TestDropIncompleteBar:
    """v0.12 bug-fix: `_drop_incomplete_bar` отбрасывает партиальный 1H/4H бар
    перед compute_snapshot. Bybit get_klines возвращает все бары включая
    текущий незакрытый — caнonical RSI/MACD/BB определены на closed candles.
    """

    def _bar(self, ts_ms: int, close: float = 100.0):
        from ai_trader.trading.client import Bar
        return Bar(ts=ts_ms, open=close, high=close, low=close, close=close, volume=1.0)

    def test_empty_list_returns_empty(self):
        from ai_trader.trading.context import _drop_incomplete_bar
        assert _drop_incomplete_bar([], 60) == []

    def test_last_bar_in_future_window_is_dropped(self):
        """Бар начался в текущем интервале и ещё не закрыт → отбрасываем."""
        from ai_trader.trading.context import _drop_incomplete_bar
        import time
        now_ms = int(time.time() * 1000)
        # Бар начался 30 минут назад, interval=60 → закроется через 30 минут
        partial = self._bar(now_ms - 30 * 60 * 1000)
        closed = self._bar(now_ms - 90 * 60 * 1000)
        out = _drop_incomplete_bar([closed, partial], 60)
        assert out == [closed]

    def test_last_bar_already_closed_is_kept(self):
        """Бар начался >interval назад → уже закрыт, оставляем."""
        from ai_trader.trading.context import _drop_incomplete_bar
        import time
        now_ms = int(time.time() * 1000)
        # Бар начался 70 минут назад, interval=60 → закрылся 10 минут назад
        closed = self._bar(now_ms - 70 * 60 * 1000)
        out = _drop_incomplete_bar([closed], 60)
        assert out == [closed]

    def test_4h_interval_partial_bar_dropped(self):
        """Аналогично для 4H (interval=240) баров."""
        from ai_trader.trading.context import _drop_incomplete_bar
        import time
        now_ms = int(time.time() * 1000)
        # Бар начался 2 часа назад, 4H бар → ещё 2 часа до закрытия
        partial = self._bar(now_ms - 120 * 60 * 1000)
        out = _drop_incomplete_bar([partial], 240)
        assert out == []

    def test_only_last_bar_checked(self):
        """Промежуточные бары всегда закрыты — мы не их трогаем, только tail."""
        from ai_trader.trading.context import _drop_incomplete_bar
        import time
        now_ms = int(time.time() * 1000)
        bars = [
            self._bar(now_ms - (k + 1) * 60 * 60 * 1000)
            for k in reversed(range(3))
        ]
        # Добавляем partial в конец
        partial = self._bar(now_ms - 5 * 60 * 1000)
        bars.append(partial)
        out = _drop_incomplete_bar(bars, 60)
        assert len(out) == 3
        assert out[-1].ts == bars[-2].ts


class TestPromptsCleanupNoMissingSignals:
    """v0.12 bug-fix: в промптах НЕ должно остаться упоминаний сигналов,
    которые отсутствуют в v0.3-контексте (VWAP / F&G / L-S / OI / liquidation /
    DVOL). Раньше LLM получал инструкции использовать эти данные и
    галлюцинировал значения.
    """

    FORBIDDEN_FRAGMENTS = (
        "VWAP", "vwap",
        "Fear & Greed", "F&G",
        "DVOL",
        "liquidation cascade", "Liquidation cascade",
        "OI extreme",
        "OI delta",
        "retail L/S",
        "Long/Short ratio",
        "buy_ratio",
        "RV ",  # realized volatility
    )

    def test_full_system_prompt_no_missing_signals(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        for frag in self.FORBIDDEN_FRAGMENTS:
            assert frag not in SYSTEM_PROMPT, (
                f"SYSTEM_PROMPT still mentions {frag!r}, but this data is not "
                "in v0.3 context — LLM may hallucinate."
            )

    def test_review_system_prompt_no_missing_signals(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT_REVIEW
        for frag in self.FORBIDDEN_FRAGMENTS:
            assert frag not in SYSTEM_PROMPT_REVIEW, (
                f"SYSTEM_PROMPT_REVIEW still mentions {frag!r}, but this data "
                "is not in v0.3 context — LLM may hallucinate."
            )

    def test_full_prompt_trigger_1_uses_bb_middle(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "BB middle" in SYSTEM_PROMPT
        # MACRO REGIME SHIFT (бывший trigger 4) удалён целиком
        assert "MACRO REGIME SHIFT" not in SYSTEM_PROMPT


class TestRsiExtremeThreshold:
    """v0.12 bug-fix: «RSI extreme» теперь имеет числовое определение —
    ≤25 / ≥75. Это согласуется с 2026 best-practice (Tapbit, Apptrading)
    для bear/bull capitulation в crypto-perp на 1H, и предотвращает
    случай XRPUSDT id=58 где LLM трактовал RSI=32.8 как «oversold» и
    зашёл counter-trend против явного 1H downtrend.
    """

    def _snap(self, rsi_val: float):
        from ai_trader.analysis.indicators import IndicatorSnapshot
        return IndicatorSnapshot(
            last_close=100.0, rsi14=rsi_val,
            macd_line=0.0, macd_signal=0.0, macd_hist=0.0,
            atr14=1.0, atr14_pct=1.0,
            ema20=100.0, ema50=100.0,
            bb_upper=102.0, bb_middle=100.0, bb_lower=98.0,
            bb_position=0.5,
        )

    def test_rsi_24_labelled_extreme_oversold(self):
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(24.0))
        assert "[EXTREME OVERSOLD]" in s
        assert "[OVERSOLD]" not in s.replace("[EXTREME OVERSOLD]", "")

    def test_rsi_28_labelled_oversold_but_not_extreme(self):
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(28.0))
        assert "[OVERSOLD]" in s
        assert "EXTREME" not in s

    def test_rsi_32_8_no_oversold_label(self):
        """Прямой regression для XRPUSDT id=58: RSI=32.8 не должен
        получать [OVERSOLD] лейбл."""
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(32.8))
        assert "OVERSOLD" not in s
        assert "EXTREME" not in s

    def test_rsi_72_labelled_overbought_but_not_extreme(self):
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(72.0))
        assert "[OVERBOUGHT]" in s
        assert "EXTREME" not in s

    def test_rsi_76_labelled_extreme_overbought(self):
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(76.0))
        assert "[EXTREME OVERBOUGHT]" in s

    def test_rsi_50_no_label(self):
        from ai_trader.analysis.indicators import format_snapshot
        s = format_snapshot(self._snap(50.0))
        assert "OVERSOLD" not in s
        assert "OVERBOUGHT" not in s

    def test_full_prompt_counter_trend_rule_uses_25_and_75(self):
        """Промпт должен явно требовать RSI ≤ 25 / ≥ 75 для counter-trend,
        а не размытое «RSI extreme»."""
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "RSI <= 25" in SYSTEM_PROMPT
        assert "RSI >= 75" in SYSTEM_PROMPT
        assert "[EXTREME OVERSOLD]" in SYSTEM_PROMPT
        assert "[EXTREME OVERBOUGHT]" in SYSTEM_PROMPT


# ─── v0.13 (2026-05-18): Nof1-style meta-cognition fields ────────────────
#
# parse_action для action="open" должен ТРЕБОВАТЬ:
#   - confidence: number 0.0-1.0
#   - invalidation_condition: non-empty string ≤ 500 chars
#   - risk_usd: number 0 < x ≤ 10
#
# Также SYSTEM_PROMPT должен содержать секции guidance, чтобы LLM знал
# зачем эти поля и как их грамотно заполнить.


def _open_json(**overrides) -> str:
    """Helper: minimal valid open-JSON со всеми required полями v0.13.
    overrides позволяют выкинуть/переопределить любое поле для reject-тестов.
    """
    base = {
        "action": "open",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "leverage": 3,
        "position_size_usd": 200,
        "stop_loss": 60000,
        "take_profit": 65000,
        "confidence": 0.65,
        "invalidation_condition": "1H closes below 59500 (EMA50 lost)",
        "risk_usd": 6.5,
        "reason": "breakout",
    }
    for k, v in overrides.items():
        if v is _MISSING:
            base.pop(k, None)
        else:
            base[k] = v
    import json as _json
    return _json.dumps(base)


_MISSING = object()


class TestOpenSchemaV13Required:
    """Все три новых поля обязательны при action=open."""

    def test_open_full_valid(self):
        result = parse_action(_open_json(), ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.raw["confidence"] == 0.65
        assert result.raw["invalidation_condition"].startswith("1H closes")
        assert result.raw["risk_usd"] == 6.5

    def test_open_missing_confidence_rejected(self):
        result = parse_action(_open_json(confidence=_MISSING), ALLOWED)
        assert isinstance(result, str)
        assert "confidence required" in result

    def test_open_missing_invalidation_rejected(self):
        result = parse_action(_open_json(invalidation_condition=_MISSING), ALLOWED)
        assert isinstance(result, str)
        assert "invalidation_condition required" in result

    def test_open_missing_risk_usd_rejected(self):
        result = parse_action(_open_json(risk_usd=_MISSING), ALLOWED)
        assert isinstance(result, str)
        assert "risk_usd required" in result

    def test_open_confidence_negative_rejected(self):
        result = parse_action(_open_json(confidence=-0.1), ALLOWED)
        assert isinstance(result, str)
        assert "confidence out of range" in result

    def test_open_confidence_above_one_rejected(self):
        result = parse_action(_open_json(confidence=1.5), ALLOWED)
        assert isinstance(result, str)
        assert "confidence out of range" in result

    def test_open_confidence_string_rejected(self):
        result = parse_action(_open_json(confidence="high"), ALLOWED)
        assert isinstance(result, str)
        assert "confidence required" in result

    def test_open_confidence_bool_rejected(self):
        # Bool — подтип int в Python, нужно явно отбить
        result = parse_action(_open_json(confidence=True), ALLOWED)
        assert isinstance(result, str)
        assert "confidence required" in result

    def test_open_confidence_zero_allowed(self):
        # 0.0 — допустимый low-bound
        result = parse_action(_open_json(confidence=0.0), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_open_confidence_one_allowed(self):
        result = parse_action(_open_json(confidence=1.0), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_open_invalidation_empty_string_rejected(self):
        result = parse_action(_open_json(invalidation_condition="   "), ALLOWED)
        assert isinstance(result, str)
        assert "non-empty" in result

    def test_open_invalidation_too_long_rejected(self):
        long_str = "x" * 501
        result = parse_action(_open_json(invalidation_condition=long_str), ALLOWED)
        assert isinstance(result, str)
        assert "too long" in result

    def test_open_invalidation_non_string_rejected(self):
        result = parse_action(_open_json(invalidation_condition=123), ALLOWED)
        assert isinstance(result, str)
        assert "invalidation_condition required" in result

    def test_open_risk_usd_zero_rejected(self):
        result = parse_action(_open_json(risk_usd=0.0), ALLOWED)
        assert isinstance(result, str)
        assert "risk_usd out of range" in result

    def test_open_risk_usd_negative_rejected(self):
        result = parse_action(_open_json(risk_usd=-5.0), ALLOWED)
        assert isinstance(result, str)
        assert "risk_usd out of range" in result

    def test_open_risk_usd_above_cap_rejected(self):
        # Cap = $10 (2% of $500)
        result = parse_action(_open_json(risk_usd=15.0), ALLOWED)
        assert isinstance(result, str)
        assert "risk_usd out of range" in result

    def test_open_risk_usd_at_cap_allowed(self):
        result = parse_action(_open_json(risk_usd=10.0), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_open_risk_usd_string_rejected(self):
        result = parse_action(_open_json(risk_usd="five"), ALLOWED)
        assert isinstance(result, str)
        assert "risk_usd required" in result

    def test_close_does_not_require_new_fields(self):
        """Action="close" не требует новых полей — только position_id."""
        text = '{"action": "close", "position_id": 42, "reason": "invalidated"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "close"

    def test_hold_does_not_require_new_fields(self):
        text = '{"action": "hold", "reason": "no setup"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "hold"


class TestOpenSchemaV13PromptGuidance:
    """SYSTEM_PROMPT должен содержать секции с инструкциями по новым полям,
    чтобы LLM знал зачем они и как их корректно заполнить.
    """

    def test_prompt_mentions_confidence_field(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "\"confidence\"" in SYSTEM_PROMPT
        assert "0.00-1.00" in SYSTEM_PROMPT or "0.0, 1.0" in SYSTEM_PROMPT

    def test_prompt_mentions_invalidation_condition_field(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "\"invalidation_condition\"" in SYSTEM_PROMPT

    def test_prompt_mentions_risk_usd_field(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "\"risk_usd\"" in SYSTEM_PROMPT

    def test_prompt_has_confidence_calibration_section(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "CONFIDENCE CALIBRATION" in SYSTEM_PROMPT
        # Бэнды должны быть явно перечислены
        assert "0.30-0.49" in SYSTEM_PROMPT
        assert "0.50-0.69" in SYSTEM_PROMPT
        assert "0.70-1.00" in SYSTEM_PROMPT

    def test_prompt_has_pre_registered_invalidation_section(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "PRE-REGISTERED INVALIDATION" in SYSTEM_PROMPT

    def test_prompt_has_common_pitfalls_section(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "COMMON PITFALLS" in SYSTEM_PROMPT
        # Канонический список из Nof1 (TechPost1 § Risk Management Protocol)
        for token in (
            "OVERTRADING",
            "REVENGE TRADING",
            "ANALYSIS PARALYSIS",
            "IGNORING CORRELATION",
            "OVERLEVERAGING",
        ):
            assert token in SYSTEM_PROMPT, f"missing pitfall token: {token}"

    def test_prompt_marks_three_fields_as_mandatory(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        # В CRITICAL CONSTRAINTS должно быть прямое указание на required
        assert "MANDATORY" in SYSTEM_PROMPT
        # И в schema-блоке тоже
        assert "must be 0 < x <= 10" in SYSTEM_PROMPT

    def test_prompt_has_pre_commit_check_step(self):
        """ANALYSIS APPROACH должен включать PRE-COMMIT CHECK для open."""
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "PRE-COMMIT CHECK" in SYSTEM_PROMPT


class TestSystemPromptDynamicWhitelist:
    """v0.14 (2026-05-20): SYSTEM_PROMPT теперь шаблон с placeholder
    ``__ALLOWED_PAIRS__`` который рендерится через build_system_prompt(settings)
    из settings.symbols. Single source of truth — .env (AI_TRADER_SYMBOLS).
    """

    def _settings(self, symbols_csv: str):
        from ai_trader.config.settings import AiTraderSettings

        return AiTraderSettings(_env_file=None, AI_TRADER_SYMBOLS=symbols_csv)

    def test_default_render_lists_default_pairs(self):
        from ai_trader.llm.prompts import SYSTEM_PROMPT
        # Default render использует DEFAULT_AI_SYMBOLS
        assert "BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT." in SYSTEM_PROMPT

    def test_build_system_prompt_uses_settings_symbols(self):
        from ai_trader.llm.prompts import build_system_prompt

        s = self._settings("LTCUSDT,ATOMUSDT,BTCUSDT,SUIUSDT,LINKUSDT")
        rendered = build_system_prompt(s)
        assert "LTCUSDT, ATOMUSDT, BTCUSDT, SUIUSDT, LINKUSDT." in rendered
        # дефолтные пары не должны попасть в рендер
        assert "ETHUSDT" not in rendered
        assert "DOGEUSDT" not in rendered

    def test_build_system_prompt_no_placeholder_remains(self):
        """Placeholder должен полностью исчезнуть из рендера."""
        from ai_trader.llm.prompts import build_system_prompt

        s = self._settings("BTCUSDT,ETHUSDT")
        rendered = build_system_prompt(s)
        assert "__ALLOWED_PAIRS__" not in rendered

    def test_build_system_prompt_single_symbol(self):
        from ai_trader.llm.prompts import build_system_prompt

        s = self._settings("BTCUSDT")
        rendered = build_system_prompt(s)
        assert "BTCUSDT." in rendered
        # без trailing comma
        assert "BTCUSDT," not in rendered.split("ALLOWED PAIRS")[1].split("\n")[1]

    def test_template_still_has_other_required_sections(self):
        """Refactor не должен сломать остальные секции промпта."""
        from ai_trader.llm.prompts import build_system_prompt

        s = self._settings("LTCUSDT,BTCUSDT")
        rendered = build_system_prompt(s)
        # Все ключевые секции v0.13 на месте
        for section in (
            "CONFIDENCE CALIBRATION",
            "PRE-REGISTERED INVALIDATION",
            "COMMON PITFALLS",
            "PEAK-DRAWDOWN",
            "EXIT MANAGEMENT",
            "ANALYSIS APPROACH",
            "DECISION FORMAT",
        ):
            assert section in rendered, f"missing section: {section}"


class TestOpenSchemaV13DBRoundTrip:
    """БД должна корректно хранить и возвращать новые поля.

    Также проверяем что миграция идемпотентна — если БД уже создана
    (как на VPS) и колонки добавлены, повторный init не падает.
    """

    def test_open_position_persists_v13_fields(self, store):
        pid = store.open_position(
            symbol="BTCUSDT",
            side="Buy",
            qty=0.005,
            entry_price=80000.0,
            sl_price=78000.0,
            tp_price=84000.0,
            leverage=3,
            order_link_id="ai_v13_rt1",
            llm_reason="rt test",
            confidence=0.7,
            invalidation_condition="1H closes below 79000",
            risk_usd_declared=10.0,
        )
        assert pid > 0
        opens = store.get_open_positions()
        match = [p for p in opens if p.id == pid]
        assert len(match) == 1
        p = match[0]
        assert p.confidence == pytest.approx(0.7)
        assert p.invalidation_condition == "1H closes below 79000"
        assert p.risk_usd_declared == pytest.approx(10.0)

    def test_open_position_legacy_call_without_v13_fields_works(self, store):
        """Backward-compat: старый код вызывающий open_position без новых
        kwargs должен работать (поля nullable, default=None)."""
        pid = store.open_position(
            symbol="ETHUSDT",
            side="Sell",
            qty=0.5,
            entry_price=3000.0,
            sl_price=3100.0,
            tp_price=2800.0,
            leverage=3,
            order_link_id="ai_v13_legacy",
            llm_reason="legacy",
        )
        opens = store.get_open_positions()
        match = [p for p in opens if p.id == pid]
        assert len(match) == 1
        p = match[0]
        assert p.confidence is None
        assert p.invalidation_condition is None
        assert p.risk_usd_declared is None

    def test_migration_idempotent(self, tmp_path):
        """Повторный init на той же БД не должен падать — миграция
        проверяет PRAGMA table_info и пропускает существующие колонки."""
        from ai_trader.state.db import AiTraderStore

        db_path = tmp_path / "v13_idem.sqlite"
        store1 = AiTraderStore(db_path)
        store1.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.001, entry_price=80000,
            sl_price=78000, tp_price=84000, leverage=3,
            order_link_id="ai_idem_1", llm_reason="t",
            confidence=0.5, invalidation_condition="x", risk_usd_declared=2.0,
        )
        # Второй init на тот же файл — не должен падать на ALTER TABLE
        store2 = AiTraderStore(db_path)
        opens = store2.get_open_positions()
        assert len(opens) == 1
        assert opens[0].confidence == pytest.approx(0.5)

    def test_migration_old_db_without_columns_is_upgraded(self, tmp_path):
        """Симулируем старую БД (без новых колонок), затем открываем
        через текущий AiTraderStore — миграция должна ALTER TABLE и
        получившаяся БД должна корректно работать с новыми полями."""
        import sqlite3
        from ai_trader.state.db import AiTraderStore

        db_path = tmp_path / "v13_legacy.sqlite"
        # Создаём «старую» схему — без confidence/invalidation_condition/risk_usd_declared
        legacy_schema = """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL,
            tp_price REAL,
            leverage INTEGER NOT NULL,
            order_link_id TEXT NOT NULL UNIQUE,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            exit_price REAL,
            realized_pnl_usd REAL,
            close_reason TEXT,
            llm_reason TEXT NOT NULL
        );
        """
        with sqlite3.connect(db_path) as conn:
            conn.executescript(legacy_schema)
            conn.execute(
                """
                INSERT INTO positions (symbol, side, qty, entry_price, sl_price,
                tp_price, leverage, order_link_id, opened_at, llm_reason)
                VALUES ('BTCUSDT', 'Buy', 0.001, 80000.0, 78000.0, 84000.0, 3,
                'ai_legacy_pre_v13', '2026-05-17T00:00:00+00:00', 'pre v13')
                """
            )
            conn.commit()
        # Открываем через текущий код — должен мигрировать
        store = AiTraderStore(db_path)
        opens = store.get_open_positions()
        assert len(opens) == 1
        legacy_pos = opens[0]
        # Старая позиция: новые поля = None (миграция не заполняет данные)
        assert legacy_pos.confidence is None
        assert legacy_pos.invalidation_condition is None
        assert legacy_pos.risk_usd_declared is None
        # И можно вставить НОВУЮ позицию с новыми полями
        new_id = store.open_position(
            symbol="ETHUSDT", side="Sell", qty=0.5, entry_price=3000,
            sl_price=3100, tp_price=2800, leverage=2,
            order_link_id="ai_post_v13", llm_reason="new",
            confidence=0.8, invalidation_condition="ETH 1H above 3050",
            risk_usd_declared=5.0,
        )
        new_pos = next(p for p in store.get_open_positions() if p.id == new_id)
        assert new_pos.confidence == pytest.approx(0.8)
        assert new_pos.invalidation_condition == "ETH 1H above 3050"


# ─── v0.15 (2026-05-24, refactor): template-driven capital rules ──────────
#
# Все денежные значения промпта (capital, risk_pct, risk_usd_cap,
# daily_loss) выводятся из ``settings`` через placeholder'ы. Single source
# of truth — settings.py / .env. Поведение при default settings
# поведенчески ЭКВИВАЛЕНТНО прежнему хардкоду ($500/2%/$10/$50).
class TestSystemPromptCapitalRulesTemplate:
    """v0.15 refactor: capital rules через placeholder'ы из settings."""

    def test_default_render_byte_identical_to_pre_refactor(self):
        """Главная инвариант-проверка: при default settings промпт
        ДОЛЖЕН быть байт-в-байт тот же, что был до refactor'а v0.15.

        Если этот тест падает → refactor сломал поведение ИИ.
        SHA256 baseline: ac8f4a19b80879a1ad150955840343031b28aced10a0a654cf6cc1d06642942a
        """
        import hashlib

        from ai_trader.config.settings import AiTraderSettings
        from ai_trader.llm.prompts import SYSTEM_PROMPT, build_system_prompt

        expected_sha256 = (
            "ac8f4a19b80879a1ad150955840343031b28aced10a0a654cf6cc1d06642942a"
        )
        # 1) Module-level SYSTEM_PROMPT (default render с DEFAULT_AI_SYMBOLS).
        actual_sha = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
        assert actual_sha == expected_sha256, (
            f"SYSTEM_PROMPT changed! Expected sha256={expected_sha256}, "
            f"got {actual_sha}. Refactor v0.15 should be a no-op for "
            "default settings (capital=$500, risk=2%, daily=$50)."
        )
        # 2) build_system_prompt(default_settings) — должен дать тот же текст.
        rendered = build_system_prompt(AiTraderSettings())
        assert rendered == SYSTEM_PROMPT
        assert hashlib.sha256(rendered.encode()).hexdigest() == expected_sha256

    def test_no_hardcoded_dollar_values_in_template(self):
        """В _SYSTEM_PROMPT_TEMPLATE НЕ должно быть хардкоженных
        ``$500``/``$10``/``$50``/``2%``. Все через placeholder'ы.
        """
        from ai_trader.llm.prompts import _SYSTEM_PROMPT_TEMPLATE

        forbidden = ["$500", "$10 ", "$10.", "$10,", "$50 ", "2% of"]
        for token in forbidden:
            assert token not in _SYSTEM_PROMPT_TEMPLATE, (
                f"Hardcoded {token!r} found in template — should use "
                "placeholder __VIRTUAL_CAPITAL__/__RISK_USD_CAP__/etc."
            )

    def test_template_has_all_four_placeholders(self):
        """Template должен содержать все 4 placeholder'а (иначе render
        выдаст текст с literal '__FOO__' который собьёт LLM)."""
        from ai_trader.llm.prompts import _SYSTEM_PROMPT_TEMPLATE

        for placeholder in (
            "__VIRTUAL_CAPITAL__",
            "__RISK_PCT__",
            "__RISK_USD_CAP__",
            "__DAILY_LOSS_LIMIT__",
        ):
            assert placeholder in _SYSTEM_PROMPT_TEMPLATE, (
                f"Placeholder {placeholder} missing from template."
            )

    def test_render_with_custom_settings_produces_correct_numbers(self):
        """При смене settings (имитация ``.env``) промпт автоматически
        отражает новые значения — без правок prompts.py.

        Используем ``model_copy(update=...)`` — kwargs по python-имени
        блокируются ``validation_alias`` (pydantic читает только из env
        с alias ``AI_TRADER_*``).
        """
        from ai_trader.config.settings import AiTraderSettings
        from ai_trader.llm.prompts import build_system_prompt

        s = AiTraderSettings().model_copy(
            update={
                "virtual_capital_usd": 1000.0,
                "risk_per_trade_pct": 0.05,
                "max_daily_loss_usd": 100.0,
            }
        )
        rendered = build_system_prompt(s)
        # 1000 * 0.05 = 50.0 → cap $50
        assert "Virtual capital: $1000 USD" in rendered
        assert "5% of capital ($50 max risk per trade)" in rendered
        assert "Daily loss limit: $100" in rendered
        assert "risk_usd ∈ (0, 50]" in rendered
        assert "(0, 50]" in rendered  # multiple usages
        # Старые значения НЕ должны всплыть
        assert "$500 USD" not in rendered
        assert "2% of capital" not in rendered
        assert "$10 max" not in rendered

    def test_render_with_default_settings_no_placeholders_left(self):
        """После render'а в строке не должно остаться '__FOO__' patterns."""
        import re

        from ai_trader.config.settings import AiTraderSettings
        from ai_trader.llm.prompts import build_system_prompt

        rendered = build_system_prompt(AiTraderSettings())
        leftover = re.findall(r"__[A-Z_]+__", rendered)
        assert leftover == [], f"Unrendered placeholders: {leftover}"


class TestParseActionRiskUsdCapFromSettings:
    """v0.15: parse_action принимает risk_usd_cap явно (single source = settings)."""

    def _open_json(self, risk_usd: float) -> str:
        return (
            '{"action":"open","symbol":"BTCUSDT","side":"Buy","leverage":2,'
            '"position_size_usd":100,"stop_loss":95000,"take_profit":110000,'
            '"confidence":0.6,"invalidation_condition":"BTC 1H below 95k",'
            f'"risk_usd":{risk_usd},"reason":"test"}}'
        )

    def test_default_cap_is_10(self):
        # Default cap = 10.0 (соответствует $500 × 2% = $10).
        result = parse_action(self._open_json(15.0), ALLOWED)
        assert isinstance(result, str)
        assert "must be 0 < x <= 10" in result

    def test_custom_cap_50_allows_45(self):
        # При 10% риска и $500 капитала cap = $50.
        result = parse_action(self._open_json(45.0), ALLOWED, risk_usd_cap=50.0)
        assert isinstance(result, ParsedAction)

    def test_custom_cap_50_rejects_55(self):
        result = parse_action(self._open_json(55.0), ALLOWED, risk_usd_cap=50.0)
        assert isinstance(result, str)
        assert "must be 0 < x <= 50" in result
        assert "Per-trade cap = $50" in result

    def test_custom_cap_message_uses_actual_value(self):
        # Текст ошибки должен динамически отражать переданный cap.
        result = parse_action(self._open_json(100.0), ALLOWED, risk_usd_cap=25.0)
        assert isinstance(result, str)
        assert "0 < x <= 25" in result
        assert "$25" in result


# ─── v0.18 (2026-05-25): Net PnL reconciliation ────────────────────────────


def _make_closed_pnl(
    symbol: str = "BTCUSDT",
    *,
    side: str = "Sell",  # invert от position.side ("Buy")
    order_link_id: str = "ai_test_btc",
    closed_size: float = 0.006,
    avg_entry: float = 82184.9,
    avg_exit: float = 84651.0,
    closed_pnl: float = 14.20,  # gross 14.7966 минус ~0.59 fee
    created_ms: int = 1700000000000,
    updated_ms: int = 1700000000001,
):
    from ai_trader.trading.client import ClosedPnl
    return ClosedPnl(
        symbol=symbol, side=side, order_link_id=order_link_id,
        closed_size=closed_size, avg_entry_price=avg_entry,
        avg_exit_price=avg_exit, closed_pnl=closed_pnl,
        created_time_ms=created_ms, updated_time_ms=updated_ms,
    )


class TestFetchNetPnl:
    """v0.18: helper fetch_net_pnl должен корректно матчить запись
    Bybit closed-pnl с нашей AiPosition в БД.
    """

    @staticmethod
    def _make_pos(side: str = "Buy", link: str = "ai_test_btc"):
        from ai_trader.state.db import AiPosition
        return AiPosition(
            id=42, symbol="BTCUSDT", side=side, qty=0.006,
            entry_price=82184.9, sl_price=80541.0, tp_price=84651.0,
            leverage=1, order_link_id=link,
            opened_at="2023-11-14T22:13:20+00:00",
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None, llm_reason="t",
        )

    def test_match_by_order_link_id(self):
        """Самый надёжный путь: bybit_closed_pnl.orderLinkId == position.order_link_id."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [_make_closed_pnl(closed_pnl=14.20)],
        })
        pos = self._make_pos()
        result = fetch_net_pnl(client, pos)
        assert result is not None
        net_pnl, exit_price = result
        assert net_pnl == pytest.approx(14.20)
        assert exit_price == pytest.approx(84651.0)

    def test_match_fallback_by_size_and_side(self):
        """Если orderLinkId не совпал (другой trade) — fallback по
        closedSize + invert side + createdTime."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [_make_closed_pnl(
                order_link_id="some_other_link",
                closed_size=0.006, side="Sell",  # invert от Buy
                closed_pnl=13.95,
                created_ms=1700000000000 + 60_000,  # > opened_at
            )],
        })
        pos = self._make_pos()
        result = fetch_net_pnl(client, pos)
        assert result is not None
        assert result[0] == pytest.approx(13.95)

    def test_no_match_when_size_mismatch(self):
        """closedSize 0.012 != qty 0.006 → не матчим, возвращаем None."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [_make_closed_pnl(
                order_link_id="other", closed_size=0.012,
            )],
        })
        result = fetch_net_pnl(client, self._make_pos())
        assert result is None

    def test_api_failure_returns_none(self):
        """get_closed_pnl=None → caller должен оставить gross."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_returns_none=True)
        assert fetch_net_pnl(client, self._make_pos()) is None

    def test_empty_list_returns_none(self):
        """Bybit ещё не успел записать closed-pnl → пусто → None
        (caller fallback на gross, дойдёт через _reconcile_pnl_to_net)."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_by_symbol={"BTCUSDT": []})
        assert fetch_net_pnl(client, self._make_pos()) is None

    def test_picks_latest_updated_when_multiple_match(self):
        """Несколько кандидатов с одним link_id → берём с max updatedTime
        (финальная запись после всех partial-fills)."""
        from ai_trader.trading.pnl_reconcile import fetch_net_pnl
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [
                _make_closed_pnl(closed_pnl=10.0, updated_ms=1700000000001),
                _make_closed_pnl(closed_pnl=14.20, updated_ms=1700000000099),
                _make_closed_pnl(closed_pnl=12.5, updated_ms=1700000000050),
            ],
        })
        result = fetch_net_pnl(client, self._make_pos())
        assert result is not None
        assert result[0] == pytest.approx(14.20)


class TestUpdatePnlToNet:
    """v0.18: store.update_pnl_to_net корректно перезаписывает gross→net
    и адjustит daily_pnl на разницу (idempotent если уже net).
    """

    def test_basic_gross_to_net_adjusts_daily(self, store):
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_v18_test", llm_reason="t",
        )
        # gross close: +14.79 (без fee)
        store.close_position(
            pos_id, exit_price=84651.0, realized_pnl_usd=14.79,
            close_reason="test_gross", pnl_source="gross",
        )
        # net update: +14.20 (на $0.59 меньше из-за fee)
        store.update_pnl_to_net(
            pos_id, new_realized_pnl_usd=14.20, new_exit_price=84651.0,
        )
        # Чтение положения
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT realized_pnl_usd, pnl_source FROM positions WHERE id=?",
                (pos_id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(14.20)
        assert row["pnl_source"] == "net"
        # daily_pnl должен сдвинуться на -0.59
        assert store.get_today_pnl() == pytest.approx(14.20)

    def test_idempotent_when_already_net(self, store):
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_v18_idem", llm_reason="t",
        )
        store.close_position(
            pos_id, exit_price=84651.0, realized_pnl_usd=14.20,
            close_reason="test", pnl_source="net",
        )
        # Повторный update не должен ничего делать (уже net).
        before = store.get_today_pnl()
        store.update_pnl_to_net(pos_id, new_realized_pnl_usd=999.0)
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE id=?", (pos_id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(14.20)  # не изменилось
        assert store.get_today_pnl() == pytest.approx(before)

    def test_win_to_loss_after_fee_decrements_n_wins(self, store):
        """gross +0.41 (win) → net -0.23 (loss) после fee. n_wins должен
        уменьшиться на 1."""
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.015, entry_price=77109.2,
            sl_price=76500.0, tp_price=78000.0, leverage=1,
            order_link_id="ai_v18_w2l", llm_reason="t",
        )
        store.close_position(
            pos_id, exit_price=77136.5, realized_pnl_usd=0.41,
            close_reason="test", pnl_source="gross",
        )
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT n_wins, n_trades FROM daily_pnl WHERE day=?",
                (date.today().isoformat(),),
            ).fetchone()
        assert row["n_wins"] == 1 and row["n_trades"] == 1

        store.update_pnl_to_net(pos_id, new_realized_pnl_usd=-0.23)

        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT n_wins, n_trades, realized_pnl_usd FROM daily_pnl "
                "WHERE day=?", (date.today().isoformat(),),
            ).fetchone()
        assert row["n_wins"] == 0  # win сбросился
        assert row["n_trades"] == 1  # сделка не дублируется
        assert row["realized_pnl_usd"] == pytest.approx(-0.23)


class TestReconcilePnlToNet:
    """v0.18: догон gross→net в каждом full-cycle для позиций
    закрытых < 24h назад."""

    def test_reconciles_recently_closed_gross(self, store):
        from ai_trader.app.main import _reconcile_pnl_to_net
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_recon_test", llm_reason="t",
        )
        store.close_position(
            pos_id, exit_price=84651.0, realized_pnl_usd=14.79,
            close_reason="test_gross", pnl_source="gross",
        )
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [_make_closed_pnl(
                order_link_id="ai_recon_test", closed_pnl=14.20,
            )],
        })
        _reconcile_pnl_to_net(client, store, hours=24)
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT realized_pnl_usd, pnl_source FROM positions WHERE id=?",
                (pos_id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(14.20)
        assert row["pnl_source"] == "net"

    def test_skips_already_net_positions(self, store):
        from ai_trader.app.main import _reconcile_pnl_to_net
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_already_net", llm_reason="t",
        )
        store.close_position(
            pos_id, exit_price=84651.0, realized_pnl_usd=14.20,
            close_reason="test", pnl_source="net",
        )
        # Даже если API вернул другое значение — не перезаписываем
        client = _FakeClientReconcile(closed_pnl_by_symbol={
            "BTCUSDT": [_make_closed_pnl(
                order_link_id="ai_already_net", closed_pnl=999.0,
            )],
        })
        _reconcile_pnl_to_net(client, store, hours=24)
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE id=?", (pos_id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(14.20)  # не изменилось

    def test_api_failure_keeps_gross(self, store):
        """get_closed_pnl=None → позиция остаётся gross, повторим в
        следующем cycle."""
        from ai_trader.app.main import _reconcile_pnl_to_net
        pos_id = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.006, entry_price=82184.9,
            sl_price=80541.0, tp_price=84651.0, leverage=1,
            order_link_id="ai_outage", llm_reason="t",
        )
        store.close_position(
            pos_id, exit_price=84651.0, realized_pnl_usd=14.79,
            close_reason="test_gross", pnl_source="gross",
        )
        client = _FakeClientReconcile(closed_pnl_returns_none=True)
        _reconcile_pnl_to_net(client, store, hours=24)
        with store._conn() as c:  # noqa: SLF001
            row = c.execute(
                "SELECT realized_pnl_usd, pnl_source FROM positions WHERE id=?",
                (pos_id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(14.79)  # не тронуто
        assert row["pnl_source"] == "gross"  # ещё ждёт догона
