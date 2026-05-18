"""Тесты FX AI Trader.

Покрытие:
- parse_action: Pydantic-schema валидация (XAUUSD + BRENT, multi-dim sentiment)
- killswitch: broker-safety checks (max_open_positions, max per symbol,
  daily/total loss). v1.0: correlation haircut + same-direction
  concentration check сняты — LLM решает сам.
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
from fx_ai_trader.safety.killswitch import KillSwitch, KillSwitchConfig
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

    def test_open_sentiment_out_of_range_clamped(self):
        """LLM иногда даёт forwardness=-0.3 (путает с polarity).
        Pydantic BeforeValidator делает clamp, не отвергает решение.

        Bug-fix 13-May-2026. Research: Pydantic ofic «Validators»,
        Instructor «Validation & Retry», pydantic blog LLM-validation.
        """
        text = (
            '{"action":"open","symbol":"XAUUSD","side":"BUY",'
            '"volume_lots":0.05,"stop_loss":2380,"take_profit":2410,'
            '"reason":"clean DXY+yield",'
            '"sentiment":{"aggregate_uncertainty":0.4,"items":['
            '{"title_snippet":"Fed dovish","relevance":0.8,"polarity":0.6,'
            '"intensity":0.7,"uncertainty":0.3,"forwardness":-0.3},'
            '{"title_snippet":"China data","relevance":1.5,"polarity":-2,'
            '"intensity":"N/A","uncertainty":null,"forwardness":2.0}'
            ']}}'
        )
        result = parse_action(text, ALLOWED)
        assert isinstance(result, ParsedAction)
        assert isinstance(result.model, OpenAction)
        s = result.model.sentiment
        assert s is not None
        item0 = s.items[0]
        assert item0.forwardness == 0.0  # был -0.3 → clamp к 0
        item1 = s.items[1]
        assert item1.relevance == 1.0    # был 1.5 → clamp к 1
        assert item1.polarity == -1.0    # был -2 → clamp к -1
        assert item1.intensity == 0.0    # был "N/A" → 0 (safe default)
        assert item1.uncertainty == 0.0  # был null → 0
        assert item1.forwardness == 1.0  # был 2.0 → 1

    def test_open_high_uncertainty_blocked(self):
        """Anti-hallucination gate — aggregate_uncertainty > 0.7 → reject open."""
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
            max_positions_per_symbol=3,
        ),
        store,
    )


class TestKillSwitch:
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
        # Per-symbol cap = 3 (= общий max_open_positions, защита sanity).
        for i in range(3):
            store.open_position(
                symbol="XAUUSD", side="BUY", volume_lots=0.01,
                entry_price=2390 + i, sl_price=2380, tp_price=2410,
                broker_position_id=None, broker_order_label="ai-fx-trader",
                llm_reason="t", is_paper=True,
            )
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="SELL")
        assert not res.allowed
        # Может быть либо "max positions reached" (глобальный лимит сработал
        # первым), либо "max positions per symbol".
        assert (
            "max positions" in res.reason
            or "per symbol" in res.reason
        )

    def test_v1_no_correlation_haircut(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        """v1.0: correlation haircut снят, size_multiplier всегда 1.0.

        LLM сам решает, коррелировать ли gold+oil long в одну сторону.
        """
        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.05,
            entry_price=2390, sl_price=2380, tp_price=2410,
            broker_position_id=None, broker_order_label="ai-fx-trader",
            llm_reason="t", is_paper=True,
        )
        res = killswitch.check_can_open_position(symbol="BZ=F", side="BUY")
        assert res.allowed
        assert res.size_multiplier == 1.0

    def test_v1_no_same_direction_block(
        self, killswitch: KillSwitch, store: AiFxTraderStore,
    ):
        """v1.0: same-direction concentration block снят. 3 same-direction
        позиции разрешены, ограничены только max_open_positions=3.
        """
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
        # 3-я BUY ещё проходит (max_open_positions=3 ещё не заполнен).
        res = killswitch.check_can_open_position(symbol="XAUUSD", side="BUY")
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


# ─── pip-value table (FxPro contract specs bug-fix 2026-05-13) ─────────


class TestPipValueTable:
    """Bug-fix 2026-05-13: BRENT pip-value был занижен в 10×.

    Источники (правило ``no-data-fitting.mdc``, ≥2 confirmation):
    1. ICE Brent Crude Futures: 1 contract = 1000 barrels, $0.01/barrel.
    2. RoboForex Pro spec: 1 lot = 1000 barrels, pip = 0.01, USD.
    3. Эмпирика на FxPro demo 46883073: 0.13 lot, 30 pip = $39 floating.
    """

    def test_xauusd_pip_value_is_1usd_per_lot(self):
        from fx_ai_trader.trading.executor import _pip_value_per_std_lot

        assert _pip_value_per_std_lot("XAUUSD") == 1.0

    def test_brent_pip_value_is_10usd_per_lot(self):
        from fx_ai_trader.trading.executor import _pip_value_per_std_lot

        assert _pip_value_per_std_lot("BZ=F") == 10.0

    def test_brent_pnl_matches_empirical_observation(self):
        """0.13 lot BRENT, move от 104.824 до 105.124 (30 pips) = $39."""
        from fx_ai_trader.trading.executor import _calc_pnl_usd

        pnl = _calc_pnl_usd(
            side="BUY", entry=104.824, exit_price=105.124,
            volume_lots=0.13, symbol="BZ=F",
        )
        assert abs(pnl - 39.0) < 0.5, f"BRENT PnL {pnl} should be ~$39"

    def test_xauusd_pnl_canonical(self):
        """0.10 lot XAUUSD, move от 2700 до 2710 (1000 pips) = $100."""
        from fx_ai_trader.trading.executor import _calc_pnl_usd

        pnl = _calc_pnl_usd(
            side="BUY", entry=2700.0, exit_price=2710.0,
            volume_lots=0.10, symbol="XAUUSD",
        )
        assert abs(pnl - 100.0) < 0.01

    def test_ng_pip_value_is_10usd_per_lot(self):
        """NG=F (NAT.GAS) pip-value = $10/lot.

        Источники (2026-05-18):
        1. CME NYMEX Henry Hub Natural Gas Futures contract spec:
           10,000 MMBtu × $0.001/MMBtu tick = $10/tick.
        2. cTrader Open API ProtoOASymbol(id=1118, NAT.GAS):
           lotSize=1_000_000, pipPosition=3 → pip_value = 0.001 × 10_000
           = $10/lot. Verified via scripts/fx_ai_scout_gas_symbols.py.
        """
        from fx_ai_trader.trading.executor import _pip_value_per_std_lot

        assert _pip_value_per_std_lot("NG=F") == 10.0

    def test_ng_pip_size_is_0_001(self):
        """NG=F pip = 0.001 USD/MMBtu (digits=3, pipPosition=3)."""
        from fx_ai_trader.trading.executor import _pip_size_for

        assert _pip_size_for("NG=F") == 0.001

    def test_ng_pnl_canonical(self):
        """0.10 lot NG, move от 3.250 до 3.350 (100 pips = $0.10) = $100.

        Sanity: $0.10 move на 0.10 lot = 0.10 × 10,000 MMBtu × $0.10/MMBtu
        = $100. По формуле pip = pip_diff × volume × pip_value =
        100 × 0.10 × $10 = $100.
        """
        from fx_ai_trader.trading.executor import _calc_pnl_usd

        pnl = _calc_pnl_usd(
            side="BUY", entry=3.250, exit_price=3.350,
            volume_lots=0.10, symbol="NG=F",
        )
        assert abs(pnl - 100.0) < 0.5, f"NG PnL {pnl} should be ~$100"

    def test_ng_short_pnl(self):
        """0.05 lot NG SHORT, move от 3.500 down to 3.400 (100 pips) = +$50."""
        from fx_ai_trader.trading.executor import _calc_pnl_usd

        pnl = _calc_pnl_usd(
            side="SELL", entry=3.500, exit_price=3.400,
            volume_lots=0.05, symbol="NG=F",
        )
        assert abs(pnl - 50.0) < 0.5, f"NG SHORT PnL {pnl} should be ~$50"

    def test_unknown_symbol_falls_back_safe(self):
        from fx_ai_trader.trading.executor import _pip_value_per_std_lot

        assert _pip_value_per_std_lot("UNKNOWN") == 1.0


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
        # NG=F (NAT.GAS) добавлен 2026-05-18. Это instrument-add, не
        # стратегическое изменение (см. prompts.py v1.1 docstring).
        assert s.symbols == ("XAUUSD", "BZ=F", "NG=F")
        assert s.order_label == "ai-fx-trader"
        assert s.trading_enabled is False  # paper по умолчанию
        assert s.poll_interval_sec == 900
        assert s.review_interval_sec == 300
        assert s.max_open_positions == 3
        # v1.0: per-symbol = общий лимит (защита от runaway, не tuning).
        assert s.max_positions_per_symbol == 3
        assert s.max_lot_size == 0.50
        # v1.0: risk_per_trade_usd и correlation_haircut удалены из
        # settings (LLM решает сам).
        assert not hasattr(s, "risk_per_trade_usd")
        assert not hasattr(s, "correlation_haircut")

    def test_db_path(self):
        s = AiFxTraderSettings()
        assert s.db_path.endswith("fx_ai_trader.sqlite")


# ─── RSS gas classification (2026-05-18 NG=F instrument-add) ────────────


class TestRssGasClassification:
    """Sanity-check что gas-keywords ловят релевантные news headlines.

    Маппинг GAS_KEYWORDS подобран по research-источникам, см.
    src/fx_ai_trader/news/rss.py docstring.
    """

    def test_ng_storage_headline_matched(self):
        from fx_ai_trader.news.rss import SYMBOL_KEYWORDS, _classify_symbols

        text = (
            "Working Gas in Storage rises 95 Bcf — EIA Weekly Natural "
            "Gas Storage Report shows bearish build vs consensus"
        )
        symbols = _classify_symbols(text, list(SYMBOL_KEYWORDS.keys()))
        assert "NG=F" in symbols

    def test_lng_terminal_headline_matched(self):
        from fx_ai_trader.news.rss import SYMBOL_KEYWORDS, _classify_symbols

        text = "Freeport LNG terminal cuts feedgas after compressor outage"
        symbols = _classify_symbols(text, list(SYMBOL_KEYWORDS.keys()))
        assert "NG=F" in symbols

    def test_weather_forecast_headline_matched(self):
        from fx_ai_trader.news.rss import SYMBOL_KEYWORDS, _classify_symbols

        text = "NOAA: polar vortex incursion forecast lifts Henry Hub natgas"
        symbols = _classify_symbols(text, list(SYMBOL_KEYWORDS.keys()))
        assert "NG=F" in symbols

    def test_oil_headline_not_classified_as_gas(self):
        """Гарантия: новость о crude не должна классифицироваться как NG=F.

        Это защита от false-positives — gas-keywords пересекаются с oil
        в зоне "EIA", "pipeline", "Henry Hub" etc. Headline только про
        WTI / Brent должен попасть в BZ=F, не в NG=F.
        """
        from fx_ai_trader.news.rss import SYMBOL_KEYWORDS, _classify_symbols

        text = "Brent crude jumps as OPEC+ extends cuts; WTI follows"
        symbols = _classify_symbols(text, list(SYMBOL_KEYWORDS.keys()))
        assert "BZ=F" in symbols
        assert "NG=F" not in symbols


# ─── broker reconcile (sync DB ↔ cTrader, 2026-05-13 bug-fix) ──────────


class _FakeAdapter:
    """Минимальный fake-adapter для тестирования reconcile-логики.

    Реализует только методы, которые трогают broker_reconcile +
    _apply_close handler. Никакого сетевого I/O.
    """

    def __init__(
        self,
        *,
        active_pids: set[int] | None,
        deals: dict[int, dict] | None = None,
        close_results: dict[int, "object"] | None = None,
        current_prices: dict[str, float] | None = None,
    ) -> None:
        self._active_pids = active_pids
        self._deals = deals or {}
        self._close_results = close_results or {}
        self._current_prices = current_prices or {}
        self.close_calls: list[tuple[int, int]] = []

    def get_active_broker_position_ids(self) -> set[int] | None:
        return self._active_pids

    def get_open_positions(self):
        """Label-filtered set of broker positions (mirrors active_pids).

        Используется belt-and-suspenders label guard в `_apply_close`:
        перед live-close проверяется что broker_pid активен у broker'а
        с нашим label. Если active_pids=None — broker API недоступно
        (caller отличает от пустого set'а).
        """
        if self._active_pids is None:
            return None
        from fx_ai_trader.trading.client_adapter import BrokerPosition

        return [
            BrokerPosition(
                position_id=pid,
                symbol_name="DUMMY",
                internal_symbol="DUMMY",
                side="BUY",
                volume=1000,
                volume_lots=0.01,
                entry_price=0.0,
                sl_price=None,
                tp_price=None,
                label="ai-fx-trader",
            )
            for pid in self._active_pids
        ]

    def get_closing_deal_for_position(
        self, broker_position_id: int, lookback_hours: int = 24,
    ) -> dict | None:
        return self._deals.get(broker_position_id)

    def close_position(self, broker_position_id: int, volume: int):
        from fx_ai_trader.trading.client_adapter import OrderResult

        self.close_calls.append((broker_position_id, volume))
        return self._close_results.get(
            broker_position_id,
            OrderResult(success=True, broker_position_id=broker_position_id),
        )

    def get_current_price(self, internal_symbol: str) -> float | None:
        return self._current_prices.get(internal_symbol)

    def get_symbol_info(self, internal_symbol: str):
        from fx_pro_bot.trading.symbols import SymbolInfo

        return SymbolInfo(
            symbol_id=1, name=internal_symbol,
            min_volume=1000, max_volume=10_000_000, step_volume=1000,
            digits=2, contract_size=10_000,
        )


class TestBrokerReconcile:
    """Bug-fix 2026-05-13: live-позиции, закрытые broker'ом (SL/TP),
    оставались stale в БД. KillSwitch не учитывал реальные потери.

    Эти тесты гарантируют что:
    1. Позиция отсутствующая у broker'а → закрывается по broker-net PnL.
    2. Позиция активная → не трогается.
    3. broker API недоступно (None) → no-op, не закрываем фантомно.
    4. Closing deal не найден → оставляем open, manual review.
    5. PnL = gross + swap + commission (broker net), не наш _calc_pnl_usd.
    """

    def test_closes_broker_closed_position(self, store: AiFxTraderStore):
        import datetime
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        pid = store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.01,
            entry_price=105.031, sl_price=104.7, tp_price=106.3,
            broker_position_id=150428404,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        # Backdate opened_at past GRACE_PERIOD_SEC=900 (15 мин) чтобы
        # reconcile её обрабатывал, а не пропускал как свежую.
        aged_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=30)
        ).isoformat()
        with store._conn() as c:
            c.execute(
                "UPDATE positions SET opened_at = ? WHERE id = ?",
                (aged_iso, pid),
            )
        adapter = _FakeAdapter(
            active_pids=set(),  # broker'a больше не имеет
            deals={150428404: {
                "deal_id": 331875628,
                "ts_ms": 1778686157455,
                "exit_price": 104.721,
                "gross_pnl_usd": -3.32,
                "swap_usd": 0.0,
                "commission_usd": 0.0,
            }},
        )
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 1
        rows = store.get_open_positions()
        assert rows == []  # позиция закрыта в БД
        with store._conn() as c:
            row = c.execute(
                "SELECT exit_price, realized_pnl_usd, close_reason "
                "FROM positions WHERE id = ?", (pid,),
            ).fetchone()
        assert row[0] == pytest.approx(104.721)
        assert row[1] == pytest.approx(-3.32)
        assert row[2] == "broker_auto"

    def test_skips_position_still_active_on_broker(
        self, store: AiFxTraderStore,
    ):
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        store.open_position(
            symbol="XAUUSD", side="BUY", volume_lots=0.07,
            entry_price=4700, sl_price=4690, tp_price=4720,
            broker_position_id=200,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        adapter = _FakeAdapter(active_pids={200})  # ещё открыта
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 0
        assert len(store.get_open_positions()) == 1

    def test_grace_period_skips_fresh_positions(
        self, store: AiFxTraderStore,
    ):
        """Race-condition fix 2026-05-18: позиции младше GRACE_PERIOD_SEC
        не должны попадать в broker_reconcile — Spotware session-state
        latency для свежих ExecutionEvent может быть до 15 минут.

        Симуляция: позиция открылась только что (свежая), broker через
        reconcile() пока её не видит (active_pids=set()). До patch'а
        бот идёт искать closing deal → WARNING лог. После patch'а
        позиция пропускается без вызова deal-history API.
        """
        import datetime
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        fresh_iso = (now - datetime.timedelta(seconds=120)).isoformat()
        with store._conn() as c:
            c.execute(
                "INSERT INTO positions (symbol, side, volume_lots, "
                "entry_price, sl_price, tp_price, broker_position_id, "
                "broker_order_label, opened_at, llm_reason, is_paper) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("BZ=F", "SELL", 0.01, 104.9, 106.0, 102.5,
                 999_111, "ai-fx-trader", fresh_iso, "fresh setup", 0),
            )
        # broker НЕ видит свежий pid (latency); deals dict пустой — если
        # фикс не работает, мы бы пошли в get_closing_deal_for_position
        # и increment'нули close_calls / warning. С фиксом — НЕТ.
        adapter = _FakeAdapter(active_pids=set(), deals={})
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 0
        # Позиция всё ещё open (правильно: мы её пропустили)
        opens = [p for p in store.get_open_positions() if p.broker_position_id == 999_111]
        assert len(opens) == 1

    def test_grace_period_lets_through_aged_positions(
        self, store: AiFxTraderStore,
    ):
        """После GRACE_PERIOD_SEC старая позиция должна обрабатываться
        обычным путём (закрыться по broker-true deal)."""
        import datetime
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        # 30 минут назад — точно > GRACE_PERIOD_SEC=900s
        aged_iso = (now - datetime.timedelta(minutes=30)).isoformat()
        with store._conn() as c:
            c.execute(
                "INSERT INTO positions (symbol, side, volume_lots, "
                "entry_price, sl_price, tp_price, broker_position_id, "
                "broker_order_label, opened_at, llm_reason, is_paper) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("BZ=F", "SELL", 0.01, 105.0, 106.0, 102.5,
                 999_222, "ai-fx-trader", aged_iso, "aged setup", 0),
            )
        adapter = _FakeAdapter(
            active_pids=set(),
            deals={999_222: {
                "deal_id": 1, "ts_ms": 0, "exit_price": 104.0,
                "gross_pnl_usd": 10.0, "swap_usd": 0.0, "commission_usd": 0.0,
            }},
        )
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 1  # aged position обрабатывается → закрылась

    def test_no_op_when_broker_api_unreachable(
        self, store: AiFxTraderStore,
    ):
        """``None`` от get_active_broker_position_ids ≠ пустой set.

        КРИТИЧНО: при сетевой проблеме НЕ закрываем все позиции как
        broker-closed (правило ``None != []`` — Bybit-агент 2026-05-07).
        """
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.01,
            entry_price=105, sl_price=104, tp_price=106,
            broker_position_id=300,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        adapter = _FakeAdapter(active_pids=None)
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 0
        assert len(store.get_open_positions()) == 1

    def test_keeps_open_when_closing_deal_not_found(
        self, store: AiFxTraderStore,
    ):
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.01,
            entry_price=105, sl_price=104, tp_price=106,
            broker_position_id=400,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        adapter = _FakeAdapter(active_pids=set(), deals={})
        closed = reconcile_broker_positions(adapter, store)
        assert closed == 0
        assert len(store.get_open_positions()) == 1

    def test_uses_broker_net_pnl_not_local_calc(
        self, store: AiFxTraderStore,
    ):
        """Симулируем 2026-05-13 BRENT id=2 close: broker gross=+92.82,
        our_formula at current_price would give +101.53. После reconcile
        в БД должна быть broker'ская цифра."""
        import datetime
        from fx_ai_trader.trading.broker_reconcile import (
            reconcile_broker_positions,
        )

        pid = store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.13,
            entry_price=104.824, sl_price=104.0, tp_price=106.0,
            broker_position_id=500,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        # Backdate past GRACE_PERIOD_SEC=900 (15 мин).
        aged_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=30)
        ).isoformat()
        with store._conn() as c:
            c.execute(
                "UPDATE positions SET opened_at = ? WHERE id = ?",
                (aged_iso, pid),
            )
        adapter = _FakeAdapter(
            active_pids=set(),
            deals={500: {
                "deal_id": 331862269, "ts_ms": 0,
                "exit_price": 105.578,
                "gross_pnl_usd": 92.82,
                "swap_usd": 0.0,
                "commission_usd": 0.0,
            }},
        )
        reconcile_broker_positions(adapter, store)
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE id = ?", (pid,),
            ).fetchone()
        assert row[0] == pytest.approx(92.82)

    def test_label_guard_skips_close_for_orphan_broker_pid(
        self, store: AiFxTraderStore,
    ):
        """Multi-bot isolation: если broker_pid в нашей БД НЕ в нашем
        label-filtered active set'е, executor НЕ должен дёргать
        ``close_position()``.

        Симуляция: позиция корраптилась/была затрана manual, broker_pid
        теперь принадлежит другому label (другому боту). closing deal
        тоже не найден (т.е. позиция жива у другого бота). Label guard
        обязан отказаться от close API call'а и пометить позицию closed
        локально с ``close_reason='label_guard_orphan'``.
        """
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trader.safety.killswitch import (
            KillSwitch,
            KillSwitchConfig,
        )
        from fx_ai_trader.trading.executor import (
            CloseAction,
            ParsedAction,
            apply_action,
        )

        pid = store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.01,
            entry_price=105.031, sl_price=104.7, tp_price=106.3,
            broker_position_id=999_999,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        # broker_pid 999_999 нет в active_pids (он у "другого" label),
        # closing deal тоже не найден → label guard сработает.
        adapter = _FakeAdapter(
            active_pids={111_111},  # какая-то другая наша позиция
            deals={},  # closing deal для 999_999 отсутствует
            current_prices={"BZ=F": 104.5},
        )
        action = ParsedAction(
            action_type="close",
            model=CloseAction(action="close", position_id=pid, reason="test"),
            raw={"action": "close", "position_id": pid, "reason": "test"},
        )
        settings = AiFxTraderSettings()
        object.__setattr__(settings, "trading_enabled", True)
        ks = KillSwitch(KillSwitchConfig(
            max_daily_loss_usd=150, max_total_loss_usd=300,
            max_open_positions=3, max_positions_per_symbol=3,
        ), store)

        result = apply_action(
            action, adapter=adapter, store=store,
            settings=settings, killswitch=ks,
        )
        assert result.executed is True
        # КЛЮЧЕВОЕ: close_position() НЕ был дёрнут (cross-bot защита).
        assert adapter.close_calls == []
        # Позиция помечена closed локально с label_guard_orphan reason.
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, close_reason "
                "FROM positions WHERE id = ?", (pid,),
            ).fetchone()
        assert row[0] == pytest.approx(0.0)  # no PnL, safe orphan-close
        assert row[1] == "label_guard_orphan"
        assert "SKIP-CLOSE" in result.summary

    def test_position_not_found_in_apply_close_recovers(
        self, store: AiFxTraderStore,
    ):
        """Если LLM сама CLOSE-ит, а broker отвечает POSITION_NOT_FOUND,
        executor должен подтянуть deal и закрыть позицию (а не вернуть
        error → потеря PnL для daily_loss)."""
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trader.safety.killswitch import (
            KillSwitch,
            KillSwitchConfig,
        )
        from fx_ai_trader.trading.client_adapter import OrderResult
        from fx_ai_trader.trading.executor import (
            CloseAction,
            ParsedAction,
            apply_action,
        )

        pid = store.open_position(
            symbol="BZ=F", side="BUY", volume_lots=0.01,
            entry_price=105.031, sl_price=104.7, tp_price=106.3,
            broker_position_id=150428404,
            broker_order_label="ai-fx-trader",
            llm_reason="setup", is_paper=False,
        )
        adapter = _FakeAdapter(
            active_pids=set(),
            deals={150428404: {
                "deal_id": 331875628, "ts_ms": 0,
                "exit_price": 104.721,
                "gross_pnl_usd": -3.32,
                "swap_usd": 0.0,
                "commission_usd": 0.0,
            }},
            close_results={150428404: OrderResult(
                success=False,
                broker_position_id=150428404,
                error="close_position: cTrader error POSITION_NOT_FOUND: not found",
            )},
            current_prices={"BZ=F": 104.368},
        )
        action = ParsedAction(
            action_type="close",
            model=CloseAction(action="close", position_id=pid, reason="sl breach"),
            raw={"action": "close", "position_id": pid, "reason": "sl breach"},
        )
        settings = AiFxTraderSettings()
        object.__setattr__(settings, "trading_enabled", True)
        ks = KillSwitch(KillSwitchConfig(
            max_daily_loss_usd=150, max_total_loss_usd=300,
            max_open_positions=3, max_positions_per_symbol=3,
        ), store)

        result = apply_action(
            action, adapter=adapter, store=store,
            settings=settings, killswitch=ks,
        )
        assert result.executed is True
        assert "broker_auto" in result.summary
        # PnL в БД — broker'ская net-цифра, не our_calc на current_price.
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, exit_price, close_reason "
                "FROM positions WHERE id = ?", (pid,),
            ).fetchone()
        assert row[0] == pytest.approx(-3.32)
        assert row[1] == pytest.approx(104.721)
        assert row[2] == "broker_auto"
