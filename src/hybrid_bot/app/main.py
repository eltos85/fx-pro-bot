"""Цикл hybrid_bot: держим тренд, закрываем на пороге, сразу заходим обратно.

Пять правил стратегии (STRATEGY_HYBRID.md §17.4) один в один:

1. держим покупку, пока SMA20 > SMA50 на 4h;
2. ведём среднюю цену входа своей позиции;
3. закрываем СВОЙ объём целиком, когда цена ушла от средней на порог;
4. сразу заходим обратно тем же нотионалом, если тренд ещё вверх;
5. повторяем.

Чужую позицию на общем счёте не трогаем: если на символе есть лот, а в нашей
БД его нет — символ пропускается. Стоп/тейк на позицию не ставится никогда
(см. docstring клиента).

Пока HYBRID_TRADING_ENABLED=false бот считает и пишет сделки с mode=paper, но
ордера не отправляет — порог из §17.6 должен быть согласован до торговли.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from hybrid_bot.client import HybridClient
from hybrid_bot.db import HybridDB
from hybrid_bot.settings import HybridSettings, load_settings
from hybrid_bot.signals import distance_pct, fix_price, should_fix, trend_long
from hybrid_bot.telegram import TelegramNotifier

log = logging.getLogger("hybrid_bot")

STRATEGY = "hybrid_fix_from_avg"


def _link(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def plan(*, want: int | None, owned: dict | None, last_price: float,
         broker_size: float | None, threshold_pct: float) -> dict:
    """Что делать с символом. Без обращений к сети — вся логика решения здесь.

    ``broker_size`` = None означает, что позицию на бирже не проверяли
    (режим наблюдения): своих ордеров нет, сверять нечего.
    """
    if last_price <= 0:
        return {"action": "no_price"}
    if broker_size is not None:
        if broker_size > 0 and owned is None:
            return {"action": "skip_foreign", "size": broker_size}
        if broker_size == 0 and owned is not None:
            return {"action": "resync"}
    if want is None:
        return {"action": "no_data"}
    if owned is None:
        return {"action": "open"} if want == 1 else {"action": "stay_out"}
    if want == 0:
        return {"action": "exit"}
    if should_fix(last_price, owned["avg_entry"], threshold_pct):
        return {"action": "fix",
                "price": fix_price(owned["avg_entry"], threshold_pct)}
    return {"action": "hold",
            "distance_pct": distance_pct(last_price, owned["avg_entry"])}


def bet_size(*, position_usd: float, virtual_capital: float,
             open_notional: float) -> float:
    """Размер ставки: не больше максимума и не больше остатка капитала.

    Бот работает так, будто на счёте ровно ``virtual_capital``, сколько бы там
    ни лежало на самом деле.
    """
    left = virtual_capital - open_notional
    return max(0.0, min(position_usd, left))


def _money(pos: dict, exit_px: float, exit_fee: float = 0.0) -> float:
    """Чистые деньги сделки: ход цены минус комиссии обеих ног."""
    sign = 1.0 if pos["side"] == "Buy" else -1.0
    fees = float(pos.get("entry_fee") or 0.0) + exit_fee
    return sign * (exit_px - pos["avg_entry"]) * pos["qty"] - fees


def _executed(cfg: HybridSettings, client: HybridClient, link_id: str,
              symbol: str, qty: float, price: float) -> tuple[float, float,
                                                              float]:
    """Чем сделка обошлась на самом деле: (объём, цена, комиссия).

    В торговле — из истории исполнений: цена тикера и нулевая комиссия завышают
    результат, а канон требует сходимости учёта с биржей 1:1 (§8.4). В режиме
    наблюдения фактических данных нет, поэтому комиссия оценивается по ставке
    taker — иначе бумажные цифры были бы лучше живых.
    """
    if not cfg.trading_enabled:
        return qty, price, qty * price * cfg.taker_fee
    fill = client.fill_of(link_id)
    if fill is None:
        log.warning("%s исполнение по %s не прочитано — считаем по цене %.4f "
                    "без комиссии", symbol, link_id, price)
        return qty, price, 0.0
    return fill.qty, fill.price, fill.fee


def _open(cfg: HybridSettings, client: HybridClient, db: HybridDB,
          tg: TelegramNotifier, symbol: str, price: float, *,
          fixations: int = 0, note: str = "") -> bool:
    """Вход на размер ставки. Возвращает True, если позиция открыта."""
    notional = bet_size(position_usd=cfg.position_usd,
                        virtual_capital=cfg.virtual_capital,
                        open_notional=db.open_notional(exclude=symbol))
    if notional < cfg.min_notional_usd:
        log.info("%s ставка $%.2f меньше минимума (капитал $%.0f уже занят) — "
                 "пропуск", symbol, notional, cfg.virtual_capital)
        return False
    qty = float(client.fmt_qty(symbol, notional / price))
    info = client.instrument(symbol)
    if qty <= 0 or (info and qty < info.min_order_qty):
        log.info("%s объём %.6f меньше минимального — пропуск", symbol, qty)
        return False
    lid = _link(cfg.link_prefix)
    if cfg.trading_enabled:
        client.set_leverage(symbol, cfg.leverage)
        res = client.market(symbol=symbol, side="Buy", qty=qty,
                            order_link_id=lid)
        if not res.get("ok"):
            log.warning("%s вход отклонён: %s", symbol, res.get("error"))
            return False
    qty, fill_px, fee = _executed(cfg, client, lid, symbol, qty, price)
    db.open_pos(symbol, "Buy", qty, fill_px, lid, fixations=fixations,
                entry_fee=fee)
    log.info("%s вход %.6f по %.4f на $%.0f, комиссия $%.4f%s", symbol, qty,
             fill_px, qty * fill_px, fee, f" ({note})" if note else "")
    if not note:
        tg.send(f"🟢 {symbol}: купили {qty:.4f} по {fill_px:.2f} "
                f"на ${qty * fill_px:,.0f}, тренд 4h вверх{_paper(cfg)}")
    return True


def _close(cfg: HybridSettings, client: HybridClient, db: HybridDB,
           symbol: str, pos: dict, price: float,
           reason: str) -> tuple[float, float] | None:
    """Закрытие своего объёма reduce-only. Чужой лот не затрагивается.

    Возвращает (цена исполнения, чистые деньги) или None, если биржа отказала.
    """
    lid = _link(cfg.link_prefix)
    if cfg.trading_enabled:
        res = client.market(symbol=symbol, side="Sell", qty=pos["qty"],
                            order_link_id=lid, reduce_only=True)
        if not res.get("ok"):
            log.warning("%s выход отклонён: %s", symbol, res.get("error"))
            return None
    _, fill_px, fee = _executed(cfg, client, lid, symbol, pos["qty"], price)
    db.record_closed(pos, exit_px=fill_px, reason=reason,
                     mode=cfg.trade_mode, strategy=STRATEGY, exit_fee=fee)
    db.drop_pos(symbol)
    return fill_px, _money(pos, fill_px, fee)


def _paper(cfg: HybridSettings) -> str:
    return "" if cfg.trading_enabled else " (наблюдение, без ордеров)"


def _cycle(cfg: HybridSettings, client: HybridClient, db: HybridDB,
           tg: TelegramNotifier) -> None:
    for symbol in cfg.symbol_list:
        bars = client.closed_klines(symbol, cfg.interval, limit=250)
        closes = [c for _, c in bars]
        want = trend_long(closes)
        price = client.last_price(symbol) or (closes[-1] if closes else 0.0)
        owned = db.owned(symbol)

        broker_size: float | None = None
        if cfg.trading_enabled:
            broker = client.get_position(symbol)
            if broker is None:
                log.warning("%s биржа не ответила по позиции — пропуск", symbol)
                continue
            broker_size = broker.size

        act = plan(want=want, owned=owned, last_price=price,
                   broker_size=broker_size,
                   threshold_pct=cfg.fix_threshold_pct)
        name = act["action"]

        if name == "no_price":
            log.warning("%s нет цены — пропуск", symbol)
        elif name == "no_data":
            log.info("%s мало баров (%d)", symbol, len(closes))
        elif name == "skip_foreign":
            log.info("%s на бирже чужой лот %.6f — не трогаем", symbol,
                     act["size"])
        elif name == "resync":
            # Позицию закрыл кто-то другой: фиксируем факт по последней цене.
            db.record_closed(owned, exit_px=price, reason="broker_flat",
                             mode=cfg.trade_mode, strategy=STRATEGY)
            db.drop_pos(symbol)
            log.warning("%s позиции на бирже нет — записали закрытие по %.4f",
                        symbol, price)
            tg.send(f"⚠️ {symbol}: позицию закрыл кто-то другой, "
                    f"по {price:.2f} вышло {_money(owned, price):+,.2f} $")
        elif name == "stay_out":
            log.info("%s тренд вниз, вне рынка", symbol)
        elif name == "open":
            _open(cfg, client, db, tg, symbol, price)
        elif name == "exit":
            done = _close(cfg, client, db, symbol, owned, price, "trend_flat")
            if done:
                exit_px, money = done
                log.info("%s выход по тренду %.4f, деньги %+.2f", symbol,
                         exit_px, money)
                tg.send(f"🔴 {symbol}: тренд развернулся, закрыли "
                        f"{owned['qty']:.4f} по {exit_px:.2f}, "
                        f"{money:+,.2f} ${_paper(cfg)}")
        elif name == "fix":
            avg_was = owned["avg_entry"]
            fixations = int(owned["fixations"]) + 1
            done = _close(cfg, client, db, symbol, owned, price,
                          "fix_threshold")
            if not done:
                continue
            # Считаем по цене исполнения, а не по уровню порога: рыночный ордер
            # исполняется хуже уровня, и эта разница — измеряемая величина.
            exit_px, money = done
            dist = distance_pct(exit_px, avg_was)
            log.info("%s фиксация №%d: %.4f по %.4f (+%.2f%% от %.4f), "
                     "деньги %+.2f", symbol, fixations, owned["qty"], exit_px,
                     dist, avg_was, money)
            back = ""
            if want == 1 and _open(cfg, client, db, tg, symbol, exit_px,
                                   fixations=fixations, note="обратный вход"):
                new = db.owned(symbol)
                if new:
                    back = (f", сразу зашли обратно {new['qty']:.4f} "
                            f"по {new['avg_entry']:.2f}")
            tg.send(f"💰 {symbol}: закрыли весь свой объём {owned['qty']:.4f} "
                    f"по {exit_px:.2f} — это {dist:+.2f}% от средней "
                    f"{avg_was:.2f}, деньги {money:+,.2f} ${back}{_paper(cfg)}")
        else:
            log.info("%s держим, до порога %.2f%% из %.2f%%", symbol,
                     act.get("distance_pct", 0.0), cfg.fix_threshold_pct)


def run() -> None:
    cfg = load_settings()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log.info("старт hybrid_bot: символы=%s порог=+%.2f%% ставка=$%.0f "
             "капитал=$%.0f торговля=%s demo=%s", ",".join(cfg.symbol_list),
             cfg.fix_threshold_pct, cfg.position_usd, cfg.virtual_capital,
             cfg.trading_enabled, cfg.bybit_demo)
    if cfg.trading_enabled and not (cfg.bybit_api_key and cfg.bybit_api_secret):
        log.error("торговля включена, но нет ключей API — выходим")
        return
    os.makedirs(cfg.data_dir, exist_ok=True)
    db = HybridDB(cfg.db_path)
    client = HybridClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                          demo=cfg.bybit_demo, category=cfg.bybit_category)
    tg = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                          enabled=cfg.telegram_enabled,
                          prefix=cfg.telegram_prefix)
    while True:
        try:
            _cycle(cfg, client, db, tg)
        except Exception:
            log.exception("цикл")
        time.sleep(max(30, cfg.poll_sec))


if __name__ == "__main__":
    run()
