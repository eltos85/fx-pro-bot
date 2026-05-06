"""Тесты для AI-Trader: парсинг ответа LLM, killswitch, БД."""
from __future__ import annotations

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
            '"stop_loss": 60000, "take_profit": 65000, "reason": "breakout"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "open"
        assert result.raw["symbol"] == "BTCUSDT"
        assert result.raw["leverage"] == 3

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

