"""Внешний macro / sentiment провайдер.

Собирает глобальные индикаторы рынка крипто, не привязанные к
конкретному символу (1 запрос на цикл, кэш 10 минут):

1. **Fear & Greed Index** (alternative.me) — sentiment-индекс 0..100.
   Канонически интерпретируется как **contrarian** sentiment-индикатор
   (см. Warren Buffet «be greedy when others are fearful»; alternative.me
   FAQ): значения ≤25 = «Extreme Fear, исторически buy-zone»,
   ≥75 = «Extreme Greed, исторически sell-zone». Источник:
   https://alternative.me/crypto/fear-and-greed-index/

2. **BTC Dominance %** (CoinGecko `/global`) — доля BTC в общей
   капитализации крипторынка. Используется как regime-индикатор:
   - Растущая dominance = «BTC season», alts слабее.
   - Падающая dominance = «alt-season», капитал ротирует в alt-coins.
   Дополнительно: ETH dominance, stablecoin dominance (USDT+USDC+...) —
   stables ≥10% часто сигнализирует «риск-офф / выход в кеш».

─── Research basis ───
- Alternative.me F&G methodology: композит из volatility, momentum,
    social media, dominance, surveys, trends, BTC dominance. Public,
    no-auth API.
- CoinGecko `/global` endpoint: market_cap_percentage (всего топ-10 coins
    + stables). Free tier: ~50 req/min, более чем достаточно для нашего
    15-мин цикла.
- Garcia, Tessone «Social signals and algorithmic trading of Bitcoin»
    (Royal Society Open Science 2014) — подтверждение что social-fear
    индексы предсказывают reversal'ы (с лагом 24-72ч).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=2"
_COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_DEFAULT_TIMEOUT = 8.0
_USER_AGENT = "ai-trader/1.0 (+https://github.com/eltos85/fx-pro-bot)"

# stable-coin тикеры в CoinGecko market_cap_percentage. Не все могут быть
# в топ-10 одновременно (USDT/USDC всегда там; DAI/FDUSD/PYUSD/USDe — реже).
_STABLE_TICKERS = ("usdt", "usdc", "dai", "fdusd", "tusd", "busd", "usde", "pyusd")


@dataclass
class MacroSnapshot:
    fng_value: int | None
    fng_classification: str | None
    fng_delta_24h: int | None  # value - prev_day_value
    btc_dominance_pct: float | None
    eth_dominance_pct: float | None
    stables_dominance_pct: float | None    # сумма по _STABLE_TICKERS
    market_cap_change_24h_pct: float | None  # global mcap change USD


def _http_get_json(url: str, timeout: float = _DEFAULT_TIMEOUT) -> dict | None:
    """GET URL, возвращает parsed JSON или None при ошибке.

    Использует stdlib `urllib.request` чтобы не дёргать httpx из этого
    модуля (httpx уже в зависимостях, но stdlib проще и достаточно).
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8", errors="replace")
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        log.exception("macro http_get_json failed: %s", url)
        return None


class MacroProvider:
    """TTL-кэшируемый провайдер глобальных macro/sentiment-индикаторов.

    Default TTL 600 секунд (10 минут) — для нашего 15-мин цикла это
    означает один реальный fetch на каждый цикл, что заведомо ниже
    rate-limit'ов CoinGecko (50/min) и alternative.me (нет жёсткого).

    `_get_json` инжектится для тестов (можно подменить сетевой вызов
    на фейковый dict).
    """

    def __init__(
        self,
        ttl_seconds: int = 600,
        get_json: Callable[[str], dict | None] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._cached: MacroSnapshot | None = None
        self._cached_at: float = 0.0
        self._get_json = get_json or _http_get_json

    def get_snapshot(self) -> MacroSnapshot:
        now = time.time()
        if self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached
        snap = self._fetch()
        self._cached = snap
        self._cached_at = now
        return snap

    def _fetch(self) -> MacroSnapshot:
        fng_v, fng_c, fng_d = self._fetch_fng()
        btc_d, eth_d, stables_d, mcap_24h = self._fetch_global_dominance()
        return MacroSnapshot(
            fng_value=fng_v,
            fng_classification=fng_c,
            fng_delta_24h=fng_d,
            btc_dominance_pct=btc_d,
            eth_dominance_pct=eth_d,
            stables_dominance_pct=stables_d,
            market_cap_change_24h_pct=mcap_24h,
        )

    def _fetch_fng(self) -> tuple[int | None, str | None, int | None]:
        data = self._get_json(_FNG_URL)
        if not data:
            return None, None, None
        items = data.get("data") or []
        if not items:
            return None, None, None
        try:
            v_now = int(items[0].get("value"))
            c_now = items[0].get("value_classification")
        except (ValueError, TypeError):
            return None, None, None
        delta = None
        if len(items) >= 2:
            try:
                v_prev = int(items[1].get("value"))
                delta = v_now - v_prev
            except (ValueError, TypeError):
                pass
        return v_now, c_now, delta

    def _fetch_global_dominance(
        self,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        data = self._get_json(_COINGECKO_GLOBAL_URL)
        if not data:
            return None, None, None, None
        d = data.get("data") or {}
        mcp = d.get("market_cap_percentage") or {}

        def _pct(coin: str) -> float | None:
            if coin not in mcp:
                return None
            try:
                return float(mcp[coin])
            except (ValueError, TypeError):
                return None

        btc = _pct("btc")
        eth = _pct("eth")
        stables_total = 0.0
        any_stable = False
        for s in _STABLE_TICKERS:
            v = _pct(s)
            if v is not None:
                stables_total += v
                any_stable = True
        stables = stables_total if any_stable else None
        mcap_change_raw = d.get("market_cap_change_percentage_24h_usd")
        try:
            mcap_change = float(mcap_change_raw) if mcap_change_raw is not None else None
        except (ValueError, TypeError):
            mcap_change = None
        return btc, eth, stables, mcap_change


# ─── Метки + форматирование ──────────────────────────────────────────


def _fng_label(value: int | None) -> str:
    """Canonical alternative.me bands. Contrarian-интерпретация — через
    эксплицитные подсказки, чтобы LLM не путал «Extreme Fear» с «sell»."""
    if value is None:
        return ""
    if value <= 25:
        return " [Extreme Fear, historically contrarian-buy zone]"
    if value <= 44:
        return " [Fear]"
    if value <= 55:
        return " [Neutral]"
    if value <= 74:
        return " [Greed]"
    return " [Extreme Greed, historically contrarian-sell zone]"


def _stables_label(stables_pct: float | None) -> str:
    """Stables dominance: высокая доля = риск-офф, выход в кеш.
    Эмпирический threshold 9% для крипто-цикла 2024-2026."""
    if stables_pct is None:
        return ""
    if stables_pct >= 12:
        return " [HIGH stables — risk-off / cash-heavy]"
    if stables_pct >= 9:
        return " [elevated stables — caution]"
    return ""


def format_macro(s: MacroSnapshot) -> str:
    """Двух-трёхстрочный текст для system-promptа.

    Пример:
        Fear & Greed: 47 (Neutral, +1 vs 24h)
        BTC dom: 58.5% ETH dom: 10.1% Stables: 9.7% [elevated stables — caution]
        Total mcap 24h: -1.63%
    """
    lines: list[str] = []
    if s.fng_value is not None:
        delta_part = (
            f", {'+' if (s.fng_delta_24h or 0) > 0 else ''}{s.fng_delta_24h} vs 24h"
            if s.fng_delta_24h is not None
            else ""
        )
        cls = s.fng_classification or "?"
        lines.append(
            f"  Fear & Greed: {s.fng_value} ({cls}{delta_part}){_fng_label(s.fng_value)}"
        )
    if s.btc_dominance_pct is not None:
        eth_part = (
            f"  ETH dom: {s.eth_dominance_pct:.2f}%"
            if s.eth_dominance_pct is not None
            else ""
        )
        stables_part = (
            f"  Stables: {s.stables_dominance_pct:.2f}%{_stables_label(s.stables_dominance_pct)}"
            if s.stables_dominance_pct is not None
            else ""
        )
        lines.append(
            f"  BTC dom: {s.btc_dominance_pct:.2f}%{eth_part}{stables_part}"
        )
    if s.market_cap_change_24h_pct is not None:
        lines.append(f"  Total mcap 24h: {s.market_cap_change_24h_pct:+.2f}%")
    return "\n".join(lines) if lines else "  (macro: data unavailable)"
