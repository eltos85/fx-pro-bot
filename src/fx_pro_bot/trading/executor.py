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

    @property
    def client(self) -> CTraderClient:
        return self._client

    @property
    def symbols(self) -> SymbolCache:
        return self._symbols

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
        entry_price_hint: float = 0.0,
    ) -> OrderResult:
        """Открыть рыночную позицию с SL/TP.

        cTrader API: relativeTakeProfit на MARKET ордерах ненадёжен
        (известная проблема, подтверждена на форуме cTrader).
        Поэтому: relativeStopLoss в ордере, TP — amend после fill.
        Первый execution event — ORDER_ACCEPTED (fill_price=0), FILLED
        приходит асинхронно, поэтому при fill_price=0 используем
        entry_price_hint или reconcile.
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
                deal.executionPrice if deal and hasattr(deal, "executionPrice") and deal.executionPrice else
                pos.price if pos and hasattr(pos, "price") and pos.price else
                0.0
            )

            if pos_id and (tp_distance or sl_distance):
                import time as _time
                _time.sleep(0.5)

                price_for_amend = fill_price
                if not price_for_amend:
                    try:
                        resp = self._client.reconcile()
                        for p in resp.position:
                            if p.positionId == pos_id:
                                price_for_amend = p.price if hasattr(p, "price") and p.price else 0.0
                                break
                    except Exception:
                        pass
                if not price_for_amend:
                    price_for_amend = entry_price_hint

                if price_for_amend:
                    is_long = direction.lower() == "long"
                    amend_tp: float | None = None
                    amend_sl: float | None = None

                    if tp_distance:
                        tp_price = price_for_amend + tp_distance if is_long else price_for_amend - tp_distance
                        amend_tp = round(tp_price, sym.digits)

                    # SL уже установлен атомарно через relative_stop_loss
                    # в send_new_order — cTrader рассчитал его от реальной
                    # fill price. В amend трогать SL НЕЛЬЗЯ: за 500ms между
                    # fill и amend цена могла уйти, и SL от reconcile.price
                    # окажется на неправильной стороне (пример NG=F 23.04:
                    # entry=2.894, SL=2.889, реальный BID=2.875 → TRADING_BAD_STOPS).
                    if sl_distance and not rel_sl:
                        sl_price = price_for_amend - sl_distance if is_long else price_for_amend + sl_distance
                        amend_sl = round(sl_price, sym.digits)

                    if amend_sl is None and amend_tp is None:
                        log.info(
                            "cTrader: SL уже установлен в order, TP не нужен (pos %d)",
                            pos_id,
                        )
                        return OrderResult(
                            success=True,
                            broker_position_id=pos_id,
                            fill_price=fill_price or entry_price_hint,
                            volume=volume,
                        )

                    log.info(
                        "cTrader SL/TP amend SEND: pos %d %s entry=%.5f sl=%s tp=%s sl_dist=%.5f tp_dist=%.5f digits=%d",
                        pos_id, direction.upper(), price_for_amend,
                        f"{amend_sl:.5f}" if amend_sl else "None",
                        f"{amend_tp:.5f}" if amend_tp else "None",
                        sl_distance or 0.0, tp_distance or 0.0, sym.digits,
                    )

                    ok = self.amend_sl_tp(
                        pos_id,
                        sl_price=amend_sl,
                        tp_price=amend_tp,
                        yf_symbol=yf_symbol,
                    )
                    if ok:
                        log.info(
                            "cTrader SL/TP amend OK: pos %d, %s%s(base=%.5f)",
                            pos_id,
                            f"SL={amend_sl:.5f} " if amend_sl else "",
                            f"TP={amend_tp:.5f} " if amend_tp else "",
                            price_for_amend,
                        )
                    else:
                        # SL/TP не поставлен — закрываем позицию сразу, чтобы
                        # не оставлять «голую» позицию без защиты на брокере.
                        # Лучше потерять 1-2 pip на закрытии, чем получить
                        # неконтролируемый убыток.
                        log.error(
                            "cTrader SL/TP amend FAILED → закрываем pos %d (%s %s)",
                            pos_id, yf_symbol, direction,
                        )
                        try:
                            self.close_position(pos_id, volume)
                        except Exception as exc:
                            log.error("Ошибка аварийного закрытия pos %d: %s", pos_id, exc)
                        return OrderResult(
                            success=False,
                            error=f"SL/TP amend failed, position {pos_id} closed",
                        )
                else:
                    log.warning("cTrader: no price for SL/TP amend on pos %d", pos_id)

            return OrderResult(
                success=True,
                broker_position_id=pos_id,
                fill_price=fill_price or entry_price_hint,
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
            volume: объём для закрытия (None = reconcile для точного volume)
        """
        if volume is None:
            volume = self._resolve_position_volume(broker_position_id)

        try:
            result = self._client.close_position(broker_position_id, volume)
            return OrderResult(success=True, broker_position_id=broker_position_id)
        except Exception as exc:
            log.error("Ошибка закрытия позиции %d: %s", broker_position_id, exc)
            return OrderResult(success=False, error=str(exc))

    def _resolve_position_volume(self, broker_position_id: int) -> int:
        """Получить реальный volume позиции из cTrader reconcile."""
        try:
            resp = self._client.reconcile()
            for p in resp.position:
                if p.positionId == broker_position_id:
                    vol = p.tradeData.volume if hasattr(p, "tradeData") else 0
                    if vol:
                        return vol
        except Exception as exc:
            log.debug("_resolve_position_volume %d failed: %s", broker_position_id, exc)
        return lots_to_volume(self._lot_size)

    def amend_sl_tp(
        self,
        broker_position_id: int,
        sl_price: float | None = None,
        tp_price: float | None = None,
        yf_symbol: str | None = None,
    ) -> bool:
        """Изменить SL/TP позиции — БЕЗОПАСНО, не затирает неуказанные поля.

        cTrader AmendPositionSLTPReq: если поле не задано, protobuf шлёт 0.0,
        что cTrader интерпретирует как «удалить». Поэтому перед amend всегда
        подтягиваем текущие SL/TP из reconcile и мержим с новыми значениями.

        Sanity check (23.04.2026): для LONG SL должен быть ниже текущей
        цены, для SHORT — выше. Симметрично для TP. Если нарушено —
        отказываемся, чтобы не получить TRADING_BAD_STOPS и не оставить
        позицию без уровней. Баг наблюдался на NG=F: amend пытался
        поставить SL=2.895 для LONG при price=2.88.
        """
        try:
            cur_sl, cur_tp = self._get_broker_sl_tp(broker_position_id)

            final_sl = sl_price if sl_price is not None else (cur_sl if cur_sl else None)
            final_tp = tp_price if tp_price is not None else (cur_tp if cur_tp else None)

            if final_sl is None and final_tp is None:
                return True

            digits = 5
            if yf_symbol:
                sym = self._symbols.resolve_yfinance(yf_symbol)
                if sym:
                    digits = sym.digits

            sl_r = round(final_sl, digits) if final_sl is not None else None
            tp_r = round(final_tp, digits) if final_tp is not None else None

            if not self._validate_sl_tp_side(broker_position_id, sl_r, tp_r):
                return False

            self._client.amend_position_sl_tp(broker_position_id, sl_r, tp_r)
            return True
        except Exception as exc:
            log.error("Ошибка изменения SL/TP позиции %d: %s", broker_position_id, exc)
            return False

    def _validate_sl_tp_side(
        self,
        broker_position_id: int,
        sl_price: float | None,
        tp_price: float | None,
    ) -> bool:
        """Проверить, что SL/TP стоят с правильной стороны от текущей цены.

        LONG:  SL < current_price < TP
        SHORT: TP < current_price < SL

        Return False если нарушено — защита от TRADING_BAD_STOPS.
        """
        if sl_price is None and tp_price is None:
            return True

        try:
            resp = self._client.reconcile()
            for p in resp.position:
                if p.positionId != broker_position_id:
                    continue

                price = p.price if hasattr(p, "price") and p.price else 0.0
                td = p.tradeData if hasattr(p, "tradeData") else None
                is_buy = td.tradeSide == 1 if td else True

                if price <= 0:
                    return True

                if sl_price is not None:
                    if is_buy and sl_price >= price:
                        log.error(
                            "  amend REJECTED #%d: LONG SL %.5f >= price %.5f",
                            broker_position_id, sl_price, price,
                        )
                        return False
                    if not is_buy and sl_price <= price:
                        log.error(
                            "  amend REJECTED #%d: SHORT SL %.5f <= price %.5f",
                            broker_position_id, sl_price, price,
                        )
                        return False

                if tp_price is not None:
                    if is_buy and tp_price <= price:
                        log.error(
                            "  amend REJECTED #%d: LONG TP %.5f <= price %.5f",
                            broker_position_id, tp_price, price,
                        )
                        return False
                    if not is_buy and tp_price >= price:
                        log.error(
                            "  amend REJECTED #%d: SHORT TP %.5f >= price %.5f",
                            broker_position_id, tp_price, price,
                        )
                        return False

                return True
        except Exception as exc:
            log.debug("_validate_sl_tp_side %d failed: %s", broker_position_id, exc)

        return True

    def _get_broker_sl_tp(self, position_id: int) -> tuple[float, float]:
        """Получить текущие (SL, TP) позиции из cTrader reconcile."""
        try:
            resp = self._client.reconcile()
            for p in resp.position:
                if p.positionId == position_id:
                    sl = p.stopLoss if hasattr(p, "stopLoss") and p.HasField("stopLoss") else 0.0
                    tp = p.takeProfit if hasattr(p, "takeProfit") and p.HasField("takeProfit") else 0.0
                    return sl, tp
        except Exception as exc:
            log.debug("_get_broker_sl_tp pos %d failed: %s", position_id, exc)
        return 0.0, 0.0

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
