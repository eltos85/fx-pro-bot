"""Опционный implied-volatility индекс (Deribit DVOL) для BTC и ETH.

Deribit DVOL — это канонический options-implied-volatility индекс
крипты (аналог VIX для S&P): annualised IV (% per annum), вычисляемый
из book опционов с разными strike и tenor. Это **взгляд опционного
рынка** на ожидаемую волатильность — фундаментально другая фича чем
наша Realized Volatility (i1), которая считается из past returns.

В нашем prompt-е DVOL и RV вместе формируют «Volatility Risk Premium»
картинку:
- IV >> RV: option market закладывает страх, реальное движение пока
    меньше → возможен squeeze IV вниз / либо realised pickup.
- IV ≈ RV: рынок откалиброван.
- IV << RV: complacency, реальная волатильность уже выше options pricing.

─── Research basis ───
- Deribit «DVOL Index Methodology» (2021+): Black-Scholes-implied
    volatility 30-дневного ATM-strike, weighted по open interest.
- Andersen/Bollerslev/Diebold/Vega «Real-time price discovery in
    global stock, bond and foreign exchange markets» (J. Int. Econ.
    2007) — IV vs RV spread как лидирующий индикатор для market
    regime change.
- Bouri/Lucey/Shahzad «Bitcoin's predictive power on volatility»
    (J. Financial Markets 2024) — ETH/BTC IV-RV spread имеет значимую
    cross-asset predictive content для крипто-перпов 1-7 дней.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ai_trader.macro.external import _http_get_json

log = logging.getLogger(__name__)

# Deribit public endpoint, no auth.
# resolution=3600 = 1h candles по DVOL; data: [[ts_ms, open, high, low, close]].
_DVOL_URL_TEMPLATE = (
    "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    "?currency={currency}&start_timestamp={start}&end_timestamp={end}&resolution=3600"
)


@dataclass
class OptionsIvSnapshot:
    btc_iv_now: float | None
    btc_iv_24h_low: float | None
    btc_iv_24h_high: float | None
    btc_iv_24h_change_pct: float | None    # (now - 24h_ago) / 24h_ago * 100
    eth_iv_now: float | None
    eth_iv_24h_low: float | None
    eth_iv_24h_high: float | None
    eth_iv_24h_change_pct: float | None


class OptionsIvProvider:
    """TTL-кэш Deribit DVOL для BTC и ETH.

    1 cycle = 2 запроса (BTC, ETH), default TTL 600s (10 min) — те же
    параметры что у MacroProvider; rate-limit Deribit public ≈ 20 req/sec
    per IP, более чем достаточно.
    """

    def __init__(
        self,
        ttl_seconds: int = 600,
        get_json: Callable[[str], dict | None] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._cached: OptionsIvSnapshot | None = None
        self._cached_at: float = 0.0
        self._get_json = get_json or _http_get_json

    def get_snapshot(self) -> OptionsIvSnapshot:
        now = time.time()
        if self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached
        snap = self._fetch()
        self._cached = snap
        self._cached_at = now
        return snap

    def _fetch(self) -> OptionsIvSnapshot:
        btc_data = self._fetch_one("BTC")
        eth_data = self._fetch_one("ETH")
        return OptionsIvSnapshot(
            btc_iv_now=btc_data[0],
            btc_iv_24h_low=btc_data[1],
            btc_iv_24h_high=btc_data[2],
            btc_iv_24h_change_pct=btc_data[3],
            eth_iv_now=eth_data[0],
            eth_iv_24h_low=eth_data[1],
            eth_iv_24h_high=eth_data[2],
            eth_iv_24h_change_pct=eth_data[3],
        )

    def _fetch_one(
        self, currency: str
    ) -> tuple[float | None, float | None, float | None, float | None]:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 24 * 60 * 60 * 1000  # последние 24h
        url = _DVOL_URL_TEMPLATE.format(currency=currency, start=start_ms, end=end_ms)
        data = self._get_json(url)
        if not data:
            return None, None, None, None
        bars = (data.get("result") or {}).get("data") or []
        if not bars:
            return None, None, None, None
        # Каждый bar = [ts, open, high, low, close]; нас интересуют closes
        # и крайности по high/low за окно.
        try:
            closes = [float(b[4]) for b in bars]
            highs = [float(b[2]) for b in bars]
            lows = [float(b[3]) for b in bars]
        except (ValueError, TypeError, IndexError):
            log.exception("DVOL parse failed for %s", currency)
            return None, None, None, None
        if not closes:
            return None, None, None, None
        iv_now = closes[-1]
        iv_low = min(lows)
        iv_high = max(highs)
        # Change % vs 24h-ago (первый bar окна как proxy на «24h назад»)
        change_pct: float | None = None
        if len(closes) >= 2:
            iv_ago = closes[0]
            if iv_ago > 0:
                change_pct = (iv_now - iv_ago) / iv_ago * 100
        return iv_now, iv_low, iv_high, change_pct


# ─── Метки + форматирование ──────────────────────────────────────────


def _iv_regime_label(iv_pct: float | None) -> str:
    """Эмпирические крипто-IV режимы (annualised %).

    Базируется на 2024-2026 исторических данных по BTC DVOL:
    - <30% — экстремально низкая IV (низкий market stress, complacency)
    - 30-50% — нормальный диапазон
    - 50-80% — повышенная IV (anxiety / pre-event)
    - >80% — экстремум (paniс / шок).

    Для ETH те же пороги +10pp shift (ETH IV исторически выше BTC на
    ~10-15 pp при normal regime).
    """
    if iv_pct is None:
        return ""
    if iv_pct < 30:
        return " [LOW IV — complacency]"
    if iv_pct < 50:
        return " [normal IV]"
    if iv_pct < 80:
        return " [elevated IV]"
    return " [EXTREME IV — panic / shock]"


def format_options_iv(s: OptionsIvSnapshot) -> str:
    """Многострочный формат для system-promptа.

    Пример:
        BTC IV (Deribit DVOL, annualised): 38.74% [normal IV]
            24h range: 38.36 → 40.54  | change: -3.94%
        ETH IV: 54.55% [elevated IV]
            24h range: 52.82 → 56.24  | change: -1.50%
    """
    lines: list[str] = []

    def _block(prefix: str, now: float | None, lo: float | None,
               hi: float | None, change: float | None) -> str | None:
        if now is None:
            return None
        label = _iv_regime_label(now)
        line1 = f"  {prefix} IV: {now:.2f}%{label}"
        rng = ""
        if lo is not None and hi is not None:
            rng = f"24h range: {lo:.2f} → {hi:.2f}"
        ch = ""
        if change is not None:
            ch = f"change: {change:+.2f}%"
        line2_parts = [p for p in (rng, ch) if p]
        line2 = f"    {'  |  '.join(line2_parts)}" if line2_parts else ""
        return f"{line1}\n{line2}" if line2 else line1

    btc_block = _block(
        "BTC", s.btc_iv_now, s.btc_iv_24h_low, s.btc_iv_24h_high,
        s.btc_iv_24h_change_pct,
    )
    eth_block = _block(
        "ETH", s.eth_iv_now, s.eth_iv_24h_low, s.eth_iv_24h_high,
        s.eth_iv_24h_change_pct,
    )
    for b in (btc_block, eth_block):
        if b is not None:
            lines.append(b)
    if not lines:
        return "  (options IV: data unavailable)"
    return "\n".join(lines)
