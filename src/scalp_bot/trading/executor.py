"""Исполнитель сделок scalp_bot.

Два режима (settings.trading_enabled):
- PAPER  (False): ордера НЕ ставятся; позиция симулируется на live-цене
  из WS, TP/SL/тайм-стоп считаются локально, net считается с учётом
  модельных комиссий. Для shadow-валидации без риска.
- LIVE   (True, на Bybit DEMO): post-only LIMIT вход с биржевыми SL/TP,
  reduce-only MARKET выход по тайм-стопу.

Размер позиции — фикс-риск $/сделку (Van Tharp): qty = risk_usd / |entry−SL|.

Комиссии Bybit linear (https://www.bybit.com/en/help-center/article/Trading-Fee-Structure):
maker 0.02%, taker 0.055%. PAPER моделирует вход maker + выход taker.
"""
from __future__ import annotations

import logging
import time

from scalp_bot.analysis.signals import Signal

log = logging.getLogger("scalp_bot.exec")

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055


def position_size(position_usd: float, entry: float, *, min_notional: float = 0.0,
                  qty_step: float = 0.0, min_qty: float = 0.0) -> float:
    """qty из целевого notional ($). Пол min_notional (мелкий лот = комиссия
    съедает прибыль). Округление вниз под qty_step, отсев < min_qty биржи."""
    if entry <= 0 or position_usd <= 0:
        return 0.0
    notional = max(position_usd, min_notional)
    qty = notional / entry
    if qty_step > 0:
        import math
        qty = math.floor(qty / qty_step) * qty_step
    if min_qty > 0 and qty < min_qty:
        # биржевой минимум выше нашего лота — берём биржевой минимум
        qty = min_qty
    return qty


def paper_pnl(side: str, entry: float, exit_price: float, qty: float) -> tuple[float, float]:
    """(net_pnl, fees) для PAPER: вход maker, выход taker."""
    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    fees = qty * (entry * MAKER_FEE + exit_price * TAKER_FEE)
    return (gross - fees, fees)


class Executor:
    def __init__(self, db, settings, client=None, *, notifier=None,
                 now=time.time) -> None:
        self._db = db
        self._cfg = settings
        self._client = client
        self._notifier = notifier
        self._now = now
        # in-memory трекинг live-входов: trade_id -> {link, filled, ts}
        self._pending: dict[int, dict] = {}

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
                reasons=reasons, mode="paper", ts_open=self._now())
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
            mode="live", entry_order_id=link, ts_open=self._now())
        self._pending[tid] = {"link": link, "filled": cfg.entry_order_type == "market",
                              "ts": self._now()}
        log.info("LIVE open #%d %s %s qty=%.6f notional=$%.2f risk=$%.2f @%.4f "
                 "sl=%.4f tp=%.4f [%s]", tid, sig.symbol, side, qty,
                 qty * limit_price, risk_usd, limit_price, sig.sl_level,
                 sig.tp_level, reasons)
        self._notify(f"🟢 open #{tid} {sig.symbol} {sig.side.upper()} "
                     f"${qty * limit_price:.0f} @{limit_price:.4f} "
                     f"SL {sig.sl_level:.4f} TP {sig.tp_level:.4f} [{reasons}]")
        return tid

    # ─── сопровождение ───────────────────────────────────────────────────

    def manage(self, states: dict) -> None:
        for tr in self._db.open_trades():
            st = states.get(tr.symbol)
            snap = st.snapshot() if st else None
            price = snap.last_price if snap else None
            if tr.mode == "paper":
                self._manage_paper(tr, price)
            else:
                self._manage_live(tr, price)

    def _manage_paper(self, tr, price: float | None) -> None:
        if price is None:
            return
        age = self._now() - tr.ts_open
        hit_tp = price >= tr.tp if tr.side == "long" else price <= tr.tp
        hit_sl = price <= tr.sl if tr.side == "long" else price >= tr.sl
        reason = exit_px = None
        if hit_sl:
            reason, exit_px = "sl", tr.sl
        elif hit_tp:
            reason, exit_px = "tp", tr.tp
        elif age >= self._cfg.time_stop_sec:
            reason, exit_px = "time_stop", price
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

    def _manage_live(self, tr, price: float | None) -> None:
        cl = self._client
        pend = self._pending.get(tr.id)
        # 1) ожидание заполнения post-only входа
        if pend and not pend["filled"]:
            status = cl.order_status(tr.symbol, pend["link"])
            if status == "Filled":
                pend["filled"] = True
                pend["ts"] = self._now()
                return
            if status in ("Cancelled", "Rejected", "Deactivated"):
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason=f"entry_{status}",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                log.info("LIVE #%d entry %s — не открылись", tr.id, status)
                return
            if self._now() - pend["ts"] > self._cfg.entry_fill_timeout_sec:
                cl.cancel_order(tr.symbol, pend["link"])
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason="entry_timeout",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                log.info("LIVE #%d entry timeout — отменён", tr.id)
            return
        # 2) активная позиция
        pos = cl.get_position(tr.symbol)
        if pos is None:
            return  # transient — не трогаем
        if pos.size <= 0:
            pnl = cl.last_closed_pnl(tr.symbol, "scalp_")
            self._db.mark_closed(tr.id, exit_price=pos.mark_price or tr.entry,
                                 pnl_usd=pnl or 0.0, fees_usd=0.0,
                                 close_reason="tp_sl", ts_close=self._now())
            self._pending.pop(tr.id, None)
            log.info("LIVE close #%d %s pnl=%.4f (биржа TP/SL)", tr.id, tr.symbol,
                     pnl or 0.0)
            emoji = "✅" if (pnl or 0.0) >= 0 else "🔴"
            self._notify(f"{emoji} close #{tr.id} {tr.symbol} pnl=${pnl or 0.0:.2f} (TP/SL)")
            return
        if self._now() - tr.ts_open >= self._cfg.time_stop_sec:
            side = "Buy" if tr.side == "long" else "Sell"
            cl.close_market(tr.symbol, side, pos.size, f"scalp_ts_{tr.id}")
            pnl = cl.last_closed_pnl(tr.symbol, "scalp_")
            self._db.mark_closed(tr.id, exit_price=pos.mark_price or tr.entry,
                                 pnl_usd=pnl or 0.0, fees_usd=0.0,
                                 close_reason="time_stop", ts_close=self._now())
            self._pending.pop(tr.id, None)
            log.info("LIVE close #%d %s pnl=%.4f (time_stop)", tr.id, tr.symbol,
                     pnl or 0.0)
            emoji = "✅" if (pnl or 0.0) >= 0 else "🔴"
            self._notify(f"{emoji} close #{tr.id} {tr.symbol} pnl=${pnl or 0.0:.2f} (time_stop)")
