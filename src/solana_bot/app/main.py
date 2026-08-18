"""Цикл solana-bot: скринер щитков → опциональный Jupiter swap.

Без ключа или при trading_enabled=false только логирует кандидатов.
"""

from __future__ import annotations

import logging
import os
import time

from solana_bot import jupiter, wallet
from solana_bot.db import SolanaDB
from solana_bot.screener import token_price_usd, trending_shields
from solana_bot.settings import load_settings
from solana_bot.signals import WSOL, exit_reason, should_enter, universe_ok
from solana_bot.telegram import (
    TelegramNotifier,
    fmt_candidate,
    fmt_enter,
    fmt_exit,
    fmt_start,
    should_alert,
)

log = logging.getLogger("solana_bot")

_LAMPORTS = 1_000_000_000


def _manage(cfg, db: SolanaDB, tg: TelegramNotifier) -> None:
    for pos in db.all_owned():
        px = token_price_usd(pos["mint"])
        if px <= 0:
            continue
        why = exit_reason(pos["entry"], px, tp_pct=cfg.tp_pct,
                          cap_pct=cfg.cap_pct, sl_pct=cfg.sl_pct)
        if why is None:
            continue
        if cfg.trading_enabled and cfg.private_key and wallet.available():
            pk = wallet.pubkey(cfg.private_key)
            if pk:
                # qty в БД — оценка в token units; выход через SOL по котировке.
                # Для щитка продаём «весь остаток» через order amount из quote
                # не знаем decimals надёжно — продаём по текущей оценке usd→sol.
                # Практично: повторный order token→SOL на размер из last order
                # здесь не храним raw amount. Закрываем учёт + пытаемся свап
                # только если qty>0 и есть цена: amount в raw неизвестен →
                # сканируем и пишем close; живой выход — когда trading и qty
                # сохранён как raw (см. _enter).
                raw = int(pos["qty"])
                if raw > 0:
                    od = jupiter.order(
                        input_mint=pos["mint"], output_mint=WSOL,
                        amount=raw, taker=pk,
                        slippage_bps=cfg.slippage_bps,
                        api_key=cfg.jupiter_api_key)
                    if od:
                        signed = wallet.sign_order_tx(
                            cfg.private_key, od["transaction"])
                        if signed:
                            res = jupiter.execute(
                                signed_tx_b64=signed,
                                request_id=od["requestId"],
                                api_key=cfg.jupiter_api_key)
                            if not res.get("ok"):
                                log.warning("%s выход отклонён: %s",
                                            pos["symbol"], res.get("error"))
                                continue
        db.close_pos(pos["mint"], px, why)
        log.info("%s выход %s px=%.8f", pos["symbol"], why, px)
        tg.send(fmt_exit(symbol=pos["symbol"], entry=pos["entry"],
                         exit_px=px, reason=why))


def _enter(cfg, db: SolanaDB, mint: str, symbol: str, px: float,
           tg: TelegramNotifier) -> None:
    pk = wallet.pubkey(cfg.private_key) if cfg.private_key else None
    if not pk:
        log.error("нет pubkey — свап невозможен")
        return
    lamports = int(min(cfg.size_sol, cfg.max_size_sol) * _LAMPORTS)
    if lamports <= 0:
        return
    od = jupiter.order(
        input_mint=WSOL, output_mint=mint, amount=lamports, taker=pk,
        slippage_bps=cfg.slippage_bps, api_key=cfg.jupiter_api_key)
    if not od:
        return
    signed = wallet.sign_order_tx(cfg.private_key, od["transaction"])
    if not signed:
        return
    res = jupiter.execute(signed_tx_b64=signed, request_id=od["requestId"],
                          api_key=cfg.jupiter_api_key)
    if not res.get("ok"):
        log.warning("%s вход отклонён: %s", symbol, res.get("error"))
        return
    out_raw = int((res.get("result") or {}).get("outputAmountResult")
                  or od.get("outAmount") or 0)
    db.open_pos(mint, symbol, float(out_raw), px)
    log.info("%s вход raw=%s px=%.8f sol=%.4f", symbol, out_raw, px, cfg.size_sol)
    tg.send(fmt_enter(symbol=symbol, px=px, size_sol=cfg.size_sol))


def _cycle(cfg, db: SolanaDB, tg: TelegramNotifier,
           alerted: dict[str, float]) -> None:
    _manage(cfg, db, tg)
    shields = trending_shields()
    log.info("цикл open=%d seen=%d trading=%s",
             db.open_count(), len(shields), cfg.trading_enabled)
    if db.open_count() >= cfg.max_open:
        return
    for s in shields:
        if not universe_ok(s, vol_lo=cfg.volume_m5_usd, liq_lo=cfg.min_liquidity_usd,
                           age_lo=cfg.min_age_sec, skip=cfg.skip_set):
            continue
        if not should_enter(s, move_min_pct=cfg.move_m5_pct):
            continue
        if db.owned(s.mint) is not None:
            continue
        log.info("кандидат %s vol5=%.0f move=%.1f%% liq=%.0f px=%.8f",
                 s.symbol, s.volume_m5, s.move_m5_pct, s.liquidity_usd, s.price_usd)
        if not cfg.trading_enabled:
            if should_alert(alerted, s.mint, time.time(),
                            cfg.telegram_cooldown_sec):
                tg.send(fmt_candidate(
                    symbol=s.symbol, mint=s.mint, volume_m5=s.volume_m5,
                    move_m5_pct=s.move_m5_pct, liquidity_usd=s.liquidity_usd,
                    price_usd=s.price_usd))
            continue
        if not cfg.private_key or not wallet.available():
            log.info("trading выкл или нет solders/ключа — только скан")
            continue
        _enter(cfg, db, s.mint, s.symbol, s.price_usd, tg)
        return


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("SOLANA_LOG_LEVEL") or "INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = load_settings()
    log.info("старт solana trading=%s vol5>=$%.0f tp=%.1f%% cap=%.1f%% sl=%.1f%%",
             cfg.trading_enabled, cfg.volume_m5_usd, cfg.tp_pct, cfg.cap_pct,
             cfg.sl_pct)
    os.makedirs(cfg.data_dir, exist_ok=True)
    db = SolanaDB(cfg.db_path)
    if cfg.trading_enabled and not cfg.private_key:
        log.warning("SOLANA_TRADING_ENABLED но нет ключа — скан")
        cfg.trading_enabled = False
    if cfg.trading_enabled and not wallet.available():
        log.warning("нет solders — скан")
        cfg.trading_enabled = False
    tg = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                          enabled=cfg.telegram_enabled)
    log.info("telegram %s", "on" if tg.active else "off")
    tg.send(fmt_start(trading=cfg.trading_enabled))
    alerted: dict[str, float] = {}
    while True:
        try:
            _cycle(cfg, db, tg, alerted)
        except Exception:
            log.exception("цикл")
        time.sleep(max(15, cfg.poll_sec))


if __name__ == "__main__":
    run()
