"""Universe-менеджер (milestone M3).

REST-список спот-пар MEXC/Bitget, фильтры (quote=USDT, оборот в диапазоне
``YORSH_MIN/MAX_24H_VOLUME_USD``, blacklist мейджоров), ротация раз в
``YORSH_UNIVERSE_REFRESH_HOURS``, protected-символы (active-кандидаты из БД
не отписываются), менеджер подписок с лимитом на соединение
(MEXC ≤30 подписок = ≤15 символов; Bitget ≤50 каналов = ≤25 символов).

REST-эндпоинты (api-docs.mdc — со ссылкой):
- MEXC: GET /api/v3/exchangeInfo (base/quote) +
  GET /api/v3/ticker/24hr (quoteVolume; null → fallback volume×lastPrice).
  https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/
- Bitget: GET /api/v2/spot/public/symbols (baseCoin/quoteCoin) +
  GET /api/v2/spot/market/tickers (quoteVolume).
  https://www.bitget.com/api-doc/common/

Фильтр/diff/batching — чистая логика (тесты офлайн). REST-fetcher — тонкий,
по доке; live в sandbox не проверяется.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

import aiohttp

from yorsh_bot.config.settings import YorshSettings

log = logging.getLogger("yorsh_bot.universe")

# Per-exchange лимит символов на одно WS-соединение
# (MEXC: 30 подписок / 2 канала = 15; Bitget: 50 каналов / 2 = 25).
SYMBOLS_PER_CONN = {"mexc": 15, "bitget": 25}

# Чёрный список мейджоров/топ-монет (ТЗ раздел 4, промт M3 п.2) — по base-asset.
# Расширяемо: проверка через base-asset (без USDT-суффикса).
MAJORITY_BLACKLIST = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "TRX", "AVAX",
    "DOT", "MATIC", "LINK", "TON", "SHIB", "LTC", "BCH", "NEAR", "UNI",
    "APT", "FIL", "ARB", "OP", "ATOM", "ETC", "XLM", "ICP", "HBAR",
})


@dataclass
class TickerRow:
    """Нормализованная строка тикера (exchange-agnostic)."""
    symbol: str          # BTCUSDT
    base: str            # BTC
    quote: str           # USDT
    volume_usd_24h: float


# Инжектируемый fetcher: async(exchange) -> Sequence[TickerRow].
# Реализация по умолчанию — REST (ниже); тесты подставляют synthetic.
Fetcher = Callable[[str], Awaitable[Sequence[TickerRow]]]


def _base(symbol: str) -> str:
    # у нас все quote=USDT → base = symbol без 'USDT'. Запасной вариант: до USDT.
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol.split("USDT")[0] if "USDT" in symbol else symbol


def filter_universe(rows: Sequence[TickerRow], *,
                    quote: str = "USDT",
                    min_vol: float, max_vol: float,
                    blacklist: frozenset[str] = MAJORITY_BLACKLIST,
                    protected: set[str] | None = None) -> list[str]:
    """Чистая фильтрация: quote, оборот в диапазоне, не-мейджор, + protected.

    Protected-символы (active-кандидаты) добавляются БЕЗ фильтра по обороту/
    мейджору (раз они уже кандидаты — не отписываемся), но только если они
    есть в ``rows`` (символ реально торгуется на бирже).
    """
    protected = protected or set()
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r.quote != quote:
            continue
        if r.symbol in seen:
            continue
        is_protected = r.symbol in protected
        if not is_protected:
            if _base(r.symbol) in blacklist:
                continue
            if not (min_vol <= r.volume_usd_24h <= max_vol):
                continue
        out.append(r.symbol)
        seen.add(r.symbol)
    # protected, которых не оказалось в rows (delisted/пауза) — не добавляем
    # (нельзя подписаться на несуществующий символ).
    return out


def diff_subscriptions(current: set[str], target: set[str]) -> tuple[list[str], list[str]]:
    """Вернуть (to_add, to_remove) для перехода current → target."""
    to_add = sorted(target - current)
    to_remove = sorted(current - target)
    return to_add, to_remove


def batch_by_conn(symbols: Sequence[str], per_conn: int) -> list[list[str]]:
    """Разбить символы на батчи по per_conn на соединение (лимит WS-подписок)."""
    if per_conn <= 0:
        raise ValueError("per_conn must be > 0")
    return [list(symbols[i:i + per_conn])
            for i in range(0, len(symbols), per_conn)]


# ─── REST fetchers (по доке) ─────────────────────────────────────────────

MEXC_EXCHANGE_INFO = "https://api.mexc.com/api/v3/exchangeInfo"
MEXC_TICKER_24H = "https://api.mexc.com/api/v3/ticker/24hr"
BITGET_SYMBOLS = "https://api.bitget.com/api/v2/spot/public/symbols"
BITGET_TICKERS = "https://api.bitget.com/api/v2/spot/market/tickers"


async def _fetch_mexc() -> Sequence[TickerRow]:
    async with aiohttp.ClientSession() as s:
        async with s.get(MEXC_EXCHANGE_INFO) as r:
            r.raise_for_status()
            info = await r.json()
        async with s.get(MEXC_TICKER_24H) as r:
            r.raise_for_status()
            tickers = await r.json()
    # base/quote из exchangeInfo
    base_quote: dict[str, tuple[str, str]] = {}
    for sym in info.get("symbols", []):
        base_quote[sym["symbol"]] = (sym.get("baseAsset", ""),
                                     sym.get("quoteAsset", ""))
    # quoteVolume (USDT); null → fallback volume × lastPrice
    rows: list[TickerRow] = []
    for t in tickers:
        sym = t.get("symbol")
        if not sym:
            continue
        base, quote = base_quote.get(sym, (_base(sym), "USDT"))
        qv = t.get("quoteVolume")
        try:
            vol = float(qv) if qv not in (None, "") else float(
                t.get("volume", 0) or 0) * float(t.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            vol = 0.0
        rows.append(TickerRow(sym, base, quote, vol))
    return rows


async def _fetch_bitget() -> Sequence[TickerRow]:
    async with aiohttp.ClientSession() as s:
        async with s.get(BITGET_SYMBOLS) as r:
            r.raise_for_status()
            syms = (await r.json()).get("data", [])
        async with s.get(BITGET_TICKERS) as r:
            r.raise_for_status()
            ticks = (await r.json()).get("data", [])
    bq: dict[str, tuple[str, str]] = {}
    for sm in syms:
        bq[sm["symbol"]] = (sm.get("baseCoin", ""), sm.get("quoteCoin", ""))
    rows: list[TickerRow] = []
    for t in ticks:
        sym = t.get("symbol")
        if not sym:
            continue
        base, quote = bq.get(sym, (_base(sym), "USDT"))
        try:
            vol = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            vol = 0.0
        rows.append(TickerRow(sym, base, quote, vol))
    return rows


_FETCHERS = {"mexc": _fetch_mexc, "bitget": _fetch_bitget}


class UniverseManager:
    """Менеджер вселенной подписок.

    ``refresh()`` — тянет REST, фильтрует, добавляет protected, считает diff
    против ``current`` и логирует add/remove в universe_log (через callback).
    ``batches()`` — разбивает target-сет на соединения по лимиту.
    """

    def __init__(self, settings: YorshSettings, *,
                 fetcher: Fetcher | None = None,
                 log_event: Callable[[str, str, str | None], None] | None = None,
                 get_protected: Callable[[str], set[str]] | None = None) -> None:
        self.settings = settings
        self._fetcher = fetcher
        self._log = log_event or (lambda exch, ev, sym: None)
        self._get_protected = get_protected or (lambda exch: set())
        # current[exchange] = set подписанных символов
        self.current: dict[str, set[str]] = {e: set() for e in settings.exchange_list}
        # target[exchange] = последний вычисленный целевой сет
        self.target: dict[str, list[str]] = {e: [] for e in settings.exchange_list}

    async def _fetch(self, exchange: str) -> Sequence[TickerRow]:
        if self._fetcher is not None:
            return await self._fetcher(exchange)
        return await _FETCHERS[exchange]()

    async def refresh(self, exchange: str) -> tuple[list[str], list[str]]:
        """Обновить вселенную для биржи. Вернуть (to_add, to_remove)."""
        rows = await self._fetch(exchange)
        protected = self._get_protected(exchange)
        target_list = filter_universe(
            rows, quote="USDT",
            min_vol=self.settings.min_24h_volume_usd,
            max_vol=self.settings.max_24h_volume_usd,
            protected=protected)
        target_set = set(target_list)
        self.target[exchange] = target_list
        to_add, to_remove = diff_subscriptions(self.current[exchange], target_set)
        for sym in to_add:
            self._log(exchange, "add", sym)
        for sym in to_remove:
            self._log(exchange, "remove", sym)
        self.current[exchange] = target_set
        return to_add, to_remove

    def batches(self, exchange: str) -> list[list[str]]:
        per = SYMBOLS_PER_CONN.get(exchange, 15)
        return batch_by_conn(self.target.get(exchange, []), per)

    async def run_loop(self, stop: Callable[[], bool] = lambda: False) -> None:
        """Периодический refresh всех бирж (раз в universe_refresh_hours)."""
        interval = self.settings.universe_refresh_hours * 3600
        while not stop():
            for exch in self.settings.exchange_list:
                try:
                    await self.refresh(exch)
                except Exception as e:  # noqa: BLE001
                    log.warning("universe refresh %s failed: %s", exch, e)
                    self._log(exch, "refresh", f"error: {e}")
            await asyncio.sleep(interval)
