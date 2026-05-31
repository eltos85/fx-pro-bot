"""Исполнитель сделок scalp_bot.

Два режима (settings.trading_enabled):
- PAPER  (False): ордера НЕ ставятся; позиция симулируется на live-цене
  из WS, TP/SL/тайм-стоп считаются локально, net считается с учётом
  модельных комиссий. Для shadow-валидации без риска.
- LIVE   (True, на Bybit DEMO): post-only LIMIT вход с биржевыми SL/TP,
  reduce-only MARKET выход по тайм-стопу.

Размер позиции (settings.risk_based_sizing, дефолт True) — РИСК-базированный:
qty = risk_per_trade_usd / |entry−SL| (канон профи «стоп с графика, размер —
следствие», TradeOlogy/DYOR/StockCharts 2026). $-риск на сделку фиксирован,
широкий стоп лишь уменьшает лот. При R≈0.44% и риске $1 notional≈$227.
Legacy-режим (risk_based_sizing=False) — фикс-notional ($position_usd), где
$-риск НЕ фиксирован (= notional × дистанция SL в %). Killswitch в обоих
режимах ограничивает суммарный дневной/совокупный убыток.

Комиссии Bybit linear (https://www.bybit.com/en/help-center/article/Trading-Fee-Structure):
maker 0.02%, taker 0.055%. PAPER моделирует вход maker + выход taker.
"""
from __future__ import annotations

import logging
import time

from scalp_bot.analysis.signals import Signal

log = logging.getLogger("scalp_bot.exec")
play = logging.getLogger("scalp_bot.play")  # пошаговый нарратив торговли

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055

# Закрытие → человеческая причина для плейбук-логов
_CLOSE_RU = {
    "tp": "цель достигнута (TP)", "sl": "выбило по стопу (SL)",
    "tp_hit": "цель достигнута (биржевой TP)",
    "sl_hit": "выбило по стопу (биржевой SL)",
    "tp_sl": "биржа закрыла по TP/SL",  # legacy (старые строки до v0.9.4)
    "time_stop": "тайм-стоп (не пошло за время)",
    "flow_exit": "поток развернулся против — фиксирую профит раньше",
    "flow_scratch": "поток развернулся против в минусе — режу убыток рано",
    "density_gone": "стена в стакане исчезла — тезис снят, выхожу",
    "entry_Cancelled": "ордер входа отменён биржей", "entry_timeout": "вход не исполнился вовремя",
}


def bracket_exit_reason(side: str, entry: float, exit_price: float | None) -> str:
    """Расщепляет биржевой bracket-выход (v0.9.4) на tp_hit / sl_hit по знаку
    хода цены. Биржа сама закрыла позицию — это наш TP@take_profit_r или SL@−1R;
    раньше всё писалось одним ярлыком tp_sl, что мешало отличить «цель добежала»
    от «выбило стопом». exit_price неизвестен → tp_sl (legacy-фолбэк)."""
    if exit_price is None or entry <= 0:
        return "tp_sl"
    favorable = (exit_price - entry) if side == "long" else (entry - exit_price)
    return "tp_hit" if favorable >= 0 else "sl_hit"


def qty_decimals(step: float) -> int:
    """Число знаков после запятой в шаге лота (для квантизации без float-мусора)."""
    if step <= 0:
        return 8
    d = f"{step:.10f}".rstrip("0")
    return len(d.split(".")[1]) if "." in d else 0


def position_size(position_usd: float, entry: float, *, min_notional: float = 0.0,
                  qty_step: float = 0.0, min_qty: float = 0.0) -> float:
    """qty из целевого notional ($). Пол min_notional (мелкий лот = комиссия
    съедает прибыль). Округление вниз под qty_step, отсев < min_qty биржи.

    Квантизация round(..., decimals) убирает float-артефакты вида
    1.2000000000000002 (Bybit ErrCode 10001 «Qty invalid»).
    """
    if entry <= 0 or position_usd <= 0:
        return 0.0
    notional = max(position_usd, min_notional)
    qty = notional / entry
    if qty_step > 0:
        import math
        qty = round(math.floor(qty / qty_step) * qty_step, qty_decimals(qty_step))
    if min_qty > 0 and qty < min_qty:
        # биржевой минимум выше нашего лота — берём биржевой минимум
        qty = round(min_qty, qty_decimals(qty_step)) if qty_step > 0 else min_qty
    return qty


def position_size_by_risk(risk_usd: float, entry: float, sl: float, *,
                          min_notional: float = 0.0, qty_step: float = 0.0,
                          min_qty: float = 0.0) -> float:
    """qty из фиксированного $-риска: qty = risk_usd / |entry−sl| (канон профи
    «стоп с графика, размер — следствие»). Широкий стоп → меньше лот, $-риск
    постоянен. Пол min_notional (мелкий лот = комиссия съедает), округление под
    qty_step, отсев < min_qty. Источники: TradeOlogy/DYOR/StockCharts 2026."""
    if entry <= 0 or risk_usd <= 0:
        return 0.0
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0
    qty = risk_usd / dist
    if qty * entry < min_notional:  # пол по notional (мелкий лот)
        qty = min_notional / entry
    if qty_step > 0:
        import math
        qty = round(math.floor(qty / qty_step) * qty_step, qty_decimals(qty_step))
    if min_qty > 0 and qty < min_qty:
        qty = round(min_qty, qty_decimals(qty_step)) if qty_step > 0 else min_qty
    return qty


def paper_pnl(side: str, entry: float, exit_price: float, qty: float) -> tuple[float, float]:
    """(net_pnl, fees) для PAPER: вход maker, выход taker."""
    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    fees = qty * (entry * MAKER_FEE + exit_price * TAKER_FEE)
    return (gross - fees, fees)


def taker_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    """Консервативная оценка net PnL (обе ноги taker) — fallback для killswitch,
    когда биржевой closedPnl недоступен. Чуть завышает издержки (вход обычно
    maker), т.е. оценка осторожная в сторону убытка."""
    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    fees = qty * (entry + exit_price) * TAKER_FEE
    return gross - fees


class Executor:
    def __init__(self, db, settings, client=None, *, notifier=None,
                 strategies=None, now=time.time) -> None:
        self._db = db
        self._cfg = settings
        self._client = client
        self._notifier = notifier
        self._now = now
        # реестр стратегий по имени — для диспетча дискреционного выхода
        # (позицию сопровождает та же стратегия, что открыла).
        self._strategies: dict = {getattr(s, "name", ""): s
                                  for s in (strategies or [])}
        # in-memory трекинг live-входов: trade_id -> {link, filled, ts}
        self._pending: dict[int, dict] = {}
        self._hold_log: dict[int, float] = {}  # троттлинг «держу позицию»-логов
        # атрибуция филлов из приватного WS execution (источник истины по P&L):
        #   _link2trade: orderLinkId -> trade_id (вход и выход тегаются нами);
        #   _fills: trade_id -> аккумулятор {fee, pnl, close_val, close_qty}.
        # net сделки = Σ execPnl − Σ execFee (= Bybit closedPnl), без REST.
        self._link2trade: dict[str, int] = {}
        self._fills: dict[int, dict] = {}
        # отложенные close-уведомления: tid -> {ts, label, symbol}. Шлём из
        # reconcile() с РЕАЛЬНЫМ net (Telegram не должен показывать оценку,
        # которая через ~1с правится по WS). Fallback по таймауту — чтобы
        # сообщение не потерялось, если филл по WS не дойдёт.
        self._close_pending: dict[int, dict] = {}

    def _notify(self, text: str) -> None:
        if self._notifier is not None:
            self._notifier.send(text)

    # ─── открытие ────────────────────────────────────────────────────────

    def on_signal(self, sig: Signal) -> int | None:
        cfg = self._cfg
        qty_step = min_qty = 0.0
        if self._client is not None:
            info = self._client.instrument(sig.symbol)
            if info:
                qty_step, min_qty = info.qty_step, info.min_order_qty
        if getattr(cfg, "risk_based_sizing", False):
            qty = position_size_by_risk(
                cfg.risk_per_trade_usd, sig.entry_ref, sig.sl_level,
                min_notional=cfg.min_position_usd,
                qty_step=qty_step, min_qty=min_qty)
        else:
            qty = position_size(cfg.position_usd, sig.entry_ref,
                                min_notional=cfg.min_position_usd,
                                qty_step=qty_step, min_qty=min_qty)
        if qty <= 0:
            log.info("skip %s %s: qty=0 (notional/min)", sig.symbol, sig.side)
            return None
        reasons = "+".join(sig.reasons)
        risk_usd = qty * abs(sig.entry_ref - sig.sl_level)

        if not cfg.trading_enabled:
            tid = self._db.insert_open(
                symbol=sig.symbol, side=sig.side, qty=qty, entry=sig.entry_ref,
                sl=sig.sl_level, tp=sig.tp_level, score=sig.score,
                reasons=reasons, mode="paper", strategy=sig.strategy,
                ts_open=self._now())
            log.info("PAPER open #%d %s %s qty=%.6f notional=$%.2f risk=$%.2f "
                     "entry=%.4f sl=%.4f tp=%.4f [%s] score=%d",
                     tid, sig.symbol, sig.side, qty, qty * sig.entry_ref, risk_usd,
                     sig.entry_ref, sig.sl_level, sig.tp_level, reasons, sig.score)
            self._notify(f"📝 PAPER open #{tid} {sig.symbol} {sig.side.upper()} "
                         f"${qty * sig.entry_ref:.0f} @{sig.entry_ref:.4f} "
                         f"SL {sig.sl_level:.4f} TP {sig.tp_level:.4f} [{reasons}]")
            return tid

        # LIVE (demo)
        cl = self._client
        side = "Buy" if sig.side == "long" else "Sell"
        cl.set_leverage(sig.symbol, cfg.max_leverage)
        link = f"scalp_{sig.symbol}_{int(self._now() * 1000)}"
        limit_price = cl.round_price(sig.symbol, sig.entry_ref)
        res = cl.place_entry(
            symbol=sig.symbol, side=side, qty=qty, order_link_id=link,
            order_type=cfg.entry_order_type, limit_price=limit_price,
            sl_price=cl.round_price(sig.symbol, sig.sl_level),
            tp_price=cl.round_price(sig.symbol, sig.tp_level))
        if not res.get("ok"):
            log.warning("LIVE entry rejected %s %s: %s", sig.symbol, side, res.get("error"))
            return None
        tid = self._db.insert_open(
            symbol=sig.symbol, side=sig.side, qty=qty, entry=limit_price,
            sl=sig.sl_level, tp=sig.tp_level, score=sig.score, reasons=reasons,
            mode="live", strategy=sig.strategy, entry_order_id=link,
            ts_open=self._now())
        # регистрируем тег входа → атрибуция филлов из приватного WS execution
        self._link2trade[link] = tid
        self._fills[tid] = {"fee": 0.0, "pnl": 0.0, "close_val": 0.0,
                            "close_qty": 0.0}
        is_market = cfg.entry_order_type == "market"
        # Уведомление об открытии шлём ТОЛЬКО после реального филла (для maker
        # post-only ордер может быть отменён/не исполнен — тогда позиции нет).
        open_text = (f"🟢 open #{tid} {sig.symbol} {sig.side.upper()} "
                     f"${qty * limit_price:.0f} @{limit_price:.4f} "
                     f"SL {sig.sl_level:.4f} TP {sig.tp_level:.4f} [{reasons}]")
        self._pending[tid] = {"link": link, "filled": is_market,
                              "ts": self._now(), "open_text": open_text}
        log.info("LIVE %s #%d %s %s qty=%.6f notional=$%.2f risk=$%.2f @%.4f "
                 "sl=%.4f tp=%.4f [%s]",
                 "MARKET" if is_market else "PLACED", tid, sig.symbol, side, qty,
                 qty * limit_price, risk_usd, limit_price, sig.sl_level,
                 sig.tp_level, reasons)
        if is_market:
            play.info("📥 [%s] МАРКЕТ-вход %s: беру рынок @%.4f (qty=%s, $%.0f) — "
                      "позиция открыта, сопровождаю", sig.symbol,
                      sig.side.upper(), limit_price, qty, qty * limit_price)
            self._notify(open_text)
        else:
            play.info("📤 [%s] ставлю maker-лимитку %s @%.4f (qty=%s, $%.0f) на "
                      "своей стороне книги — жду филл до %.0fс", sig.symbol,
                      sig.side.upper(), limit_price, qty, qty * limit_price,
                      cfg.entry_fill_timeout_sec)
            self._notify(f"⏳ #{tid} {sig.symbol} {sig.side.upper()} maker-лимитка "
                         f"@{limit_price:.4f} выставлена, жду филл")
        return tid

    # ─── сопровождение ───────────────────────────────────────────────────

    def manage(self, states: dict) -> None:
        for tr in self._db.open_trades():
            st = states.get(tr.symbol)
            snap = st.snapshot() if st else None
            price = snap.last_price if snap else None
            if tr.mode == "paper":
                self._manage_paper(tr, price, snap)
            else:
                self._manage_live(tr, price, snap)
        # досверка оценочных PnL с биржей (closedPnl публикуется с задержкой)
        try:
            self.reconcile()
        except Exception:
            log.exception("reconcile failed")

    def ingest_executions(self, rows: list[dict]) -> None:
        """Атрибуция филлов из приватного WS execution к сделкам (вызывается из
        главного треда). Матч по нашему orderLinkId (вход/выход тегаются), для
        биржевых TP/SL (пустой/чужой linkId) — по символу к открытой сделке.
        Накапливаем точные execFee/execPnl/execPrice → net = ΣexecPnl − ΣexecFee."""
        for r in rows or []:
            tid = self._link2trade.get(r.get("orderLinkId", ""))
            if tid is None:
                tid = self._open_trade_for_symbol(r.get("symbol", ""))
            if tid is None:
                continue  # филл не наш / сделка уже финализирована
            acc = self._fills.setdefault(
                tid, {"fee": 0.0, "pnl": 0.0, "close_val": 0.0, "close_qty": 0.0})
            acc["fee"] += r.get("execFee", 0.0)
            acc["pnl"] += r.get("execPnl", 0.0)
            # закрывающий филл: closedSize>0 или есть realized P&L
            if r.get("closedSize", 0.0) > 0 or r.get("execPnl", 0.0) != 0.0:
                acc["close_val"] += r.get("execPrice", 0.0) * r.get("execQty", 0.0)
                acc["close_qty"] += r.get("execQty", 0.0)

    def _open_trade_for_symbol(self, symbol: str) -> int | None:
        if not symbol:
            return None
        for tr in self._db.open_trades():
            if tr.symbol == symbol and tr.mode == "live":
                return tr.id
        return None

    def _realized_from_fills(self, tr) -> tuple[float, float | None, bool]:
        """net по WS-филлам → (net, exit_px, complete). complete=True когда
        закрывающий объём ≈ qty сделки (все филлы выхода пришли по WS).
        net = ΣexecPnl − ΣexecFee (включая комиссию входа) = Bybit closedPnl."""
        acc = self._fills.get(tr.id)
        if not acc or acc["close_qty"] <= 0:
            return (0.0, None, False)
        net = acc["pnl"] - acc["fee"]
        exit_px = acc["close_val"] / acc["close_qty"]
        complete = acc["close_qty"] >= tr.qty * 0.98
        return (net, exit_px, complete)

    def _realized_or_estimate(self, tr, exit_price: float
                              ) -> tuple[float, float, bool]:
        """net PnL → (pnl, exit_price, is_real) из приватного WS execution.
        Если филлы выхода уже пришли (обычно WS быстрее REST) — точный net и
        реальная цена выхода. Иначе предв. оценка по цене (killswitch не должен
        «ослепнуть»); сделка помечается provisional и досверяется в reconcile()
        из того же WS-леджера на следующих циклах."""
        net, exit_px, complete = self._realized_from_fills(tr)
        if complete:
            return (net, exit_px if exit_px is not None else exit_price, True)
        pnl = taker_pnl(tr.side, tr.entry, exit_price, tr.qty)
        log.warning("LIVE #%d филлы выхода ещё не пришли по WS — предв. оценка="
                    "%.4f (досверю)", tr.id, pnl)
        return (pnl, exit_price, False)

    def _forget_trade(self, tid: int) -> None:
        """Снять трекинг закрытой сделки (после финализации реальным net)."""
        self._fills.pop(tid, None)
        self._close_pending.pop(tid, None)
        for link in [k for k, v in self._link2trade.items() if v == tid]:
            self._link2trade.pop(link, None)

    def _on_close(self, tr, pnl: float, reason: str, label: str,
                  is_real: bool) -> None:
        """Плейбук-нарратив + Telegram при закрытии. Если net реальный (филлы
        по WS уже пришли) — шлём сразу. Иначе откладываем уведомление до
        reconcile() (придёт с реальным net, без устаревшей оценки в Telegram)."""
        res = "профит" if (pnl or 0.0) >= 0 else "убыток"
        play.info("🏁 [%s] закрыл #%d %s: %s — %s, pnl=$%.2f", tr.symbol, tr.id,
                  tr.side.upper(), _CLOSE_RU.get(reason, reason), res, pnl or 0.0)
        if is_real:
            self._send_close_msg(tr.id, tr.symbol, pnl or 0.0, label)
        else:
            self._close_pending[tr.id] = {"ts": self._now(), "label": label,
                                          "symbol": tr.symbol}

    def _send_close_msg(self, tid: int, symbol: str, pnl: float, label: str,
                        approx: bool = False) -> None:
        emoji = "✅" if pnl >= 0 else "🔴"
        mark = "≈" if approx else ""
        self._notify(f"{emoji} close #{tid} {symbol} pnl={mark}${pnl:.2f} ({label})")

    def reconcile(self) -> None:
        """Досверка предварительных (оценочных) PnL по WS-леджеру: когда филлы
        выхода доедут (обычно ≤1с), переписываем БД реальным net, чтобы она
        сходилась с выпиской 1:1 (stats-collection.mdc — БД = ground truth), и
        дошлём отложенное close-уведомление с реальным net."""
        now = self._now()
        horizon = now - 600.0  # сверяем закрытия за последние 10 мин
        fallback = getattr(self._cfg, "close_notify_fallback_sec", 10.0)
        for tr in self._db.provisional_closed_since(horizon):
            net, exit_px, complete = self._realized_from_fills(tr)
            if complete:
                self._db.finalize_pnl(tr.id, pnl_usd=net, exit_price=exit_px)
                log.info("reconcile #%d %s: оценка→реальный net %.4f→%.4f (WS)",
                         tr.id, tr.symbol, tr.pnl_usd or 0.0, net)
                pend = self._close_pending.pop(tr.id, None)
                if pend is not None:  # отложенное уведомление → шлём реальный net
                    self._send_close_msg(tr.id, pend["symbol"], net, pend["label"])
                self._forget_trade(tr.id)
                continue
            # фолбэк: филлы по WS не дошли слишком долго → шлём оценку с пометкой,
            # чтобы пользователь не остался без уведомления о закрытии
            pend = self._close_pending.get(tr.id)
            if pend is not None and now - pend["ts"] > fallback:
                self._send_close_msg(tr.id, pend["symbol"], tr.pnl_usd or 0.0,
                                     pend["label"], approx=True)
                self._close_pending.pop(tr.id, None)
                log.warning("reconcile #%d %s: филлы по WS не дошли за %.0fс — "
                            "уведомление с оценкой ≈$%.2f", tr.id, tr.symbol,
                            fallback, tr.pnl_usd or 0.0)

    def _strategy_exit(self, tr, snap) -> tuple[str, float] | None:
        """Дискреционный выход СТРАТЕГИИ-владельца сделки (та же, что открыла).

        Универсальные выходы (TP/SL/тайм-стоп) живут в _manage_*. Здесь —
        только стратегийная логика (для sweep_fade: fee-aware профит-лок по
        развороту ленты). Возвращает (close_reason, exit_price) или None."""
        strat = self._strategies.get(getattr(tr, "strategy", ""))
        if strat is None or snap is None:
            return None
        try:
            return strat.should_exit(tr, snap, self._now())
        except Exception:
            log.exception("should_exit %s #%d failed", tr.strategy, tr.id)
            return None

    def _manage_paper(self, tr, price: float | None, snap=None) -> None:
        if price is None:
            return
        hit_tp = price >= tr.tp if tr.side == "long" else price <= tr.tp
        hit_sl = price <= tr.sl if tr.side == "long" else price >= tr.sl
        reason = exit_px = None
        strat_exit = self._strategy_exit(tr, snap)
        if hit_sl:
            reason, exit_px = "sl", tr.sl
        elif hit_tp:
            reason, exit_px = "tp", tr.tp
        elif strat_exit is not None:
            reason, exit_px = strat_exit
        # v0.9.5: time_stop удалён — победителю даём бежать (Философия B);
        # стоячую/убыточную сделку режут flow_scratch + биржевой SL/TP.
        if reason is None:
            return
        pnl, fees = paper_pnl(tr.side, tr.entry, exit_px, tr.qty)
        self._db.mark_closed(tr.id, exit_price=exit_px, pnl_usd=pnl, fees_usd=fees,
                             close_reason=reason, ts_close=self._now())
        log.info("PAPER close #%d %s %s @%.4f pnl=%.4f fees=%.4f (%s)",
                 tr.id, tr.symbol, tr.side, exit_px, pnl, fees, reason)
        emoji = "✅" if pnl >= 0 else "🔴"
        self._notify(f"{emoji} PAPER close #{tr.id} {tr.symbol} pnl=${pnl:.2f} "
                     f"fees=${fees:.2f} ({reason})")

    def _manage_live(self, tr, price: float | None, snap=None) -> None:
        cl = self._client
        pend = self._pending.get(tr.id)
        # 1) ожидание заполнения post-only входа
        if pend and not pend["filled"]:
            status = cl.order_status(tr.symbol, pend["link"])
            if status in ("Filled", "PartiallyFilled"):
                pend["filled"] = True
                pend["ts"] = self._now()
                log.info("LIVE #%d %s — позиция открыта", tr.id, status)
                play.info("✅ [%s] филл #%d %s @%.4f — позиция открыта, "
                          "слежу за TP %.4f / SL %.4f / разворотом ленты",
                          tr.symbol, tr.id, tr.side.upper(), tr.entry, tr.tp,
                          tr.sl)
                self._notify(pend.get("open_text", f"🟢 open #{tr.id} {tr.symbol}"))
                return
            if status in ("Cancelled", "Rejected", "Deactivated"):
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason=f"entry_{status}",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                self._forget_trade(tr.id)
                log.info("LIVE #%d entry %s — не открылись", tr.id, status)
                play.info("🚫 [%s] вход #%d не состоялся (%s) — позиции нет, "
                          "возвращаюсь к поиску", tr.symbol, tr.id, status)
                return
            if self._now() - pend["ts"] > self._cfg.entry_fill_timeout_sec:
                cl.cancel_order(tr.symbol, pend["link"])
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason="entry_timeout",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                self._forget_trade(tr.id)
                log.info("LIVE #%d entry timeout — отменён", tr.id)
                play.info("⌛ [%s] maker-лимитка #%d не исполнилась за %.0fс — "
                          "снимаю ордер (цена ушла от мейкера)", tr.symbol, tr.id,
                          self._cfg.entry_fill_timeout_sec)
            return
        # 2) активная позиция
        pos = cl.get_position(tr.symbol)
        if pos is None:
            return  # transient — не трогаем
        if pos.size <= 0:
            # биржевой TP/SL: наши филлы выхода приходят по WS execution (матч
            # по символу к открытой сделке внутри ingest_executions)
            pnl, exitp, is_real = self._realized_or_estimate(
                tr, pos.mark_price or tr.entry)
            reason = bracket_exit_reason(tr.side, tr.entry, exitp)
            self._db.mark_closed(tr.id, exit_price=exitp, pnl_usd=pnl,
                                 fees_usd=0.0, close_reason=reason,
                                 ts_close=self._now(), provisional=not is_real)
            if is_real:
                self._forget_trade(tr.id)
            self._pending.pop(tr.id, None)
            self._hold_log.pop(tr.id, None)
            log.info("LIVE close #%d %s pnl=%.4f (биржа %s)", tr.id, tr.symbol,
                     pnl or 0.0, reason)
            self._on_close(tr, pnl, reason, _CLOSE_RU.get(reason, reason), is_real)
            return
        strat_exit = self._strategy_exit(tr, snap)
        if strat_exit is not None:  # v0.9.5: только дискреционный выход; TP/SL — биржа
            close_reason = strat_exit[0]
            side = "Buy" if tr.side == "long" else "Sell"
            close_link = f"scalp_{close_reason}_{tr.id}"
            self._link2trade[close_link] = tr.id  # филлы выхода → эта сделка
            cl.close_market(tr.symbol, side, pos.size, close_link)
            pnl, exitp, is_real = self._realized_or_estimate(
                tr, pos.mark_price or tr.entry)
            self._db.mark_closed(tr.id, exit_price=exitp, pnl_usd=pnl,
                                 fees_usd=0.0, close_reason=close_reason,
                                 ts_close=self._now(), provisional=not is_real)
            if is_real:
                self._forget_trade(tr.id)
            self._pending.pop(tr.id, None)
            self._hold_log.pop(tr.id, None)
            log.info("LIVE close #%d %s pnl=%.4f (%s)", tr.id, tr.symbol,
                     pnl or 0.0, close_reason)
            self._on_close(tr, pnl, close_reason, close_reason, is_real)
            return
        # держим позицию — троттлим лог дистанций до TP/SL и возраста
        iv = getattr(self._cfg, "narrate_interval_sec", 15.0)
        if price is not None and self._now() - self._hold_log.get(tr.id, 0.0) >= iv:
            self._hold_log[tr.id] = self._now()
            age = self._now() - tr.ts_open
            to_tp = (tr.tp - price) if tr.side == "long" else (price - tr.tp)
            to_sl = (price - tr.sl) if tr.side == "long" else (tr.sl - price)
            play.info("⏱ [%s] держу #%d %s %.0fс | цена %.4f | до TP %+.4f, "
                      "до SL %+.4f", tr.symbol, tr.id, tr.side.upper(), age,
                      price, to_tp, to_sl)
