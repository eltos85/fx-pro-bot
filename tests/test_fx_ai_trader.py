"""Тесты FX AI Trader.

Покрытие:
- parse_action: Pydantic-schema валидация (XAUUSD + BRENT, multi-dim sentiment)
- killswitch: correlation-aware checks, daily/total loss limits, per-symbol cap
- token_lock: race-safe refresh с re-check
- paper reconcile: SL/TP touch detection
- volume rounding per-symbol
- label-isolation: фильтрация broker reconcile

Тесты НЕ покрывают (требуют live cTrader): adapter.place_market_order,
adapter.get_bars, ensure_valid_token_race_safe c реальной OAuth-сетью.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.safety.killswitch import (
    KillSwitch,
    KillSwitchConfig,
    _correlated_with,
)
from fx_ai_trader.state.db import AiFxTraderStore
from fx_ai_trader.trading.client_adapter import Bar
from fx_ai_trader.trading.executor import (
    CloseAction,
    HoldAction,
    OpenAction,
    ParsedAction,
    parse_action,
)
from fx_ai_trader.trading.paper_reconcile import _touched


ALLOWED = ("XAUUSD", "BZ=F")


# ─── parse_action: Pydantic schema ───────────────────────────────────────


class TestParseActionSchema:
    def test_hold_simple(self):
        text = '{"action": "hold", "reason": "no setup"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action_type == "hold"
        assert isinstance(result.model, HoldAction)

    def test_hold_with_sentiment(self):
        text = (
            '{"action": "hold", "reason": "wait", '
            '"sentiment": {"aggregate_uncertainty": 0.4, "items": ['
            '{"title_snippet": "Fed signals pause", '
            '"relevance": 0.8, "polarity": 0.2, "intensity": 0.6, '
            '"uncertainty": 0.3, "forwardness": 0.7}'
            ']}}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, HoldAction)
        assert result.model.sentiment is not None
        assert result.model.sentiment.aggregate_uncertainty == 0.4
        assert len(result.model.sentiment.items) == 1
        assert result.model.sentiment.items[0].relevance == 0.8

    def test_open_xauusd_buy_valid(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":2380.00,"take_profit":2410.00,'
            '"reason":"DXY weakness + EMA20 trend up + low uncertainty",'
            '"sentiment":{"aggregate_uncertainty":0.3,"items":[]}}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, OpenAction)
        assert result.model.symbol == "XAUUSD"
        assert result.model.side == "BUY"
        assert result.model.volume_lots == 0.05

    def test_open_brent_sell_valid(self):
        text = (
            '{"action":"open","symbol":"BZ=F","side":"SELL",'
            '"volume_lots":0.10,"stop_loss":86.50,"take_profit":84.00,'
            '"reason":"EIA build + OPEC dovish",'
            '"sentiment":{"aggregate_uncertainty":0.25,"items":[]}}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, OpenAction)
        assert result.model.symbol == "BZ=F"
        assert result.model.side == "SELL"

    def test_open_high_uncertainty_blocked(self):
        """Sentiment uncertainty gate (Risk 1 mitigation, arxiv 2603.11408)."""
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":2380,"take_profit":2410,'
            '"reason":"speculative",'
            '"sentiment":{"aggregate_uncertainty":0.85,"items":[]}}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "uncertainty" in result.lower()

    def test_open_high_uncertainty_custom_threshold(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":2380,"take_profit":2410,'
            '"reason":"x",'
            '"sentiment":{"aggregate_uncertainty":0.65,"items":[]}}'
        )
        # с порогом 0.5 — блокируется
        result = parse_action(text, ALLOWED, max_uncertainty=0.5)
        assert isinstance(result, str)
        # с порогом 0.7 — проходит
        result2 = parse_action(text, ALLOWED, max_uncertainty=0.7)
        assert isinstance(result2, ParsedAction)

    def test_open_invalid_side(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"Long",'
            '"volume_lots":0.05,"stop_loss":2380,"take_profit":2410,"reason":"x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "schema validation" in result.lower() or "side" in result.lower()

    def test_open_unknown_symbol(self):
        text = (
            '{"action":"open","symbol":"EURUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":1.07,"take_profit":1.09,"reason":"x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "not in allowed list" in result

    def test_open_negative_lots(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":-0.01,"stop_loss":2380,"take_profit":2410,"reason":"x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)

    def test_open_lots_above_max(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":20.0,"stop_loss":2380,"take_profit":2410,"reason":"x"}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, str)
        assert "schema validation" in result.lower() or "le=10" in result.lower() or "less than" in result.lower() or "10" in result

    def test_close_valid(self):
        text = '{"action": "close", "position_id": 7, "reason": "SL invalidated"}'
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, CloseAction)
        assert result.model.position_id == 7

    def test_close_review_mode_open_rejected(self):
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":2380,"take_profit":2410,"reason":"x",'
            '"sentiment":{"aggregate_uncertainty":0.3,"items":[]}}'
        )
        result = parse_action(text, ALLOWED, review_mode=True)
        assert isinstance(result, str)
        assert "review_mode" in result

    def test_markdown_fence(self):
        text = (
            "```json\n"
            '{"action": "hold", "reason": "wait"}\n'
            "```"
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert result.action_type == "hold"

    def test_extra_commentary_around_json(self):
        text = (
            "ANALYSIS: gold is consolidating, no entry.\n\n"
            "DECISION:\n"
            '{"action": "hold", "reason": "RSI 55, no signal"}\n\n'
            "End of analysis."
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_no_json(self):
        result = parse_action("Some commentary without JSON.", ALLOWED)
        assert isinstance(result, str)


# ─── killswitch ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> AiFxTraderStore:
    return AiFxTraderStore(tmp_path / "test.sqlite")


@pytest.fixture
def killswitch(store: AiFxTraderStore) -> KillSwitch:
    return KillSwitch(
        KillSwitchConfig(
            max_daily_loss_usd=150.0,
            max_total_loss_usd=300.0,
            max_open_positions=3,
            max_positions_per_symbol=2,
            correlation_haircut=0.7,
        ),
        store,
    )


class TestKillSwitch:
    def test_correlated_set(self):
        assert _correlated_with("XAUUSD") == {"BZ=F"}
        assert _correlated_with("BZ=F") == {"XAUUSD"}
        assert _correlated_with("EURUSD") == set()

    def test_empty_store_allows(self, killswitch: KillSwitch):
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="BUY")
        assert res.allowed is True
        assert res.size_multiplier == 1.0

    def test_max_positions_blocks(self, killswitch: KillSwitch, store: AiFxTraderStore):
        for i in range(3):
            store.open_position(
                symbol="XAUUSD", side="BUY", volume_lots=0.01,
                entry_price=2390 + i, sl_price=2380, tp_price=2410,
                broker_position_id=None, broker_order_label="ai-fx-trader",
                llm_reason="t", is_paper=True,
            )
        res = killswitch.check_can_open_position(symbol="BZ=F", side="BUY")
        assert not res.allowed
        assert "max positions reached" in res.reason

    def test_max_positions_per_symbol_blocks(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        for i in range(2):
            store.open_position(
                symbol="XAUUSD", side="BUY", volume_lots=0.01,
                entry_price=2390 + i, sl_price=2380, tp_price=2410,
                broker_position_id=None, broker_order_label="ai-fx-trader",
                llm_reason="t", is_paper=True,
            )
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="SELL")
        assert not res.allowed
        assert "max positions per symbol" in res.reason

    def test_correlation_haircut_on_second_same_dir(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.05,
            entry_price=2390, sl_price=2380, tp_price=2410,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        res = killswitch.check_can_open_position(symbol="BZ=F", side="BUY")
        assert res.allowed
        assert res.size_multiplier == pytest.approx(0.7)

    def test_same_direction_concentration_blocks_third(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        # 2 BUY на correlated assets (XAUUSD + BZ=F) → 3-я BUY на любом из них блок.
        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.05,
            entry_price=2390, sl_price=2380, tp_price=2410,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.10,
            entry_price=85, sl_price=84, tp_price=87,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="BUY")
        assert not res.allowed
        assert "same-direction concentration" in res.reason

    def test_opposite_direction_after_two_same_allowed(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        # 2 BUY открыты → SELL ещё можно (заполнили 3-ю позицию слот).
        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.05,
            entry_price=2390, sl_price=2380, tp_price=2410,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.10,
            entry_price=85, sl_price=84, tp_price=87,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="SELL")
        assert res.allowed

    def test_daily_loss_blocks(self, killswitch: KillSwitch, store: AiFxTraderStore):
        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.05,
            entry_price=2390, sl_price=2380, tp_price=2410,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        opened = store.get_open_positions()[0]
        store.close_position(
            opened.id, exit_price=2380, realized_pnl_usd=-160,
            close_reason="hit_limit",
        )
        res = killswitch.check_can_trade()
        assert not res.allowed
        assert "daily" in res.reason.lower()


# ─── paper reconcile: SL/TP touch ────────────────────────────────────────


class TestPaperReconcile:
    def test_long_sl_hit(self):
        bar = Bar(ts=1, open=2400, high=2402, low=2378, close=2385, volume=0)
        res = _touched(bar, "BUY", sl=2380, tp=2410)
        assert res == ("sl_hit", 2380)

    def test_long_tp_hit(self):
        bar = Bar(ts=1, open=2400, high=2412, low=2398, close=2410, volume=0)
        res = _touched(bar, "BUY", sl=2380, tp=2410)
        assert res == ("tp_hit", 2410)

    def test_long_no_touch(self):
        bar = Bar(ts=1, open=2400, high=2405, low=2395, close=2402, volume=0)
        res = _touched(bar, "BUY", sl=2380, tp=2410)
        assert res is None

    def test_short_sl_hit(self):
        bar = Bar(ts=1, open=86, high=86.6, low=85.8, close=86.4, volume=0)
        res = _touched(bar, "SELL", sl=86.5, tp=84.0)
        assert res == ("sl_hit", 86.5)

    def test_short_tp_hit(self):
        bar = Bar(ts=1, open=85, high=85.5, low=83.9, close=84.0, volume=0)
        res = _touched(bar, "SELL", sl=86.5, tp=84.0)
        assert res == ("tp_hit", 84.0)

    def test_long_sl_and_tp_in_same_bar_prefers_sl(self):
        # Gap-day: бар сразу пробил и SL и TP → SL (worst execution).
        bar = Bar(ts=1, open=2400, high=2415, low=2375, close=2410, volume=0)
        res = _touched(bar, "BUY", sl=2380, tp=2410)
        assert res == ("sl_hit", 2380)


# ─── volume rounding (CTraderFxAdapter._clamp_volume) ────────────────────


class TestVolumeRounding:
    def test_clamp_basic(self):
        from fx_pro_bot.trading.symbols import SymbolInfo

        from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

        info = SymbolInfo(
            symbol_id=1, name="XAUUSD",
            min_volume=1000, max_volume=10_000_000, step_volume=1000,
            digits=2, contract_size=100,
        )
        # 1234 → round-down к 1000 (step=1000)
        assert CTraderFxAdapter._clamp_volume(1234, info) == 1000
        # 0 → bumped к min_volume
        assert CTraderFxAdapter._clamp_volume(0, info) == 1000
        # Above max → capped
        assert CTraderFxAdapter._clamp_volume(20_000_000, info) == 10_000_000


# ─── token_lock race-safe ────────────────────────────────────────────────


class TestTokenLockRecheck:
    def test_fresh_token_no_refresh(self, tmp_path: Path):
        """Если expires_at далеко в будущем — refresh не вызывается."""
        from fx_pro_bot.trading.auth import TokenData

        from fx_ai_trader.trading.token_lock import ensure_valid_token_race_safe

        token_path = tmp_path / "ctrader_tokens.json"
        future = time.time() + 30 * 24 * 3600  # 30 дней
        token_path.write_text(
            '{"access_token":"FRESH_TOKEN","refresh_token":"RT",'
            f'"expires_at":{future},"token_type":"bearer"}}'
        )

        result = ensure_valid_token_race_safe(
            token_path, "cid", "csecret",
        )
        assert result.access_token == "FRESH_TOKEN"

    def test_concurrent_re_check_avoids_double_refresh(
        self, tmp_path: Path, monkeypatch,
    ):
        """После acquire lock делаем re-read; если другой процесс уже
        обновил токен — refresh НЕ вызывается."""
        from fx_pro_bot.trading import auth as auth_mod
        from fx_ai_trader.trading import token_lock as tl_mod
        from fx_ai_trader.trading.token_lock import ensure_valid_token_race_safe

        token_path = tmp_path / "ctrader_tokens.json"
        # Старый токен expires в ближайший час → нужен refresh.
        soon = time.time() + 600
        token_path.write_text(
            '{"access_token":"OLD","refresh_token":"OLD_RT",'
            f'"expires_at":{soon},"token_type":"bearer"}}'
        )

        # Сым refresh: фейлим если кто-то его вызвал (мы должны увидеть
        # свежий on-disk token и не звать refresh).
        called = {"n": 0}

        def fake_refresh(*args, **kwargs):
            called["n"] += 1
            return auth_mod.TokenData(
                access_token="REFRESHED",
                refresh_token="NEW_RT",
                expires_at=time.time() + 2_628_000,
            )

        # Эмулируем "другой процесс перезаписал файл свежими токенами,
        # пока мы ждали на flock" — патчим _read_token:
        original_read = tl_mod._read_token
        call_seq = []

        def patched_read(path):
            data = original_read(path)
            call_seq.append(data.access_token)
            # На второе чтение (под flock) вернём freshly-refreshed файл.
            if len(call_seq) >= 2:
                far_future = time.time() + 60 * 24 * 3600
                # Реально перезаписываем файл — имитирует параллельный процесс.
                token_path.write_text(
                    '{"access_token":"BY_OTHER","refresh_token":"OTHER_RT",'
                    f'"expires_at":{far_future},"token_type":"bearer"}}'
                )
                return original_read(path)
            return data

        monkeypatch.setattr(tl_mod, "_read_token", patched_read)
        monkeypatch.setattr(tl_mod, "refresh_access_token", fake_refresh)

        result = ensure_valid_token_race_safe(token_path, "cid", "csecret")
        assert result.access_token == "BY_OTHER"
        assert called["n"] == 0


# ─── settings ────────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults(self):
        s = AiFxTraderSettings()
        assert s.symbols == ("XAUUSD", "BZ=F")
        assert s.order_label == "ai-fx-trader"
        assert s.trading_enabled is False  # paper по умолчанию
        assert s.poll_interval_sec == 900
        assert s.review_interval_sec == 300
        assert s.max_open_positions == 3
        assert s.max_positions_per_symbol == 2
        assert s.risk_per_trade_usd == 25.0
        assert s.correlation_haircut == 0.7

    def test_db_path(self):
        s = AiFxTraderSettings()
        assert s.db_path.endswith("fx_ai_trader.sqlite")
