"""Живой поток цены для AI-Trader (Bybit perpetual-futures).

Phase 1 эквивалент порта fx-архитектуры (2026-05-29): даёт in-memory
кэш «последней цены» по каждому символу через public WebSocket-стрим
тикеров Bybit. Событийные датчики (``price_sensor.py``) читают этот кэш
без единого REST-вызова — поэтому опрос раз в ~15с бесплатен по API.

Что модуль НЕ делает (strategy-guard.mdc):
- НЕ открывает / не закрывает позиции, не двигает SL/TP.
- НЕ принимает торговых решений. Только поддерживает кэш живой цены.

Подключение / подписка (api-docs.mdc — параметры из официальной доки):
- pybit ``WebSocket(channel_type="linear")`` — public ticker stream,
  push 100ms. Auth/demo-флаг НЕ нужен: публичные market-data одинаковы
  для demo и live (demo-сабдомен — только для private-стримов).
  https://bybit-exchange.github.io/docs/v5/websocket/public/ticker
- ``ping_interval=20`` / ``ping_timeout=10`` — keep-alive heartbeat
  (pybit шлёт ``{"op":"ping"}``); ``retries`` + ``restart_on_error``
  включают встроенный авто-reconnect и re-subscribe топиков.
  https://github.com/bybit-exchange/pybit/blob/master/pybit/_websocket_stream.py

Linear ticker шлёт ``type=snapshot`` (полный набор полей) затем
``type=delta`` (только изменившиеся поля) — кэш МЁРЖИТ присутствующие
поля, иначе хранит прошлое значение
(https://bybit-exchange.github.io/docs/v5/websocket/public/ticker).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("ai_trader.price_stream")


class BybitPriceStream:
    """In-memory кэш живой mid-цены поверх pybit public ticker-стрима.

    Потокобезопасность: callback приходит в ws-потоке pybit, чтение
    (``get_live_mid``) — из main-loop, поэтому кэш под ``threading.Lock``.

    ``max_age_sec``: если по символу не было апдейта дольше этого порога
    (обрыв стрима / нет ликвидности) — ``get_live_mid`` возвращает None,
    и датчики на этом символе не стреляют → безопасная деградация к
    плановым таймерам.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        category: str = "linear",
        testnet: bool = False,
        max_age_sec: float = 60.0,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        retries: int = 10,
        ws_factory: Callable[..., Any] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._symbols = list(symbols)
        self._category = category
        self._testnet = testnet
        self._max_age_sec = max_age_sec
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._retries = retries
        self._ws_factory = ws_factory
        self._now = now
        self._lock = threading.Lock()
        # symbol -> {"mark": float|None, "last": float|None, "ts": monotonic}
        self._cache: dict[str, dict[str, float]] = {}
        self._ws: Any = None

    def start(self) -> None:
        """Открыть WS и подписаться на ticker-стрим всех символов."""
        if not self._symbols:
            log.warning("BybitPriceStream: нет символов — стрим не запущен")
            return
        try:
            if self._ws_factory is not None:
                self._ws = self._ws_factory()
            else:
                from pybit.unified_trading import WebSocket

                self._ws = WebSocket(
                    testnet=self._testnet,
                    channel_type=self._category,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    retries=self._retries,
                    restart_on_error=True,
                    trace_logging=False,
                )
            self._ws.ticker_stream(symbol=self._symbols, callback=self._on_tick)
            log.info(
                "BybitPriceStream: подписка на %d тикеров (%s)",
                len(self._symbols),
                ", ".join(self._symbols),
            )
        except Exception:
            log.exception("BybitPriceStream.start failed (фолбэк: датчики молчат)")
            self._ws = None

    def stop(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.exit()
        except Exception:
            log.exception("BybitPriceStream.stop: ws.exit() failed")
        finally:
            self._ws = None

    def _on_tick(self, message: dict) -> None:
        """Callback ws-потока. Мёржит snapshot/delta в кэш.

        Формат linear ticker:
        ``{"topic":"tickers.BTCUSDT","type":"snapshot|delta",
           "data":{"symbol":"BTCUSDT","lastPrice":"...","markPrice":"...",...}}``
        """
        try:
            data = message.get("data")
            if not isinstance(data, dict):
                return
            symbol = data.get("symbol")
            if not symbol:
                return
            mark = _parse_float(data.get("markPrice"))
            last = _parse_float(data.get("lastPrice"))
            now = self._now()
            with self._lock:
                entry = self._cache.setdefault(symbol, {})
                # delta шлёт только изменившиеся поля — мёржим присутствующие.
                if mark is not None:
                    entry["mark"] = mark
                if last is not None:
                    entry["last"] = last
                if mark is not None or last is not None:
                    entry["ts"] = now
        except Exception:
            log.exception("BybitPriceStream._on_tick parse failed")

    def get_live_mid(self, symbol: str) -> float | None:
        """Живая mid-цена символа (markPrice приоритетнее lastPrice).

        None если: символ не виден в стриме, или апдейт старше
        ``max_age_sec`` (обрыв / стейл).
        """
        with self._lock:
            entry = self._cache.get(symbol)
            if not entry:
                return None
            ts = entry.get("ts")
            if ts is None or (self._now() - ts) > self._max_age_sec:
                return None
            price = entry.get("mark")
            if price is None:
                price = entry.get("last")
            return price

    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return bool(self._ws.is_connected())
        except Exception:
            return False


def _parse_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    if f <= 0:
        return None
    return f
