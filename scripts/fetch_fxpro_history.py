#!/usr/bin/env python3
"""Скачать историческую M5-историю за N дней через FxPro cTrader Open API.

Делает пагинированные запросы окнами по ~20 дней (~5760 M5-баров на запрос —
в пределах лимита cTrader, обычно до 10k за ответ). Сохраняет CSV по
символу в data/fxpro_klines/<symbol>_M5.csv в формате, совместимом с
backtest-фреймворком.

Использование:
    # Локально, если есть доступ к токенам:
    python3 -m scripts.fetch_fxpro_history --days 90

    # Через контейнер (VPS/локально):
    docker exec fx-pro-bot-advisor-1 python3 -m scripts.fetch_fxpro_history \\
        --days 90 --out /app/data/fxpro_klines

Формат CSV (совпадает с bybit-klines):
    timestamp,open,high,low,close,volume
    1735689600000,1.08572,1.08584,1.08569,1.08581,142.0
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fx_pro_bot.config.settings import DEFAULT_SYMBOLS, Settings
from fx_pro_bot.market_data.ctrader_feed import TRENDBAR_SCALE
from fx_pro_bot.trading.auth import TokenStore, ensure_valid_token
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.symbols import SymbolCache
from fx_pro_bot.trading.executor import TradeExecutor

log = logging.getLogger("fetch_fxpro_history")

DEFAULT_INTERVAL_MIN = 5
# 20 дней × 24h × 12 баров/час = 5760 баров — в пределах cTrader лимита
# для M5. Для M1 нужно меньшее окно (~3-4 дня), чтобы не упереться в лимит.
WINDOW_DAYS = 20
SLEEP_BETWEEN_REQUESTS_SEC = 0.25  # throttle: ~4 req/s


def _default_window_days(interval_min: int) -> int:
    """Подбирает размер окна так, чтобы было ~5000 баров на запрос
    (запас под cTrader лимит ~10000)."""
    if interval_min <= 0:
        return WINDOW_DAYS
    bars_per_day = 1440 // interval_min
    if bars_per_day <= 0:
        return WINDOW_DAYS
    return max(1, 5000 // bars_per_day)


@dataclass(frozen=True, slots=True)
class FetchStats:
    symbol: str
    bars: int
    windows: int
    gaps_filled: int
    elapsed_sec: float


def _decode_bar(tb) -> tuple[int, float, float, float, float, float]:
    """raw ProtoOATrendbar → (ts_ms, open, high, low, close, volume)."""
    scale = TRENDBAR_SCALE
    low_abs = tb.low
    low = low_abs / scale
    open_ = (low_abs + tb.deltaOpen) / scale
    high = (low_abs + tb.deltaHigh) / scale
    close = (low_abs + tb.deltaClose) / scale
    ts_ms = int(tb.utcTimestampInMinutes) * 60 * 1000
    volume = float(tb.volume)
    return ts_ms, open_, high, low, close, volume


def _fetch_symbol(
    client: CTraderClient,
    symbol_cache: SymbolCache,
    yf_symbol: str,
    from_ms: int,
    to_ms: int,
    interval_min: int,
    window_days: int = WINDOW_DAYS,
) -> list[tuple[int, float, float, float, float, float]]:
    """Пагинированный fetch одного символа. Возвращает отсортированный дедуп-список."""
    sym = symbol_cache.resolve_yfinance(yf_symbol)
    if sym is None:
        log.warning("  %s не найден в cTrader каталоге, пропускаем", yf_symbol)
        return []

    window_ms = window_days * 86400 * 1000
    seen: dict[int, tuple[int, float, float, float, float, float]] = {}
    windows = 0

    cur_from = from_ms
    while cur_from < to_ms:
        cur_to = min(cur_from + window_ms, to_ms)
        try:
            raw = client.get_trendbars(
                symbol_id=sym.symbol_id,
                period_minutes=interval_min,
                from_ts_ms=cur_from,
                to_ts_ms=cur_to,
            )
        except Exception as exc:
            log.error("  %s окно %s-%s failed: %s", yf_symbol,
                      datetime.fromtimestamp(cur_from/1000, UTC).date(),
                      datetime.fromtimestamp(cur_to/1000, UTC).date(), exc)
            cur_from = cur_to
            continue

        windows += 1
        for tb in raw:
            ts_ms, o, h, l, c, v = _decode_bar(tb)
            seen[ts_ms] = (ts_ms, o, h, l, c, v)

        time.sleep(SLEEP_BETWEEN_REQUESTS_SEC)
        cur_from = cur_to

    bars = sorted(seen.values(), key=lambda x: x[0])
    log.info("  %s: %d баров в %d окнах (%s → %s)", yf_symbol, len(bars), windows,
             datetime.fromtimestamp(bars[0][0]/1000, UTC).date() if bars else "—",
             datetime.fromtimestamp(bars[-1][0]/1000, UTC).date() if bars else "—")
    return bars


def _write_csv(path: Path, bars: list[tuple[int, float, float, float, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for row in bars:
            w.writerow(row)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=90, help="Глубина истории в днях (default: 90)")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN,
                   help=f"Интервал в минутах (default: {DEFAULT_INTERVAL_MIN})")
    p.add_argument("--out", default="data/fxpro_klines", help="Выходная папка для CSV")
    p.add_argument("--symbols", default="",
                   help="Разделитель-запятые список символов; по умолчанию DEFAULT_SYMBOLS")
    p.add_argument("--window-days", type=int, default=0,
                   help="Размер окна fetch в днях (0=auto по интервалу)")
    args = p.parse_args()

    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip()) or DEFAULT_SYMBOLS
    out_dir = Path(args.out)
    window_days = args.window_days or _default_window_days(args.interval)

    log.info("Fetching %d symbols × %dd M%d (window=%dd) → %s",
             len(symbols), args.days, args.interval, window_days, out_dir)

    s = Settings()
    ts = TokenStore(s.ctrader_token_path)
    td = ensure_valid_token(ts, s.ctrader_client_id, s.ctrader_client_secret)
    client = CTraderClient(
        client_id=s.ctrader_client_id,
        client_secret=s.ctrader_client_secret,
        access_token=td.access_token,
        account_id=s.ctrader_account_id,
        host_type=s.ctrader_host_type,
        refresh_token=td.refresh_token,
    )
    client.start(timeout=30)

    sc = SymbolCache()
    ex = TradeExecutor(client, sc)
    ex.load_symbols()
    log.info("Загружено %d символов из каталога cTrader", len(sc._by_id))

    now_ms = int(time.time() * 1000)
    from_ms = now_ms - args.days * 86400 * 1000

    t_total = time.monotonic()
    stats: list[FetchStats] = []

    for yf_sym in symbols:
        t0 = time.monotonic()
        bars = _fetch_symbol(
            client, sc, yf_sym, from_ms, now_ms, args.interval,
            window_days=window_days,
        )
        if not bars:
            continue
        filename = yf_sym.replace("=X", "").replace("=F", "_F").replace("-", "_")
        out_path = out_dir / f"{filename}_M{args.interval}.csv"
        _write_csv(out_path, bars)
        stats.append(FetchStats(
            symbol=yf_sym,
            bars=len(bars),
            windows=(args.days + window_days - 1) // window_days,
            gaps_filled=0,
            elapsed_sec=time.monotonic() - t0,
        ))
        log.info("  → %s (%.1fs)", out_path, stats[-1].elapsed_sec)

    client.stop()

    log.info("=" * 60)
    log.info("Готово за %.1fs. Инструменты:", time.monotonic() - t_total)
    for st in stats:
        log.info("  %-12s %6d баров (%.1fs)", st.symbol, st.bars, st.elapsed_sec)
    log.info("ИТОГО: %d инструментов, %d баров",
             len(stats), sum(s.bars for s in stats))

    return 0


if __name__ == "__main__":
    sys.exit(main())
