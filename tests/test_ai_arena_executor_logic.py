"""Бизнес-логика `_apply_open` / `_apply_close` / `_resolve_net_close`.

Source compliance с gist nof1-prompt.md + правило
`.cursor/rules/ai-arena-sources.mdc`. Фокус — на **поведении**, не на
тексте prompt'а:

- net PnL берётся из Bybit `get_closed_pnl` (а не gross-расчёт)
- реальный entry_price берётся из `get_positions().avgPrice` после ордера
- exit_price берётся из `get_closed_pnl().avgExitPrice`
- `coin: "BTC"` маппится в `BTCUSDT` для всех Bybit-вызовов
- no-pyramiding: вторая позиция по тому же coin отвергается
- direction sanity: long требует SL<price<TP, short — TP<price<SL
- никаких hard-cap'ов на leverage/risk/RR (Nof1 не имеет server-side)

Используем фейковый ``AiArenaBybitClient`` (in-memory) с записью всех
вызовов — это даёт строгую проверку «в Bybit ушёл именно `BTCUSDT`»,
«именно `get_closed_pnl` дёрнулся при close», и т.д.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_arena.config.settings import AiArenaSettings
from ai_arena.state.db import AiArenaStore
from ai_arena.trading.client import (
    AiArenaBybitClient,
    ClosedPnlRecord,
    InstrumentInfo,
    Position,
    Ticker,
)
from ai_arena.trading.executor import (
    ParsedAction,
    _apply_close,
    _apply_open,
    apply_action,
    parse_action,
)


SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


# ─── Фейковый Bybit клиент ──────────────────────────────────────────────


@dataclass
class _Call:
    method: str
    args: tuple
    kwargs: dict


class FakeBybitClient(AiArenaBybitClient):
    """In-memory реализация без реальной сети.

    Не вызывает ``__init__`` родителя (у нас нет API-ключей в тестах).
    Записывает все вызовы в ``self.calls`` для verify.
    """

    def __init__(
        self,
        *,
        ticker_price: float = 100000.0,
        funding_rate: float = 0.0001,
        instrument: InstrumentInfo | None = None,
        post_open_position: Position | None = None,
        closed_pnl_records: list[ClosedPnlRecord] | None = None,
        place_order_ok: bool = True,
        get_positions_response: list[Position] | None = None,
        wallet_balance_usdt: float | None = None,
        wallet_balance_sequence: list[float | None] | None = None,
    ):
        self.calls: list[_Call] = []
        self._ticker_price = ticker_price
        self._funding_rate = funding_rate
        self._instrument = instrument or InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=0.001,
            min_order_qty=0.001,
            max_order_qty=1e6,
            tick_size=0.5,
        )
        self._post_open_position = post_open_position
        self._closed_pnl_records = closed_pnl_records or []
        self._place_order_ok = place_order_ok
        self._get_positions_response = get_positions_response
        # `wallet_balance_sequence` — для сценариев [before_close, after_close]
        # с разными значениями (тестируем balance-delta fallback).
        # `wallet_balance_usdt` — статичное значение для каждого вызова.
        self._wallet_balance_usdt = wallet_balance_usdt
        self._wallet_balance_sequence = list(wallet_balance_sequence or [])

    def _record(self, method: str, *args, **kwargs):
        self.calls.append(_Call(method=method, args=args, kwargs=kwargs))

    def get_ticker(self, symbol: str) -> Ticker | None:
        self._record("get_ticker", symbol)
        return Ticker(
            symbol=symbol,
            last_price=self._ticker_price,
            bid=self._ticker_price - 0.5,
            ask=self._ticker_price + 0.5,
            funding_rate=self._funding_rate,
            volume_24h=10000.0,
            price_change_pct_24h=1.0,
        )

    def get_instrument_info(self, symbol: str) -> InstrumentInfo | None:
        self._record("get_instrument_info", symbol)
        return InstrumentInfo(
            symbol=symbol,
            qty_step=self._instrument.qty_step,
            min_order_qty=self._instrument.min_order_qty,
            max_order_qty=self._instrument.max_order_qty,
            tick_size=self._instrument.tick_size,
        )

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._record("set_leverage", symbol, leverage)
        return True

    def place_order(self, **params) -> dict:
        self._record("place_order", **params)
        if self._place_order_ok:
            return {"ok": True, "result": {"orderId": "fake_order_id"}}
        return {"ok": False, "error": "fake error"}

    def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict:
        self._record("close_position", symbol, side, qty, link_id)
        return {"ok": True, "result": {"orderId": "fake_close_id"}}

    def get_positions(self, symbol: str | None = None):
        self._record("get_positions", symbol=symbol)
        if self._get_positions_response is not None:
            return self._get_positions_response
        if self._post_open_position is not None:
            return [self._post_open_position]
        return []

    def get_closed_pnl(
        self,
        *,
        symbol: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
    ):
        self._record(
            "get_closed_pnl",
            symbol=symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )
        return list(self._closed_pnl_records)

    def get_wallet_balance_usdt(self) -> float | None:
        self._record("get_wallet_balance_usdt")
        if self._wallet_balance_sequence:
            return self._wallet_balance_sequence.pop(0)
        return self._wallet_balance_usdt


# ─── Helpers ────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> AiArenaStore:
    return AiArenaStore(tmp_path / "ai_arena_test.sqlite")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> AiArenaSettings:
    # Изолируем env, чтобы не подцепить реальные `.env`-настройки.
    for key in [
        "AI_ARENA_BYBIT_API_KEY", "AI_ARENA_BYBIT_API_SECRET",
        "AI_ARENA_DEEPSEEK_API_KEY", "AI_ARENA_TRADING_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AI_ARENA_TRADING_ENABLED", "true")
    monkeypatch.setenv("AI_ARENA_DATA_DIR", str(tmp_path))
    return AiArenaSettings()


def _open_action(coin: str = "BTC", side: str = "buy_to_enter", **kw) -> ParsedAction:
    raw = {
        "signal": side,
        "coin": coin,
        "quantity": kw.get("quantity", 0.005),
        "leverage": kw.get("leverage", 3),
        "stop_loss": kw.get("stop_loss", 99000.0),
        "profit_target": kw.get("profit_target", 102000.0),
        "invalidation_condition": kw.get("invalidation_condition", "x"),
        "confidence": kw.get("confidence", 0.7),
        "risk_usd": kw.get("risk_usd", 5.0),
        "justification": kw.get("justification", "test"),
    }
    return ParsedAction(signal=side, raw=raw)


def _close_action(coin: str = "BTC") -> ParsedAction:
    return ParsedAction(
        signal="close",
        raw={"signal": "close", "coin": coin, "justification": "TP"},
    )


# ─── Coin mapping (фикс #7) ─────────────────────────────────────────────


class TestCoinMappingToBybit:
    """gist L168: coin = `BTC` (не `BTCUSDT`). Bybit V5 требует `BTCUSDT`.

    Маппинг происходит на стороне executor через `arena_to_bybit`. Тесты
    проверяют что в Bybit-клиент уходит именно `BTCUSDT`, а LLM
    оперирует `BTC`.
    """

    def test_apply_open_calls_bybit_with_usdt_suffix(self, store, settings):
        bybit = FakeBybitClient(
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,
                entry_price=100050.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=500.25, liquidation_price=80000.0,
            ),
        )
        result = _apply_open(_open_action("BTC"), client=bybit, store=store, settings=settings)
        assert result.executed, result.error
        # Все Bybit-вызовы должны идти на BTCUSDT, не на BTC
        for c in bybit.calls:
            sym = (
                c.kwargs.get("symbol")
                or (c.args[0] if c.args else None)
            )
            if sym is not None:
                assert sym == "BTCUSDT", (
                    f"Bybit call {c.method} получил {sym!r}, ожидался BTCUSDT"
                )

    def test_apply_close_calls_bybit_with_usdt_suffix(self, store, settings):
        # Откроем позицию вручную в БД (через store), потом закроем.
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.005, entry_price=100000.0,
            sl_price=99000.0, tp_price=102000.0, leverage=3,
            order_link_id="arena_pre", llm_justification="t",
            confidence=0.7, invalidation_condition="x", risk_usd=5.0,
        )
        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Sell", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=101500.0,
                    closed_pnl=7.32, open_fee=0.05, close_fee=0.05,
                    leverage=3.0, exec_type="Trade",
                    order_id="x", created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed, result.error
        # close_position вызван с BTCUSDT (Bybit-формат)
        close_calls = [c for c in bybit.calls if c.method == "close_position"]
        assert len(close_calls) == 1
        assert close_calls[0].args[0] == "BTCUSDT"
        # get_closed_pnl вызван с BTCUSDT
        cp_calls = [c for c in bybit.calls if c.method == "get_closed_pnl"]
        assert len(cp_calls) == 1
        assert cp_calls[0].kwargs["symbol"] == "BTCUSDT"


# ─── Real entry price (фикс #2) ─────────────────────────────────────────


class TestRealEntryPriceFromBybit:
    """gist подразумевает actual fill price (биржа — источник правды).

    Раньше мы записывали `ticker.last_price` ДО ордера. На market-fill
    с slippage это давало рассогласование. Теперь — `position.avgPrice`
    после place_order через `get_positions(symbol)`.
    """

    def test_entry_price_taken_from_bybit_avg_price(self, store, settings):
        # Bybit вернёт avgPrice=100150 (slippage +150 vs ticker 100000).
        bybit = FakeBybitClient(
            ticker_price=100000.0,
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,
                entry_price=100150.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=500.75, liquidation_price=80000.0,
            ),
        )
        result = _apply_open(_open_action("BTC"), client=bybit, store=store, settings=settings)
        assert result.executed, result.error
        # В БД должен попасть реальный fill 100150, не ticker 100000
        positions = store.get_open_positions()
        assert len(positions) == 1
        assert positions[0].entry_price == 100150.0

    def test_real_quantity_from_bybit_position(self, store, settings):
        # Если Bybit вернёт чуть другую qty (например, после rounding)
        # — использовать её, а не requested.
        bybit = FakeBybitClient(
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,  # та же
                entry_price=100050.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=500.25, liquidation_price=80000.0,
            ),
        )
        result = _apply_open(_open_action("BTC", quantity=0.005), client=bybit, store=store, settings=settings)
        assert result.executed, result.error
        positions = store.get_open_positions()
        assert positions[0].qty == 0.005

    def test_risk_usd_recomputed_with_real_entry(self, store, settings):
        # SL=99000, real entry=100200 → risk = 1200 * 0.005 = 6.0
        # (claimed=5.0 от ticker price 100000 был бы 5.0, но мы пишем actual)
        bybit = FakeBybitClient(
            ticker_price=100000.0,
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,
                entry_price=100200.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=501.0, liquidation_price=80000.0,
            ),
        )
        result = _apply_open(_open_action("BTC", stop_loss=99000.0), client=bybit, store=store, settings=settings)
        assert result.executed, result.error
        positions = store.get_open_positions()
        # risk = |100200 - 99000| * 0.005 = 6.0
        assert positions[0].risk_usd == pytest.approx(6.0, abs=0.01)


# ─── Net PnL (фикс #1) и real exit price (фикс #3) ──────────────────────


class TestNetPnLFromClosedPnlEndpoint:
    """gist подразумевает actual exchange PnL (после fees + funding).

    Bybit `/v5/position/closed-pnl` отдаёт `closedPnl` — net. Мы
    обязаны брать его, а не считать `(exit-entry)*qty` локально.
    """

    def _open_in_db(self, store, **kw):
        store.open_position(
            symbol=kw.get("symbol", "BTCUSDT"),
            side=kw.get("side", "Buy"),
            qty=kw.get("qty", 0.005),
            entry_price=kw.get("entry_price", 100000.0),
            sl_price=99000.0,
            tp_price=102000.0,
            leverage=3,
            order_link_id="arena_pre",
            llm_justification="t",
            confidence=0.7,
            invalidation_condition="x",
            risk_usd=5.0,
        )

    def test_net_pnl_used_not_gross(self, store):
        # Gross был бы (101500-100000)*0.005 = 7.5
        # Net = 7.32 (за вычетом fees) — должен попасть в БД.
        self._open_in_db(store, entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Sell", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=101500.0,
                    closed_pnl=7.32, open_fee=0.05, close_fee=0.05,
                    leverage=3.0, exec_type="Trade",
                    order_id="x", created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        # PnL в БД — net 7.32, не gross 7.50
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, exit_price FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(7.32, abs=0.001)
        # exit_price — avgExitPrice от Bybit, не ticker
        assert row["exit_price"] == pytest.approx(101500.0)

    def test_net_pnl_can_be_negative_when_gross_positive(self, store):
        # Gross = (100100-100000)*0.005 = 0.5 (positive),
        # но fees сожрали → net = -0.3.
        # Раньше БД писала +0.5, что показывало «win» там где был «loss».
        self._open_in_db(store, entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Sell", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=100100.0,
                    closed_pnl=-0.3, open_fee=0.4, close_fee=0.4,
                    leverage=3.0, exec_type="Trade",
                    order_id="x", created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(-0.3, abs=0.001)

    def test_short_position_net_pnl(self, store):
        # Short: open at 100000, close at 99000. Gross = +5.0, net = +4.5.
        self._open_in_db(store, side="Sell", entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Buy", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=99000.0,
                    closed_pnl=4.5, open_fee=0.25, close_fee=0.25,
                    leverage=3.0, exec_type="Trade",
                    order_id="x", created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(4.5, abs=0.001)

    def test_close_defers_when_closed_pnl_and_balance_both_unavailable(
        self, store, monkeypatch
    ):
        # Если get_closed_pnl=None (API outage) после всех retry И
        # wallet balance тоже недоступен → PnL=NULL в БД,
        # `reconcile_pending_pnl` на следующем цикле подберёт.
        # Это лучше чем `0.0`: пользователь не получает враньё
        # «pnl=$0.00 (net of fees)» в Telegram.
        # monkeypatch time.sleep чтобы retry прошли мгновенно.
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_in_db(store, entry_price=100000.0, qty=0.005)

        class _AllNone(FakeBybitClient):
            def get_closed_pnl(self, **kw):
                self._record("get_closed_pnl", **kw)
                return None

            def get_wallet_balance_usdt(self):
                self._record("get_wallet_balance_usdt")
                return None  # тоже упал

        bybit = _AllNone()
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        assert "pending" in result.summary  # UX: «pnl=pending…», не $0.00
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        # PnL = NULL (оба пути failed: closed-pnl + wallet-delta)
        assert row["realized_pnl_usd"] is None
        # daily_pnl не должен быть обновлён (PnL ещё неизвестен)
        with store._conn() as c:
            agg = c.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS s, "
                "COALESCE(SUM(n_trades), 0) AS n FROM daily_pnl"
            ).fetchone()
        assert agg["s"] == 0.0 and agg["n"] == 0
        # Bybit действительно вызван 6 раз (max_retries для close-action)
        cpnl_calls = [c for c in bybit.calls if c.method == "get_closed_pnl"]
        assert len(cpnl_calls) == 6
        # wallet balance вызван 1 раз — для wallet_before. После — fallback
        # пропущен (т.к. wallet_before=None, fallback не вызывается)
        wb_calls = [c for c in bybit.calls if c.method == "get_wallet_balance_usdt"]
        assert len(wb_calls) == 1

    def test_close_resolves_on_retry_when_bybit_lags(
        self, store, monkeypatch
    ):
        # Real-world сценарий: Bybit `closed-pnl` появляется через
        # ~3-5 секунд после executed close. Первая попытка возвращает
        # пустой list (ещё нет записи), вторая — уже содержит запись.
        # Бот должен подобрать PnL без отложки.
        import ai_arena.trading.executor as ex
        sleeps: list[float] = []
        monkeypatch.setattr(ex.time, "sleep", lambda s: sleeps.append(s))

        self._open_in_db(store, entry_price=100000.0, qty=0.005)

        class _LaggyCpnl(FakeBybitClient):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._calls_seen = 0

            def get_closed_pnl(self, **kw):
                self._record("get_closed_pnl", **kw)
                self._calls_seen += 1
                if self._calls_seen == 1:
                    return []  # ещё не зарегистрирован
                return [
                    ClosedPnlRecord(
                        symbol="BTCUSDT", side="Sell", qty=0.005,
                        avg_entry_price=100000.0, avg_exit_price=99500.0,
                        closed_pnl=2.45,  # net (gross 2.50 − fees 0.05)
                        open_fee=0.025, close_fee=0.025, leverage=3.0,
                        exec_type="Trade", order_id="x",
                        created_time_ms=0,
                        updated_time_ms=int(__import__("time").time() * 1000),
                    ),
                ]

        bybit = _LaggyCpnl()
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        assert "pending" not in result.summary
        assert "2.45" in result.summary or "+2.45" in result.summary
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, exit_price FROM positions "
                "WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(2.45, abs=0.001)
        assert row["exit_price"] == pytest.approx(99500.0, abs=0.001)
        cpnl_calls = [c for c in bybit.calls if c.method == "get_closed_pnl"]
        assert len(cpnl_calls) == 2  # 1 пустая + 1 с матчем
        assert sleeps == [1.0]  # один backoff между attempt 1 и 2


# ─── Balance-delta fallback (когда closed-pnl молчит) ──────────────────


class TestBalanceDeltaFallback:
    """Когда Bybit closed-pnl недоступен, вычисляем net PnL как
    ``walletBalance_after - walletBalance_before``.

    Bybit V5 ``walletBalance`` (UNIFIED) обновляется мгновенно при
    executed close (списывает realized PnL + fees сразу). Это
    надёжный обход demo latency closed-pnl endpoint (BUILDLOG
    2026-05-15).

    Bybit docs: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
    """

    def _open_in_db(self, store, *, entry_price: float, qty: float):
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=qty,
            entry_price=entry_price, sl_price=None, tp_price=None,
            leverage=3, order_link_id=f"open_{uuid.uuid4().hex[:6]}",
            llm_justification="test", confidence=0.7,
            invalidation_condition=None, risk_usd=None,
        )

    def test_close_uses_balance_delta_when_closed_pnl_silent(
        self, store, monkeypatch
    ):
        # Сценарий: closed-pnl всегда отдаёт пусто (как demo Bybit
        # 2026-05-15), но walletBalance работает. Бот должен:
        # 1. Сохранить wallet_before=$50000 в positions перед close.
        # 2. После 6 retry closed-pnl (всё пусто) → fallback к balance.
        # 3. wallet_after = $49997.55 → delta = -$2.45 = net PnL.
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_in_db(store, entry_price=100000.0, qty=0.005)

        bybit = FakeBybitClient(
            closed_pnl_records=[],  # closed-pnl всегда пусто
            # 1й вызов: wallet_before=50000 (перед close);
            # 2й вызов: wallet_after=49997.55 (для дельты)
            wallet_balance_sequence=[50000.00, 49997.55],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)

        assert result.executed
        assert "pending" not in result.summary
        assert "-2.45" in result.summary  # delta = 49997.55 - 50000 = -2.45

        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, wallet_balance_before_close "
                "FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(-2.45, abs=0.001)
        assert row["wallet_balance_before_close"] == pytest.approx(50000.0)

        # daily_pnl обновлён (lose, n_trades+=1, n_wins=0)
        with store._conn() as c:
            agg = c.execute(
                "SELECT SUM(realized_pnl_usd) AS s, SUM(n_trades) AS n, "
                "SUM(n_wins) AS w FROM daily_pnl"
            ).fetchone()
        assert agg["s"] == pytest.approx(-2.45, abs=0.001)
        assert agg["n"] == 1
        assert agg["w"] == 0  # лосс

    def test_close_uses_balance_delta_for_winning_trade(
        self, store, monkeypatch
    ):
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_in_db(store, entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[],
            wallet_balance_sequence=[50000.0, 50012.50],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        assert "+12.50" in result.summary

        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(12.50, abs=0.001)

    def test_closed_pnl_takes_priority_over_balance_delta(
        self, store, monkeypatch
    ):
        # Если closed-pnl ОТДАЛ запись — её используем, balance delta
        # НЕ нужна (даже если wallet_before сохранён).
        # Защищает от случая funding payment в окне между before/after.
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_in_db(store, entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Sell", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=99500.0,
                    closed_pnl=-2.55,  # net of fees
                    open_fee=0.025, close_fee=0.025, leverage=3.0,
                    exec_type="Trade", order_id="x",
                    created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
            # Если бы балансовый fallback сработал, дал бы -10 (50000 → 49990).
            # Закрытие должно использовать closed_pnl=-2.55, НЕ -10.
            wallet_balance_sequence=[50000.0, 49990.0],
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        assert "-2.55" in result.summary

        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(-2.55, abs=0.001)

        # closed-pnl вызван 1 раз (нашёл сразу), wallet_balance — только
        # 1 раз (для wallet_before, дельта НЕ запрашивалась).
        wb_calls = [c for c in bybit.calls if c.method == "get_wallet_balance_usdt"]
        assert len(wb_calls) == 1

    def test_wallet_before_saved_even_if_balance_check_fails(
        self, store, monkeypatch
    ):
        # Если первый вызов get_wallet_balance_usdt дал None
        # (API outage) — wallet_before не сохраняется, но close
        # всё равно отправляется. Fallback просто не сработает —
        # позиция останется pending для reconcile_pending_pnl.
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_in_db(store, entry_price=100000.0, qty=0.005)
        bybit = FakeBybitClient(
            closed_pnl_records=[],
            wallet_balance_sequence=[None],  # упал
        )
        result = _apply_close(_close_action("BTC"), client=bybit, store=store)
        assert result.executed
        assert "pending" in result.summary
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, wallet_balance_before_close "
                "FROM positions WHERE closed_at IS NOT NULL"
            ).fetchone()
        assert row["realized_pnl_usd"] is None
        assert row["wallet_balance_before_close"] is None


# ─── _reconcile_pending_pnl: добивает NULL → net PnL ────────────────────


class TestReconcilePendingPnl:
    """`_reconcile_pending_pnl` гарантирует что закрытые позиции с
    PnL=NULL (биржа не успела отдать запись за 4 retry) подберут
    PnL на следующем цикле + daily_pnl агрегат обновится РОВНО ОДИН
    РАЗ (двойной learn запрещён)."""

    def _open_and_close_with_null_pnl(self, store, *, qty: float = 0.005):
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=qty,
            entry_price=100000.0, sl_price=None, tp_price=None,
            leverage=3, order_link_id="t-link",
            llm_justification="test", confidence=0.7,
            invalidation_condition=None, risk_usd=None,
        )
        pos = store.get_open_positions()[0]
        store.close_position(
            pos.id, exit_price=99500.0, realized_pnl_usd=None,
            close_reason="exchange_closed",
        )
        return pos

    def test_pending_position_resolved_on_next_cycle(
        self, store, monkeypatch
    ):
        from ai_arena.trading.reconcile import reconcile_pending_pnl
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        pos = self._open_and_close_with_null_pnl(store)
        assert len(store.get_pending_pnl_positions()) == 1

        bybit = FakeBybitClient(
            closed_pnl_records=[
                ClosedPnlRecord(
                    symbol="BTCUSDT", side="Sell", qty=0.005,
                    avg_entry_price=100000.0, avg_exit_price=99500.0,
                    closed_pnl=2.45, open_fee=0.025, close_fee=0.025,
                    leverage=3.0, exec_type="Trade", order_id="x",
                    created_time_ms=0,
                    updated_time_ms=int(__import__("time").time() * 1000),
                ),
            ],
        )
        reconcile_pending_pnl(bybit, store, tg=None)

        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, exit_price FROM positions WHERE id = ?",
                (pos.id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(2.45, abs=0.001)
        assert row["exit_price"] == pytest.approx(99500.0, abs=0.001)
        assert store.get_pending_pnl_positions() == []
        # daily_pnl: добавлено 1 trade с pnl=2.45 (1 win)
        with store._conn() as c:
            agg = c.execute(
                "SELECT SUM(realized_pnl_usd) AS s, SUM(n_trades) AS n, "
                "SUM(n_wins) AS w FROM daily_pnl"
            ).fetchone()
        assert agg["s"] == pytest.approx(2.45, abs=0.001)
        assert agg["n"] == 1
        assert agg["w"] == 1

    def test_finalize_pending_pnl_does_not_double_count(self, store):
        # Если позиция уже была закрыта с PnL!=NULL (через
        # reconcile_closed_positions), finalize_pending_pnl должна
        # отказаться, чтобы не задвоить daily_pnl.
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.005,
            entry_price=100000.0, sl_price=None, tp_price=None,
            leverage=3, order_link_id="t-link2",
            llm_justification="test", confidence=0.7,
            invalidation_condition=None, risk_usd=None,
        )
        pos = store.get_open_positions()[0]
        store.close_position(
            pos.id, exit_price=99500.0, realized_pnl_usd=2.45,
            close_reason="exchange_closed",
        )
        # daily_pnl уже содержит 1 trade
        with pytest.raises(ValueError, match="already has PnL"):
            store.finalize_pending_pnl(
                pos.id, exit_price=99500.0, realized_pnl_usd=2.45,
            )
        # Агрегат не задвоился
        with store._conn() as c:
            agg = c.execute(
                "SELECT SUM(n_trades) AS n FROM daily_pnl"
            ).fetchone()
        assert agg["n"] == 1

    def test_pending_position_remains_when_bybit_still_silent(
        self, store, monkeypatch
    ):
        # closed-pnl=пусто И wallet_before=NULL (helper не сохраняет
        # — позиция как exchange-closed) → fallback недоступен,
        # позиция остаётся pending.
        from ai_arena.trading.reconcile import reconcile_pending_pnl
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        self._open_and_close_with_null_pnl(store)
        bybit = FakeBybitClient(closed_pnl_records=[])  # пусто
        reconcile_pending_pnl(bybit, store, tg=None)
        # Позиция осталась в pending (PnL всё ещё NULL)
        assert len(store.get_pending_pnl_positions()) == 1

    def test_pending_resolved_via_balance_delta_when_wallet_before_saved(
        self, store, monkeypatch
    ):
        # Если позиция была закрыта ботом (wallet_before сохранён)
        # И closed-pnl всё ещё молчит на reconcile-цикле — fallback
        # к balance delta срабатывает.
        from ai_arena.trading.reconcile import reconcile_pending_pnl
        import ai_arena.trading.executor as ex
        monkeypatch.setattr(ex.time, "sleep", lambda _s: None)

        pos = self._open_and_close_with_null_pnl(store)
        # Симулируем что _apply_close сохранил wallet_before
        store.set_wallet_before_close(pos.id, 50000.0)

        bybit = FakeBybitClient(
            closed_pnl_records=[],  # closed-pnl всё ещё пусто
            wallet_balance_usdt=49997.55,  # wallet_after
        )
        reconcile_pending_pnl(bybit, store, tg=None)

        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE id = ?",
                (pos.id,),
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(-2.45, abs=0.001)
        assert store.get_pending_pnl_positions() == []
        # daily_pnl обновлён ровно один раз (loss)
        with store._conn() as c:
            agg = c.execute(
                "SELECT SUM(realized_pnl_usd) AS s, SUM(n_trades) AS n, "
                "SUM(n_wins) AS w FROM daily_pnl"
            ).fetchone()
        assert agg["s"] == pytest.approx(-2.45, abs=0.001)
        assert agg["n"] == 1
        assert agg["w"] == 0


# ─── No pyramiding (gist L108) ──────────────────────────────────────────


class TestNoPyramidingPolicy:
    """gist L108: «NO pyramiding: Cannot add to existing positions
    (one position per coin maximum)»."""

    def test_second_open_for_same_coin_rejected(self, store, settings):
        bybit = FakeBybitClient(
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,
                entry_price=100000.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=500.0, liquidation_price=80000.0,
            ),
        )
        first = _apply_open(_open_action("BTC"), client=bybit, store=store, settings=settings)
        assert first.executed
        second = _apply_open(_open_action("BTC"), client=bybit, store=store, settings=settings)
        assert not second.executed
        assert second.error is not None
        assert "no pyramiding" in second.error.lower()

    def test_position_for_different_coin_allowed(self, store, settings):
        bybit_btc = FakeBybitClient(
            post_open_position=Position(
                symbol="BTCUSDT", side="Buy", size=0.005,
                entry_price=100000.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=500.0, liquidation_price=80000.0,
            ),
        )
        first = _apply_open(_open_action("BTC"), client=bybit_btc, store=store, settings=settings)
        assert first.executed
        bybit_eth = FakeBybitClient(
            ticker_price=3000.0,
            instrument=InstrumentInfo(
                symbol="ETHUSDT", qty_step=0.01, min_order_qty=0.01,
                max_order_qty=1e6, tick_size=0.01,
            ),
            post_open_position=Position(
                symbol="ETHUSDT", side="Buy", size=0.01,
                entry_price=3000.0, leverage=3.0,
                unrealised_pnl=0.0, position_value=30.0, liquidation_price=2400.0,
            ),
        )
        eth_action = _open_action(
            "ETH", quantity=0.01, stop_loss=2900.0, profit_target=3100.0,
        )
        second = _apply_open(eth_action, client=bybit_eth, store=store, settings=settings)
        assert second.executed, second.error


# ─── Direction sanity (gist L183-184) ───────────────────────────────────


class TestDirectionSanityFromSource:
    """gist L183-184:
    - profit_target must be above entry price for longs, below for shorts
    - stop_loss must be below entry price for longs, above for shorts
    """

    def test_long_with_sl_above_price_rejected(self, store, settings):
        # ticker=100000, SL=101000 (выше цены) — должно быть отвергнуто
        bybit = FakeBybitClient(ticker_price=100000.0)
        result = _apply_open(
            _open_action("BTC", stop_loss=101000.0, profit_target=102000.0),
            client=bybit, store=store, settings=settings,
        )
        assert not result.executed
        assert "LONG" in result.error
        assert "SL" in result.error

    def test_long_with_tp_below_price_rejected(self, store, settings):
        bybit = FakeBybitClient(ticker_price=100000.0)
        result = _apply_open(
            _open_action("BTC", stop_loss=99000.0, profit_target=99500.0),
            client=bybit, store=store, settings=settings,
        )
        assert not result.executed
        assert "LONG" in result.error

    def test_short_with_sl_below_price_rejected(self, store, settings):
        bybit = FakeBybitClient(ticker_price=100000.0)
        result = _apply_open(
            _open_action("BTC", side="sell_to_enter", stop_loss=99000.0, profit_target=98000.0),
            client=bybit, store=store, settings=settings,
        )
        assert not result.executed
        assert "SHORT" in result.error

    def test_short_with_tp_above_price_rejected(self, store, settings):
        bybit = FakeBybitClient(ticker_price=100000.0)
        result = _apply_open(
            _open_action("BTC", side="sell_to_enter", stop_loss=101000.0, profit_target=100500.0),
            client=bybit, store=store, settings=settings,
        )
        assert not result.executed
        assert "SHORT" in result.error


# ─── Никаких server-side hard-cap'ов (правило ai-arena-sources.mdc) ─────


class TestNoServerSideCaps:
    """Source Nof1 НЕ имеет KillSwitch / max_risk / max_lev / R:R cap.

    Все эти решения — на стороне LLM (через required JSON-поля).
    Тесты гарантируют что parser/executor пропускают любые валидные
    LLM-комбинации (даже с extreme leverage / низким R:R / большим risk).
    """

    def test_high_leverage_20x_passes_parser(self):
        text = json.dumps({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0.005, "leverage": 20,  # max source value
            "stop_loss": 99000.0, "profit_target": 102000.0,
            "invalidation_condition": "x", "confidence": 0.95,
            "risk_usd": 50.0, "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.raw["leverage"] == 20

    def test_extreme_leverage_above_20_still_passes_parser(self):
        # source говорит «1-20x» как guidance, не как hard-cap.
        # Серверный rejection бы сделал нас отступниками от source.
        text = json.dumps({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0.005, "leverage": 50,
            "stop_loss": 99000.0, "profit_target": 102000.0,
            "invalidation_condition": "x", "confidence": 0.95,
            "risk_usd": 50.0, "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.raw["leverage"] == 50

    def test_low_rr_passes_parser(self):
        # R:R 1:5 (огромный SL, мизерный TP) — source не запрещает,
        # это guidance в prompt'е.
        text = json.dumps({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0.005, "leverage": 3,
            "stop_loss": 90000.0,  # риск 10000
            "profit_target": 100200.0,  # награда 200
            "invalidation_condition": "x", "confidence": 0.5,
            "risk_usd": 50.0, "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)


# ─── apply_action диспатч ──────────────────────────────────────────────


class TestApplyActionDispatch:
    def test_hold_returns_not_executed_with_summary(self, store, settings):
        bybit = FakeBybitClient()
        action = ParsedAction(
            signal="hold",
            raw={"signal": "hold", "justification": "no edge"},
        )
        result = apply_action(action, client=bybit, store=store, settings=settings)
        assert not result.executed
        assert "HOLD" in result.summary

    def test_close_without_open_position_errors(self, store, settings):
        bybit = FakeBybitClient()
        result = apply_action(
            _close_action("BTC"), client=bybit, store=store, settings=settings,
        )
        assert not result.executed
        assert "no open position" in result.error
