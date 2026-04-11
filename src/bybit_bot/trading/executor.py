"""Исполнитель сделок: преобразует сигналы в ордера Bybit."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, Signal, atr
from bybit_bot.config.settings import Settings, tick_size
from bybit_bot.market_data.models import Bar
from bybit_bot.trading.client import BybitClient, OrderResult

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

    def __init__(self, client: BybitClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def compute_trade(
        self,
        symbol: str,
        signal: Signal,
        bars: list[Bar],
        balance: float,
    ) -> TradeParams | None:
        """Рассчитать параметры сделки на основе сигнала и проверить маржу."""
        if signal.direction == Direction.FLAT:
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

        risk_usd = balance * self._settings.capital_per_trade_pct
        qty_raw = risk_usd / (sl_distance * self._settings.leverage)

        ts = tick_size(symbol)
        qty_rounded = self._round_qty(qty_raw, symbol, ts)
        if qty_rounded <= 0:
            log.warning("%s: qty=0 после округления (баланс $%.0f слишком мал для %s)", symbol, balance, symbol)
            return None

        margin_required = qty_rounded * price / self._settings.leverage
        max_margin = balance * self._settings.max_margin_per_trade_pct
        if margin_required > max_margin:
            log.warning(
                "%s: маржа $%.2f > лимит $%.2f (%.0f%% баланса), пропускаю",
                symbol, margin_required, max_margin,
                self._settings.max_margin_per_trade_pct * 100,
            )
            return None

        sl = round(sl, self._price_precision(ts))
        tp = round(tp, self._price_precision(ts))

        log.info(
            "%s: qty=%.6f, risk=$%.2f (%.1f%%), margin=$%.2f (%.1f%% баланса)",
            symbol, qty_rounded, qty_rounded * sl_distance,
            qty_rounded * sl_distance / balance * 100,
            margin_required, margin_required / balance * 100,
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
        return self._client.set_leverage(symbol, self._settings.leverage)

    @staticmethod
    def _round_qty(qty: float, symbol: str, ts: float) -> float:
        min_qty_map = {
            "BTCUSDT": 0.001,
            "ETHUSDT": 0.01,
            "SOLUSDT": 0.1,
            "XRPUSDT": 1.0,
            "BNBUSDT": 0.01,
            "DOGEUSDT": 10.0,
            "ADAUSDT": 1.0,
            "LINKUSDT": 0.1,
            "AVAXUSDT": 0.1,
            "LTCUSDT": 0.01,
            "DOTUSDT": 0.1,
            "MATICUSDT": 1.0,
            "NEARUSDT": 0.1,
            "APTUSDT": 0.1,
            "ARBUSDT": 1.0,
            "SUIUSDT": 0.1,
            "UNIUSDT": 0.1,
            "AAVEUSDT": 0.01,
            "ATOMUSDT": 0.1,
            "TRXUSDT": 1.0,
            "FILUSDT": 0.1,
            "INJUSDT": 0.1,
            "FETUSDT": 1.0,
            "RENDERUSDT": 0.1,
            "TONUSDT": 0.1,
            "SEIUSDT": 1.0,
            "TIAUSDT": 0.1,
            "ONDOUSDT": 1.0,
            "PENDLEUSDT": 0.1,
            "WLDUSDT": 1.0,
            "OPUSDT": 1.0,
            "HBARUSDT": 1.0,
            "RUNEUSDT": 0.1,
            "ALGOUSDT": 1.0,
            "SHIBUSDT": 100.0,
            "PEPEUSDT": 100.0,
            "WIFUSDT": 1.0,
            "BONKUSDT": 100.0,
            "FLOKIUSDT": 100.0,
        }
        min_qty = min_qty_map.get(symbol, 0.01)

        if qty < min_qty:
            return 0.0

        step = min_qty
        rounded = round(qty / step) * step

        decimals = max(0, len(str(step).rstrip("0").split(".")[-1])) if "." in str(step) else 0
        return round(rounded, decimals)

    @staticmethod
    def _price_precision(ts: float) -> int:
        if ts >= 1:
            return 0
        s = f"{ts:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
