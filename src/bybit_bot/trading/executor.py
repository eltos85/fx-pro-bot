"""Исполнитель сделок: преобразует сигналы в ордера Bybit."""

from __future__ import annotations

import logging
import math
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

        sl_mult = signal.sl_atr_mult if signal.sl_atr_mult is not None else self._settings.strategy_sl_atr_mult
        tp_mult = signal.tp_atr_mult if signal.tp_atr_mult is not None else self._settings.strategy_tp_atr_mult

        is_statarb = signal.pair_tag is not None and signal.sl_atr_mult is None

        if is_statarb:
            sl = None
            tp = None
            sl_distance = atr_val * 2.0
        else:
            sl_distance = atr_val * sl_mult
            tp_distance = atr_val * tp_mult
            if side == "Buy":
                sl = price - sl_distance
                tp = price + tp_distance
            else:
                sl = price + sl_distance
                tp = price - tp_distance

        capital = self._settings.account_balance
        risk_usd = capital * self._settings.capital_per_trade_pct

        max_margin = capital * self._settings.max_margin_per_trade_pct
        if is_statarb:
            max_margin = max_margin / 2

        qty_raw = risk_usd / (sl_distance * self._settings.leverage)

        inst = self._instruments.get(symbol)
        if inst:
            qty_rounded = self._round_qty_api(qty_raw, inst)
        else:
            qty_rounded = self._round_qty_fallback(qty_raw, symbol)

        if qty_rounded <= 0:
            log.warning("%s: qty=0 после округления (капитал $%.0f слишком мал)", symbol, capital)
            return None

        margin_required = qty_rounded * price / self._settings.leverage

        if margin_required > max_margin and inst:
            max_qty = max_margin * self._settings.leverage / price
            qty_rounded = self._round_qty_api(max_qty, inst)
            if qty_rounded <= 0:
                log.warning("%s: даже min qty ($%.2f маржи) > лимит $%.2f, пропускаю",
                            symbol, inst.min_order_qty * price / self._settings.leverage, max_margin)
                return None
            margin_required = qty_rounded * price / self._settings.leverage
            log.info("%s: qty уменьшен до %.6f (маржа $%.2f ≤ лимит $%.2f)",
                     symbol, qty_rounded, margin_required, max_margin)
        elif margin_required > max_margin:
            log.warning("%s: маржа $%.2f > лимит $%.2f, пропускаю",
                        symbol, margin_required, max_margin)
            return None

        if margin_required > available_balance:
            log.warning(
                "%s: маржа $%.2f > доступно $%.2f на бирже, пропускаю",
                symbol, margin_required, available_balance,
            )
            return None

        if inst and qty_rounded * price < inst.min_notional:
            log.warning("%s: notional $%.2f < min $%.0f", symbol, qty_rounded * price, inst.min_notional)
            return None

        price_prec = self._price_precision(inst.tick_size if inst else 0.01)
        if sl is not None:
            sl = round(sl, price_prec)
        if tp is not None:
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
        """Отправить ордер на Bybit с валидацией SL/TP по реальной цене."""
        sl = params.sl
        tp = params.tp

        if sl is not None or tp is not None:
            try:
                ticker = self._client.get_tickers(params.symbol)
                last_price = float(ticker.get("lastPrice", 0))
            except Exception:
                last_price = 0.0

            if last_price > 0:
                invalid = False
                if sl is not None:
                    if params.side == "Buy" and sl >= last_price:
                        log.warning("%s: SL=%.6f >= lastPrice=%.6f для Buy",
                                    params.symbol, sl, last_price)
                        invalid = True
                    elif params.side == "Sell" and sl <= last_price:
                        log.warning("%s: SL=%.6f <= lastPrice=%.6f для Sell",
                                    params.symbol, sl, last_price)
                        invalid = True
                if tp is not None and not invalid:
                    if params.side == "Buy" and tp <= last_price:
                        log.warning("%s: TP=%.6f <= lastPrice=%.6f для Buy",
                                    params.symbol, tp, last_price)
                        invalid = True
                    elif params.side == "Sell" and tp >= last_price:
                        log.warning("%s: TP=%.6f >= lastPrice=%.6f для Sell",
                                    params.symbol, tp, last_price)
                        invalid = True
                if invalid:
                    log.warning("%s: убираю SL/TP, откроюсь без них", params.symbol)
                    sl = None
                    tp = None

        log.info(
            "Открываю %s %s qty=%s SL=%.4f TP=%.4f",
            params.side, params.symbol, params.qty, sl or 0, tp or 0,
        )
        return self._client.place_order(
            params.symbol,
            params.side,
            params.qty,
            sl=sl,
            tp=tp,
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
        """Округлить qty ВНИЗ (floor) по правилам инструмента с Bybit API."""
        if qty < inst.min_order_qty:
            return 0.0

        step = inst.qty_step
        floored = math.floor(qty / step) * step

        decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[1])) if step < 1 else 0
        return round(floored, decimals)

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
