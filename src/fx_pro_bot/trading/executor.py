"""Исполнитель сделок: высокоуровневый интерфейс для торговли через cTrader.

Связывает логику бота (yfinance-символы, direction long/short, лоты)
с низкоуровневым cTrader API (symbolId, BUY/SELL, volume в центах).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.symbols import (
    SymbolCache,
    SymbolInfo,
    YFINANCE_TO_CTRADER,
    _YFINANCE_PREFIX_MAP,
    lots_to_volume,
    volume_to_lots,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderResult:
    success: bool
    broker_position_id: int = 0
    fill_price: float = 0.0
    volume: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class AccountInfo:
    balance: float = 0.0
    equity: float = 0.0
    margin_used: float = 0.0
    free_margin: float = 0.0
    currency: str = "USD"


class TradeExecutor:
    """Высокоуровневый исполнитель сделок.

    Использование:
        executor = TradeExecutor(client, symbol_cache, lot_size=0.01)
        result = executor.open_position("EURUSD=X", "long", sl_price=1.08)
        executor.close_position(broker_position_id=12345)
    """

    def __init__(
        self,
        client: CTraderClient,
        symbols: SymbolCache,
        lot_size: float = 0.01,
    ) -> None:
        self._client = client
        self._symbols = symbols
        self._lot_size = lot_size

    def load_symbols(self) -> int:
        """Загрузить и закешировать символы с cTrader. Вернуть количество."""
        resp = self._client.get_symbols()
        infos = []
        for s in resp.symbol:
            infos.append(
                SymbolInfo(
                    symbol_id=s.symbolId,
                    name=s.symbolName if hasattr(s, "symbolName") else str(s.symbolId),
                    min_volume=getattr(s, "minVolume", 1000),
                    max_volume=getattr(s, "maxVolume", 10_000_000),
                    step_volume=getattr(s, "stepVolume", 1000),
                    digits=getattr(s, "digits", 5),
                )
            )
        self._symbols.populate(infos)

        all_yf = {**YFINANCE_TO_CTRADER, **_YFINANCE_PREFIX_MAP}
        for yf in all_yf:
            sym = self._symbols.resolve_yfinance(yf)
            if sym:
                log.info("  ✓ %s → %s (id=%d)", yf, sym.name, sym.symbol_id)
            else:
                log.warning("  ✗ %s — не найден в cTrader", yf)

        return len(infos)

    def open_position(
        self,
        yf_symbol: str,
        direction: str,
        sl_price: float | None = None,
        tp_price: float | None = None,
        lot_size: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        """Открыть рыночную позицию.

        Args:
            yf_symbol: символ yfinance (EURUSD=X, GC=F, ...)
            direction: "long" или "short"
            sl_price: абсолютная цена Stop Loss
            tp_price: абсолютная цена Take Profit
            lot_size: размер лота (по умолчанию self._lot_size)
            comment: комментарий к ордеру
        """
        sym = self._symbols.resolve_yfinance(yf_symbol)
        if sym is None:
            return OrderResult(success=False, error=f"Символ {yf_symbol} не найден в кеше cTrader")

        lots = lot_size if lot_size is not None else self._lot_size
        volume = lots_to_volume(lots)
        volume = self._clamp_volume(volume, sym)

        trade_side = "BUY" if direction.lower() == "long" else "SELL"

        sl_rounded = round(sl_price, sym.digits) if sl_price is not None else None
        tp_rounded = round(tp_price, sym.digits) if tp_price is not None else None

        try:
            result = self._client.send_new_order(
                symbol_id=sym.symbol_id,
                trade_side=trade_side,
                volume=volume,
                stop_loss=sl_rounded,
                take_profit=tp_rounded,
                comment=comment or f"fx-pro-bot {yf_symbol} {direction}",
            )

            pos = result.position if hasattr(result, "position") else None
            return OrderResult(
                success=True,
                broker_position_id=pos.positionId if pos else 0,
                fill_price=pos.price if pos and hasattr(pos, "price") else 0.0,
                volume=volume,
            )
        except Exception as exc:
            log.error("Ошибка открытия позиции %s %s: %s", yf_symbol, direction, exc)
            return OrderResult(success=False, error=str(exc))

    def close_position(
        self,
        broker_position_id: int,
        volume: int | None = None,
    ) -> OrderResult:
        """Закрыть позицию по broker ID.

        Args:
            broker_position_id: ID позиции в cTrader
            volume: объём для закрытия (None = полное закрытие)
        """
        if volume is None:
            volume = lots_to_volume(self._lot_size)

        try:
            result = self._client.close_position(broker_position_id, volume)
            return OrderResult(success=True, broker_position_id=broker_position_id)
        except Exception as exc:
            log.error("Ошибка закрытия позиции %d: %s", broker_position_id, exc)
            return OrderResult(success=False, error=str(exc))

    def amend_sl_tp(
        self,
        broker_position_id: int,
        sl_price: float | None = None,
        tp_price: float | None = None,
        yf_symbol: str | None = None,
    ) -> bool:
        """Изменить SL/TP позиции."""
        try:
            digits = 5
            if yf_symbol:
                sym = self._symbols.resolve_yfinance(yf_symbol)
                if sym:
                    digits = sym.digits
            sl_r = round(sl_price, digits) if sl_price is not None else None
            tp_r = round(tp_price, digits) if tp_price is not None else None
            self._client.amend_position_sl_tp(broker_position_id, sl_r, tp_r)
            return True
        except Exception as exc:
            log.error("Ошибка изменения SL/TP позиции %d: %s", broker_position_id, exc)
            return False

    def get_account_info(self) -> AccountInfo:
        """Получить информацию о счёте."""
        try:
            resp = self._client.get_trader_info()
            trader = resp.trader if hasattr(resp, "trader") else resp
            return AccountInfo(
                balance=getattr(trader, "balance", 0) / 100,
                equity=getattr(trader, "balance", 0) / 100,
                margin_used=getattr(trader, "usedMargin", 0) / 100,
                free_margin=(
                    getattr(trader, "balance", 0) - getattr(trader, "usedMargin", 0)
                ) / 100,
                currency=getattr(trader, "depositCurrency", "USD"),
            )
        except Exception as exc:
            log.error("Ошибка получения данных счёта: %s", exc)
            return AccountInfo()

    def get_open_positions(self) -> list:
        """Получить список открытых позиций из cTrader."""
        try:
            resp = self._client.reconcile()
            return list(resp.position) if hasattr(resp, "position") else []
        except Exception as exc:
            log.error("Ошибка reconcile: %s", exc)
            return []

    def close_all_positions(self) -> int:
        """Аварийное закрытие всех позиций. Возвращает количество закрытых."""
        positions = self.get_open_positions()
        closed = 0
        for pos in positions:
            try:
                self._client.close_position(pos.positionId, pos.volume)
                closed += 1
                log.warning("EMERGENCY CLOSE: positionId=%d", pos.positionId)
            except Exception as exc:
                log.error("Не удалось закрыть позицию %d: %s", pos.positionId, exc)
        return closed

    @staticmethod
    def _clamp_volume(volume: int, sym: SymbolInfo) -> int:
        """Привести volume к допустимому диапазону символа."""
        volume = max(volume, sym.min_volume)
        volume = min(volume, sym.max_volume)
        if sym.step_volume > 0:
            volume = (volume // sym.step_volume) * sym.step_volume
        return volume
