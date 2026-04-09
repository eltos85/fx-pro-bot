"""Исполнитель сделок: высокоуровневый интерфейс для торговли через cTrader.

Связывает логику бота (yfinance-символы, direction long/short, лоты)
с низкоуровневым cTrader API (symbolId, BUY/SELL, volume в центах).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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

        light: dict[int, str] = {}
        for s in resp.symbol:
            name = s.symbolName if hasattr(s, "symbolName") else str(s.symbolId)
            light[s.symbolId] = name

        details: dict[int, Any] = {}
        batch_size = 50
        id_list = list(light.keys())
        for i in range(0, len(id_list), batch_size):
            chunk = id_list[i : i + batch_size]
            try:
                det_resp = self._client.get_symbol_details(chunk)
                for sym_det in det_resp.symbol:
                    details[sym_det.symbolId] = sym_det
            except Exception as exc:
                log.warning("get_symbol_details batch %d failed: %s", i, exc)

        infos = []
        for sid, name in light.items():
            det = details.get(sid)
            infos.append(
                SymbolInfo(
                    symbol_id=sid,
                    name=name,
                    min_volume=getattr(det, "minVolume", 1000) if det else 1000,
                    max_volume=getattr(det, "maxVolume", 10_000_000) if det else 10_000_000,
                    step_volume=getattr(det, "stepVolume", 1000) if det else 1000,
                    digits=getattr(det, "digits", 5) if det else 5,
                    contract_size=getattr(det, "lotSize", 100_000) if det else 100_000,
                )
            )
        self._symbols.populate(infos)

        all_yf = {**YFINANCE_TO_CTRADER, **_YFINANCE_PREFIX_MAP}
        for yf in all_yf:
            sym = self._symbols.resolve_yfinance(yf)
            if sym:
                log.info("  ✓ %s → %s (id=%d, digits=%d, lot=%d)", yf, sym.name, sym.symbol_id, sym.digits, sym.contract_size)
            else:
                log.warning("  ✗ %s — не найден в cTrader", yf)

        return len(infos)

    def open_position(
        self,
        yf_symbol: str,
        direction: str,
        sl_distance: float | None = None,
        tp_distance: float | None = None,
        lot_size: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        """Открыть рыночную позицию с SL/TP.

        cTrader API: relativeTakeProfit на MARKET ордерах ненадёжен
        (известная проблема, подтверждена на форуме cTrader).
        Поэтому: relativeStopLoss в ордере, TP — amend после fill.

        Args:
            yf_symbol: символ yfinance (EURUSD=X, GC=F, ...)
            direction: "long" или "short"
            sl_distance: расстояние SL от entry в единицах цены (всегда > 0)
            tp_distance: расстояние TP от entry в единицах цены (всегда > 0)
            lot_size: размер лота (по умолчанию self._lot_size)
            comment: комментарий к ордеру
        """
        sym = self._symbols.resolve_yfinance(yf_symbol)
        if sym is None:
            return OrderResult(success=False, error=f"Символ {yf_symbol} не найден в кеше cTrader")

        lots = lot_size if lot_size is not None else self._lot_size
        requested_volume = lots_to_volume(lots, sym.contract_size)
        volume = self._clamp_volume(requested_volume, sym)

        if volume > requested_volume * 3:
            return OrderResult(
                success=False,
                error=f"min_volume слишком большой: запрос {requested_volume}, "
                      f"минимум {sym.min_volume} ({sym.name}), пропускаем",
            )

        trade_side = "BUY" if direction.lower() == "long" else "SELL"

        step = 10 ** (5 - sym.digits)
        rel_sl = self._to_relative(sl_distance, step) if sl_distance else None

        try:
            result = self._client.send_new_order(
                symbol_id=sym.symbol_id,
                trade_side=trade_side,
                volume=volume,
                relative_stop_loss=rel_sl,
                relative_take_profit=None,
                comment=comment or f"fx-pro-bot {yf_symbol} {direction}",
            )

            pos = result.position if hasattr(result, "position") else None
            pos_id = pos.positionId if pos else 0

            deal = result.deal if hasattr(result, "deal") else None
            fill_price = (
                deal.executionPrice if deal and hasattr(deal, "executionPrice") else
                pos.price if pos and hasattr(pos, "price") else
                0.0
            )

            if pos_id and tp_distance and fill_price:
                tp_price = (
                    fill_price + tp_distance if direction.lower() == "long"
                    else fill_price - tp_distance
                )
                tp_rounded = round(tp_price, sym.digits)
                existing_sl = getattr(pos, "stopLoss", None) if pos else None
                try:
                    self._client.amend_position_sl_tp(
                        pos_id,
                        stop_loss=existing_sl if existing_sl else None,
                        take_profit=tp_rounded,
                    )
                    log.info(
                        "cTrader: TP set via amend → pos %d, TP=%.5f SL=%.5f (fill=%.5f ±%.5f)",
                        pos_id, tp_rounded, existing_sl or 0, fill_price, tp_distance,
                    )
                except Exception as tp_exc:
                    log.warning("cTrader: amend TP failed for pos %d: %s", pos_id, tp_exc)

            return OrderResult(
                success=True,
                broker_position_id=pos_id,
                fill_price=fill_price,
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
                vol = pos.tradeData.volume if hasattr(pos, "tradeData") else 0
                if not vol:
                    continue
                self._client.close_position(pos.positionId, vol)
                closed += 1
                log.warning("EMERGENCY CLOSE: positionId=%d", pos.positionId)
            except Exception as exc:
                log.error("Не удалось закрыть позицию %d: %s", pos.positionId, exc)
        return closed

    def get_unrealized_pnl(self) -> dict[int, tuple[float, float]]:
        """P&L открытых позиций от бэкенда cTrader → {positionId: (gross_usd, net_usd)}."""
        try:
            resp = self._client.get_unrealized_pnl()
            digits = int(getattr(resp, "moneyDigits", 2))
            divisor = 10 ** digits
            out = {}
            for p in resp.positionUnrealizedPnL:
                out[p.positionId] = (
                    p.grossUnrealizedPnL / divisor,
                    p.netUnrealizedPnL / divisor,
                )
            return out
        except Exception as exc:
            log.error("get_unrealized_pnl failed: %s", exc)
            return {}

    def get_deal_list(self, from_ts: int, to_ts: int) -> list[dict]:
        """Закрытые сделки с grossProfit от cTrader → список dict."""
        try:
            resp = self._client.get_deal_list(from_ts, to_ts)
            deals = []
            for d in resp.deal:
                cpd = d.closePositionDetail if d.HasField("closePositionDetail") else None
                if cpd is None:
                    continue
                md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
                divisor = 10 ** md
                deals.append({
                    "dealId": d.dealId,
                    "positionId": d.positionId,
                    "symbolId": d.symbolId,
                    "volume": d.filledVolume,
                    "grossProfit": cpd.grossProfit / divisor,
                    "swap": cpd.swap / divisor,
                    "commission": cpd.commission / divisor,
                    "balance": cpd.balance / divisor,
                    "pnlFee": getattr(cpd, "pnlConversionFee", 0) / divisor,
                    "executionPrice": getattr(d, "executionPrice", 0),
                    "entryPrice": cpd.entryPrice,
                    "timestamp": d.executionTimestamp,
                })
            return deals
        except Exception as exc:
            log.error("get_deal_list failed: %s", exc)
            return []

    @staticmethod
    def _to_relative(distance: float, step: int) -> int:
        """Перевести ценовую дельту в формат cTrader (1/100000) с правильной точностью.

        step = 10^(5 - digits) — минимальный шаг для символа.
        Гарантирует результат >= step (хотя бы 1 минимальное движение цены).
        """
        raw = int(round(distance * 100_000))
        if step > 1:
            aligned = (raw // step) * step
            return max(aligned, step)
        return max(raw, 1)

    @staticmethod
    def _clamp_volume(volume: int, sym: SymbolInfo) -> int:
        """Привести volume к допустимому диапазону символа."""
        volume = max(volume, sym.min_volume)
        volume = min(volume, sym.max_volume)
        if sym.step_volume > 0:
            volume = (volume // sym.step_volume) * sym.step_volume
        return volume
