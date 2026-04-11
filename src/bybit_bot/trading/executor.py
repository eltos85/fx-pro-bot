"""Исполнитель сделок: преобразует сигналы в ордера Bybit."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, Signal, atr
from bybit_bot.config.settings import Settings
from bybit_bot.market_data.models import Bar
from bybit_bot.trading.client import BybitClient, InstrumentInfo, OrderResult

log = logging.getLogger(__name__)


@dataclass
class TradeParams:
    symbol: str
    side: str  # "Buy" | "Sell"
    qty: str
    sl: float | None = None
    tp: float | None = None


class TradeExecutor:
    """Рассчитывает размер позиции, SL/TP и отправляет ордера."""

    def __init__(
        self,
        client: BybitClient,
        settings: Settings,
        instruments: dict[str, InstrumentInfo] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._instruments = instruments or {}

    def compute_trade(
        self,
        symbol: str,
        signal: Signal,
        bars: list[Bar],
        available_balance: float,
    ) -> TradeParams | None:
        """Рассчитать параметры сделки на основе сигнала и проверить маржу.

        Размер позиции считается по account_balance из настроек (а не по API-балансу),
        чтобы на демо-счёте ($175K) торговать как на реальном ($500).
        available_balance используется только для проверки наличия свободной маржи.
        """
        if signal.direction == Direction.FLAT:
            return None

        if self._instruments and symbol not in self._instruments:
            log.debug("%s: не найден в инструментах Bybit, пропускаю", symbol)
            return None

        price = bars[-1].close
        atr_val = atr(bars)
        if atr_val <= 0:
            log.warning("%s: ATR=0, пропускаю", symbol)
            return None

        side = "Buy" if signal.direction == Direction.LONG else "Sell"

        sl_distance = atr_val * self._settings.strategy_sl_atr_mult
        tp_distance = atr_val * self._settings.strategy_tp_atr_mult

        if side == "Buy":
            sl = price - sl_distance
            tp = price + tp_distance
        else:
            sl = price + sl_distance
            tp = price - tp_distance

        capital = self._settings.account_balance
        risk_usd = capital * self._settings.capital_per_trade_pct
        qty_raw = risk_usd / (sl_distance * self._settings.leverage)

        inst = self._instruments.get(symbol)
        if inst:
            qty_rounded = self._round_qty_api(qty_raw, inst)
        else:
            qty_rounded = self._round_qty_fallback(qty_raw, symbol)

        if qty_rounded <= 0:
            log.warning("%s: qty=0 после округления (капитал $%.0f слишком мал)", symbol, capital)
            return None

        if inst and qty_rounded * price < inst.min_notional:
            log.warning("%s: notional $%.2f < min $%.0f", symbol, qty_rounded * price, inst.min_notional)
            return None

        margin_required = qty_rounded * price / self._settings.leverage
        max_margin = capital * self._settings.max_margin_per_trade_pct
        if margin_required > max_margin:
            log.warning(
                "%s: маржа $%.2f > лимит $%.2f (%.0f%% от $%.0f), пропускаю",
                symbol, margin_required, max_margin,
                self._settings.max_margin_per_trade_pct * 100, capital,
            )
            return None

        if margin_required > available_balance:
            log.warning(
                "%s: маржа $%.2f > доступно $%.2f на бирже, пропускаю",
                symbol, margin_required, available_balance,
            )
            return None

        price_prec = self._price_precision(inst.tick_size if inst else 0.01)
        sl = round(sl, price_prec)
        tp = round(tp, price_prec)

        log.info(
            "%s: qty=%s, risk=$%.2f (%.1f%%), margin=$%.2f (%.1f%% от $%.0f)",
            symbol, qty_rounded, qty_rounded * sl_distance,
            qty_rounded * sl_distance / capital * 100,
            margin_required, margin_required / capital * 100, capital,
        )

        return TradeParams(
            symbol=symbol,
            side=side,
            qty=str(qty_rounded),
            sl=sl,
            tp=tp,
        )

    def execute(self, params: TradeParams) -> OrderResult:
        """Отправить ордер на Bybit."""
        log.info(
            "Открываю %s %s qty=%s SL=%.4f TP=%.4f",
            params.side, params.symbol, params.qty, params.sl or 0, params.tp or 0,
        )
        return self._client.place_order(
            params.symbol,
            params.side,
            params.qty,
            sl=params.sl,
            tp=params.tp,
        )

    def close_position(self, symbol: str, side: str, qty: str) -> OrderResult:
        return self._client.close_position(symbol, side, qty)

    def set_leverage(self, symbol: str) -> bool:
        if self._instruments and symbol not in self._instruments:
            return False
        inst = self._instruments.get(symbol)
        lev = self._settings.leverage
        if inst and lev > inst.max_leverage:
            lev = int(inst.max_leverage)
        return self._client.set_leverage(symbol, lev)

    @staticmethod
    def _round_qty_api(qty: float, inst: InstrumentInfo) -> float:
        """Округлить qty по правилам инструмента с Bybit API."""
        if qty < inst.min_order_qty:
            return 0.0

        step = inst.qty_step
        rounded = round(qty / step) * step

        decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[1])) if step < 1 else 0
        return round(rounded, decimals)

    @staticmethod
    def _round_qty_fallback(qty: float, symbol: str) -> float:
        """Fallback для случаев без данных API (тесты)."""
        step = 0.01
        if qty < step:
            return 0.0
        rounded = round(qty / step) * step
        return round(rounded, 2)

    @staticmethod
    def _price_precision(ts: float) -> int:
        if ts >= 1:
            return 0
        s = f"{ts:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
