"""Тесты для AI-Trader: парсинг ответа LLM, killswitch, БД."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
from ai_trader.state.db import AiTraderStore
from ai_trader.trading.executor import ParsedAction, parse_action


ALLOWED = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


# ─── parse_action ────────────────────────────────────────────────────────


# v0.11.1: для action="open" JSON обязан содержать compliance sub-object.
# Чтобы не дублировать его в каждом тесте — собран в константу.
_VALID_COMPLIANCE = (
    '"compliance": {'
    '"sl_atr_ratio": 1.8, '
    '"rr_net_fee": 2.0, '
    '"counter_trend": false, '
    '"confirmations": ["trend (4H EMA up)", "positioning (retail neutral)"]'
    '}'
)


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
            f'{_VALID_COMPLIANCE}, '
            '"reason": "breakout"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action == "open"
        assert result.raw["symbol"] == "BTCUSDT"
        assert result.raw["leverage"] == 3
        assert result.raw["compliance"]["sl_atr_ratio"] == 1.8

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
            '"stop_loss": 100, "take_profit": 110, '
            f'{_VALID_COMPLIANCE}, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "not in allowed list" in result

    def test_open_negative_leverage(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": -1, "position_size_usd": 100, '
            '"stop_loss": 60000, "take_profit": 65000, '
            f'{_VALID_COMPLIANCE}, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "leverage" in result.lower()

    # v0.11.1: новые тесты compliance валидации.

    def test_open_missing_compliance(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, "reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "compliance" in result

    def test_open_compliance_bad_sl_ratio(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, '
            '"compliance": {"sl_atr_ratio": "not-a-number", '
            '"rr_net_fee": 2.0, "counter_trend": false, '
            '"confirmations": ["a", "b"]}, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "sl_atr_ratio" in result

    def test_open_compliance_confirmations_too_few(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, '
            '"compliance": {"sl_atr_ratio": 1.8, "rr_net_fee": 2.0, '
            '"counter_trend": false, "confirmations": ["only-one"]}, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "confirmations" in result

    def test_open_compliance_counter_trend_not_bool(self):
        text = (
            '{"action": "open", "symbol": "BTCUSDT", "side": "Buy", '
            '"leverage": 3, "position_size_usd": 200, '
            '"stop_loss": 60000, "take_profit": 65000, '
            '"compliance": {"sl_atr_ratio": 1.8, "rr_net_fee": 2.0, '
            '"counter_trend": "yes", "confirmations": ["a", "b"]}, '
            '"reason": "x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "counter_trend" in result

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

    def test_msg_content_none_treated_as_empty_no_crash(self, monkeypatch):
        """Регрессия 2026-05-08 cycle 5 на VPS: DeepSeek вернул response с
        `content=None` после 504-retry от anthropic SDK. Раньше падало
        TypeError 'NoneType' object is not iterable, и весь цикл крашился.
        Теперь None-content трактуется как empty-response → сработает
        retry + no-thinking fallback."""
        from types import SimpleNamespace

        calls: list[dict] = []
        none_msg = SimpleNamespace(
            content=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=0),
        )
        good_msg = self._make_msg("RECOVERED")
        responses = [none_msg, none_msg, good_msg]

        class FakeClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                calls.append(kwargs)
                return responses.pop(0)

        client = self._make_client(monkeypatch, FakeClient)
        resp = client.ask("sys", "user")
        # Не упало — это и есть главный assertion (regression)
        assert resp.text == "RECOVERED"
        assert resp.error is None
        # Прошли 2 попытки с thinking + 1 fallback без thinking
        assert len(calls) == 3
        assert "thinking" not in calls[2]


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


# ─── build_system_prompt: подстановка plairs/limits в шаблон ─────────


class TestBuildSystemPrompt:
    """v0.4 (2026-05-07): SYSTEM_PROMPT превращён в шаблон с плейсхолдерами,
    значения подставляются из `AiTraderSettings`. Эти тесты гарантируют:
    - все %(name)s корректно подменяются (нет KeyError),
    - JSON-схема в шаблоне не ломается (фигурные скобки литеральны),
    - изменения настроек реально попадают в промпт LLM.
    """

    @staticmethod
    def _make_settings(monkeypatch, **env):
        # Чистим от лишних переменных, чтобы не наследовалось из локального .env
        for key in list(__import__("os").environ.keys()):
            if key.startswith(("AI_TRADER_", "DEEPSEEK_")):
                monkeypatch.delenv(key, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        from ai_trader.config.settings import AiTraderSettings
        return AiTraderSettings()

    def test_default_prompt_contains_default_pairs_and_limits(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt

        settings = self._make_settings(monkeypatch)
        prompt = build_system_prompt(settings)

        # Базовые лимиты дефолтного конфига (settings.py).
        assert "Maximum 5 simultaneous open positions" in prompt
        assert "Maximum leverage: 5x" in prompt
        assert "Virtual capital: $500 USD" in prompt
        assert "Maximum risk per trade: 6% of capital ($30 max" in prompt
        assert "Daily loss limit: $150" in prompt

        # 9 дефолтных пар в списке ALLOWED (TAOUSDT удалён 2026-05-10
        # как USER OVERRIDE — 3 трейда подряд в минус, см. BUILDLOG).
        for sym in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
                    "AVAXUSDT", "LTCUSDT", "ATOMUSDT", "WLDUSDT"):
            assert sym in prompt, f"{sym} должен быть в ALLOWED PAIRS"
        # TAOUSDT — отключён, не должен быть в дефолтном промпте
        assert "TAOUSDT" not in prompt

        # JSON-схема не сломана плейсхолдерами.
        assert '"action": "open"' in prompt
        assert '"action": "close"' in prompt
        assert '"action": "hold"' in prompt
        # %(min_size)d-%(max_size).0f → 50-500 для default capital
        assert "position_size_usd\": 50-500" in prompt
        assert "leverage\": 1-5" in prompt

    def test_custom_settings_propagate(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt

        settings = self._make_settings(
            monkeypatch,
            AI_TRADER_SYMBOLS="BTCUSDT,ETHUSDT,SOLUSDT",
            AI_TRADER_VIRTUAL_CAPITAL="1000",
            AI_TRADER_MAX_POSITIONS="7",
            AI_TRADER_MAX_LEVERAGE="3",
            AI_TRADER_RISK_PER_TRADE="0.01",
            AI_TRADER_MAX_DAILY_LOSS="100",
        )
        prompt = build_system_prompt(settings)

        assert "Maximum 7 simultaneous open positions" in prompt
        assert "Maximum leverage: 3x" in prompt
        assert "Virtual capital: $1000 USD" in prompt
        # 1% of $1000 = $10
        assert "1% of capital ($10 max" in prompt
        assert "Daily loss limit: $100" in prompt
        # SOLUSDT появляется (а DOGE не должен)
        assert "SOLUSDT" in prompt
        assert "DOGEUSDT" not in prompt
        # JSON-schema лимиты обновляются
        assert "leverage\": 1-3" in prompt
        assert "position_size_usd\": 50-1000" in prompt

    def test_no_unresolved_placeholders(self, monkeypatch):
        """В финальном промпте не должно остаться ни одного %(...)s — иначе
        LLM получит сырой плейсхолдер вместо значения."""
        import re

        from ai_trader.llm.prompts import build_system_prompt

        settings = self._make_settings(monkeypatch)
        prompt = build_system_prompt(settings)
        leftovers = re.findall(r"%\([a-zA-Z_]+\)[a-zA-Z.0-9]+", prompt)
        assert leftovers == [], f"unresolved placeholders in prompt: {leftovers}"


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
                 positions_returns_none=False, ticker_returns_none=False):
        self._positions = positions_by_symbol or {}
        self._tickers = ticker_by_symbol or {}
        self._positions_none = positions_returns_none
        self._ticker_none = ticker_returns_none

    def get_positions(self, symbol=None):
        if self._positions_none:
            return None
        return list(self._positions.get(symbol, []))

    def get_ticker(self, symbol):
        if self._ticker_none:
            return None
        return self._tickers.get(symbol)


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
            f'{_VALID_COMPLIANCE}, '
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
    (не для всех 9 пар) и пропускать 4H/news/macro.
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


# ─── v0.11 (2026-05-11): STOP-LOSS DISCIPLINE + PRE-DECISION CHECKLIST ───
#
# Цель: заставить LLM соблюдать SL distance >= 1.5x ATR(1H).
# Implementation:
# 1) context: REFERENCE SL BOUNDARIES блок с pre-computed числами;
# 2) prompts: STOP-LOSS DISCIPLINE раздел + обязательный CHECKLIST(open);
# 3) executor: soft warning-log + summary tag `[sl_atr=X.XX]`/`[sl_atr=X.XX!]`.


class TestSlReferenceBoundaries:
    """Pre-computed REFERENCE SL BOUNDARIES для full и review контекста."""

    @staticmethod
    def _make_indicator(atr14: float | None):
        from ai_trader.analysis.indicators import IndicatorSnapshot

        return IndicatorSnapshot(
            last_close=60000.0,
            rsi14=50.0,
            macd_line=0.0, macd_signal=0.0, macd_hist=0.0,
            atr14=atr14,
            atr14_pct=(atr14 / 60000.0 * 100) if atr14 else None,
            ema20=60000.0, ema50=60000.0,
            bb_upper=61000.0, bb_middle=60000.0, bb_lower=59000.0, bb_position=0.5,
            vwap=60000.0, vwap_dev_pct=0.0,
            rv_pct=80.0, rv_window_bars=24,
        )

    @staticmethod
    def _make_ticker(price: float):
        from ai_trader.trading.client import Ticker

        return Ticker(
            symbol="BTCUSDT", last_price=price, bid=price - 1, ask=price + 1,
            funding_rate=0.0001, volume_24h=1000.0, price_change_pct_24h=0.0,
        )

    def test_format_with_atr_emits_boundaries(self):
        from ai_trader.trading.context import SymbolSnapshot, _format_sl_reference

        snap = SymbolSnapshot(
            symbol="BTCUSDT",
            ticker=self._make_ticker(60000.0),
            bars_1h=[], bars_4h=[],
            ind_1h=self._make_indicator(atr14=300.0),
        )
        out = _format_sl_reference(snap)
        assert out is not None
        assert "REFERENCE SL BOUNDARIES" in out
        assert "1H ATR=$300" in out
        # 1.5×300 = 450, 2.0×300 = 600
        assert "$450" in out
        assert "$600" in out
        # Buy boundary: 60000 - 450 = 59550
        assert "59550" in out
        # Sell boundary: 60000 + 450 = 60450
        assert "60450" in out

    def test_format_returns_none_when_atr_missing(self):
        from ai_trader.trading.context import SymbolSnapshot, _format_sl_reference

        snap = SymbolSnapshot(
            symbol="BTCUSDT",
            ticker=self._make_ticker(60000.0),
            bars_1h=[], bars_4h=[],
            ind_1h=self._make_indicator(atr14=None),
        )
        assert _format_sl_reference(snap) is None

    def test_format_returns_none_when_ticker_missing(self):
        from ai_trader.trading.context import SymbolSnapshot, _format_sl_reference

        snap = SymbolSnapshot(
            symbol="BTCUSDT",
            ticker=None,
            bars_1h=[], bars_4h=[],
            ind_1h=self._make_indicator(atr14=300.0),
        )
        assert _format_sl_reference(snap) is None

    def test_full_context_includes_boundaries(self):
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_prompt,
        )

        snap = SymbolSnapshot(
            symbol="BTCUSDT",
            ticker=self._make_ticker(60000.0),
            bars_1h=[], bars_4h=[],
            ind_1h=self._make_indicator(atr14=300.0),
        )
        ctx = MarketContext(
            snapshots=[snap], open_positions=[],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        out = format_context_for_prompt(ctx)
        assert "REFERENCE SL BOUNDARIES" in out
        assert "1.5xATR" in out

    def test_review_context_includes_boundaries(self):
        from ai_trader.trading.context import (
            MarketContext, SymbolSnapshot, format_context_for_review,
        )

        snap = SymbolSnapshot(
            symbol="BTCUSDT",
            ticker=self._make_ticker(60000.0),
            bars_1h=[], bars_4h=[],
            ind_1h=self._make_indicator(atr14=300.0),
        )
        ctx = MarketContext(
            snapshots=[snap], open_positions=[],
            virtual_capital_usd=500.0, real_equity_usd=500.0,
        )
        out = format_context_for_review(ctx)
        assert "REFERENCE SL BOUNDARIES" in out


class TestStopLossDisciplinePrompt:
    """v0.11: STOP-LOSS DISCIPLINE + PRE-DECISION CHECKLIST в SYSTEM_PROMPT."""

    @staticmethod
    def _make_settings(monkeypatch):
        for key in list(__import__("os").environ.keys()):
            if key.startswith(("AI_TRADER_", "DEEPSEEK_")):
                monkeypatch.delenv(key, raising=False)
        from ai_trader.config.settings import AiTraderSettings
        return AiTraderSettings()

    def test_prompt_contains_stop_loss_discipline_block(self, monkeypatch):
        from ai_trader.llm.prompts import build_system_prompt

        prompt = build_system_prompt(self._make_settings(monkeypatch))
        assert "STOP-LOSS DISCIPLINE" in prompt
        assert "1.5x ATR" in prompt
        # Должно ссылаться на pre-computed REFERENCE SL BOUNDARIES
        assert "REFERENCE SL BOUNDARIES" in prompt

    def test_prompt_contains_compliance_in_open_schema(self, monkeypatch):
        """v0.11.1: compliance — обязательный sub-object в JSON-схеме open."""
        from ai_trader.llm.prompts import build_system_prompt

        prompt = build_system_prompt(self._make_settings(monkeypatch))
        # JSON-схема open должна включать compliance с 4 полями.
        assert "compliance" in prompt
        assert "sl_atr_ratio" in prompt
        assert "rr_net_fee" in prompt
        assert "counter_trend" in prompt
        assert "confirmations" in prompt
        # Должен быть COMPLIANCE-блок с правилами.
        assert "COMPLIANCE" in prompt

    def test_critical_constraints_mention_min_sl_distance(self, monkeypatch):
        """v0.11.1: CRITICAL CONSTRAINTS должен упоминать 1.5xATR и compliance в JSON."""
        from ai_trader.llm.prompts import build_system_prompt

        prompt = build_system_prompt(self._make_settings(monkeypatch))
        idx = prompt.find("CRITICAL CONSTRAINTS")
        assert idx >= 0
        tail = prompt[idx:]
        assert "1.5x ATR" in tail
        assert "compliance" in tail


class TestExecutorSlComplianceWarning:
    """soft enforcement: SL/ATR < 1.5 → WARNING + summary tag, но trade allowed."""

    @staticmethod
    def _make_executor_deps(monkeypatch, tmp_path, *, trading_enabled: bool = False):
        """Минимальные fakes для apply_action(open). Без реальной биржи."""
        from ai_trader.config.settings import AiTraderSettings
        from ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
        from ai_trader.state.db import AiTraderStore

        for key in list(__import__("os").environ.keys()):
            if key.startswith(("AI_TRADER_", "DEEPSEEK_")):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AI_TRADER_TRADING_ENABLED", "true" if trading_enabled else "false")
        settings = AiTraderSettings()

        db_path = tmp_path / "ai.sqlite"
        store = AiTraderStore(str(db_path))

        class FakeTicker:
            symbol = "BTCUSDT"
            last_price = 60000.0
            bid = 59999.0
            ask = 60001.0
            funding_rate = 0.0001
            volume_24h = 1000.0
            price_change_pct_24h = 0.0

        class FakeInfo:
            tick_size = 0.1
            qty_step = 0.001
            min_order_qty = 0.001
            max_order_qty = 100.0

        class FakeClient:
            def get_ticker(self, _sym):
                return FakeTicker()

            def get_instrument_info(self, _sym):
                return FakeInfo()

            def set_leverage(self, _sym, _lev):
                return True

            def place_order(self, **_kw):
                return {"ok": True}

            def close_position(self, *_a, **_kw):
                return {"ok": True}

            def get_positions(self, **_kw):
                return []

        ks = KillSwitch(
            KillSwitchConfig(
                max_daily_loss_usd=settings.max_daily_loss_usd,
                max_total_loss_usd=settings.max_total_loss_usd,
                max_open_positions=settings.max_open_positions,
                max_leverage=settings.max_leverage,
            ),
            store,
        )
        return settings, store, FakeClient(), ks

    def test_compliant_sl_emits_clean_tag(self, monkeypatch, tmp_path, caplog):
        """SL/ATR = 2.0 (compliant) → tag [sl_atr=2.00], no warning."""
        import logging
        from ai_trader.trading.executor import ParsedAction, apply_action

        settings, store, client, ks = self._make_executor_deps(monkeypatch, tmp_path)
        # entry≈60000, SL=59400 → dist=600, ATR=300 → ratio=2.0
        action = ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "BTCUSDT", "side": "Buy",
                "leverage": 1, "position_size_usd": 100,
                "stop_loss": 59400.0, "take_profit": 61500.0,  # R:R = 1500/600 = 2.5
                "reason": "test",
            },
        )
        with caplog.at_level(logging.WARNING, logger="ai_trader.trading.executor"):
            res = apply_action(
                action, client=client, store=store, settings=settings,
                killswitch=ks, atr_by_symbol={"BTCUSDT": 300.0},
            )
        # PAPER MODE: executed=False, но summary должен появиться (без compliance tag —
        # PAPER ветка возвращается до compliance check). Это документированное
        # поведение: compliance check срабатывает только в реальном ордере.
        assert res.summary.startswith("[PAPER]")

    def test_violation_logs_warning_via_real_order_path(
        self, monkeypatch, tmp_path, caplog
    ):
        """SL/ATR = 1.0 (violation) с trading_enabled=true → WARNING лог + tag [sl_atr=1.00!]."""
        import logging
        from ai_trader.trading.executor import ParsedAction, apply_action

        settings, store, client, ks = self._make_executor_deps(
            monkeypatch, tmp_path, trading_enabled=True
        )
        action = ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "BTCUSDT", "side": "Buy",
                "leverage": 1, "position_size_usd": 100,
                "stop_loss": 59700.0, "take_profit": 60900.0,  # R:R = 900/300 = 3.0
                "reason": "test",
            },
        )
        with caplog.at_level(logging.WARNING, logger="ai_trader.trading.executor"):
            res = apply_action(
                action, client=client, store=store, settings=settings,
                killswitch=ks, atr_by_symbol={"BTCUSDT": 300.0},
            )
        assert res.executed is True
        assert "[sl_atr=1.00!]" in res.summary
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        violation_logs = [r for r in warnings if "SL_DISCIPLINE_VIOLATION" in r.getMessage()]
        assert len(violation_logs) == 1, (
            f"expected 1 SL_DISCIPLINE_VIOLATION warning, got {len(violation_logs)}: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_compliant_sl_real_order_clean_tag(self, monkeypatch, tmp_path, caplog):
        """SL/ATR = 2.0 (compliant) в real-order ветке → tag [sl_atr=2.00], no warning."""
        import logging
        from ai_trader.trading.executor import ParsedAction, apply_action

        settings, store, client, ks = self._make_executor_deps(
            monkeypatch, tmp_path, trading_enabled=True
        )
        action = ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "BTCUSDT", "side": "Buy",
                "leverage": 1, "position_size_usd": 100,
                "stop_loss": 59400.0, "take_profit": 61500.0,
                "reason": "test",
            },
        )
        with caplog.at_level(logging.WARNING, logger="ai_trader.trading.executor"):
            res = apply_action(
                action, client=client, store=store, settings=settings,
                killswitch=ks, atr_by_symbol={"BTCUSDT": 300.0},
            )
        assert res.executed is True
        assert "[sl_atr=2.00]" in res.summary
        violation_logs = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "SL_DISCIPLINE_VIOLATION" in r.getMessage()
        ]
        assert len(violation_logs) == 0

    def test_no_atr_no_tag(self, monkeypatch, tmp_path):
        """atr_by_symbol=None → ни warning, ни tag (back-compat)."""
        from ai_trader.trading.executor import ParsedAction, apply_action

        settings, store, client, ks = self._make_executor_deps(monkeypatch, tmp_path)
        action = ParsedAction(
            action="open",
            raw={
                "action": "open", "symbol": "BTCUSDT", "side": "Buy",
                "leverage": 1, "position_size_usd": 100,
                "stop_loss": 59000.0, "take_profit": 62000.0,
                "reason": "test",
            },
        )
        res = apply_action(
            action, client=client, store=store, settings=settings,
            killswitch=ks,  # atr_by_symbol не передаём
        )
        # PAPER mode → не доходит до compliance ветки.
        assert "sl_atr=" not in res.summary
