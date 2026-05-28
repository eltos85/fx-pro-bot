"""Crypto macro: BTC dominance + total crypto market cap.

v0.30 (2026-05-28): закрывает hidden-disconnect между SYSTEM_PROMPT
(который ссылается на BTC dominance, post-ETF decoupling, alt-season
rotation) и реальным контекстом, который раньше эту цифру не отдавал.
LLM либо галлюцинировал, либо игнорировал — та же нестыковка #4 из
FX-trader Phase 1 audit (BUILDLOG_AI_FX_TRADER.md 2026-05-26):
«SYSTEM_PROMPT обещает данные, context их не даёт».

──────────────────────────────────────────────────────────────────────
Источник данных: CoinGecko Public API ``/global`` endpoint.

| Поле response | Что это |
|---|---|
| ``data.market_cap_percentage.btc``  | BTC dominance % от total cap |
| ``data.total_market_cap.usd``       | Total crypto market cap USD  |
| ``data.market_cap_change_percentage_24h_usd`` | 24h Δ total cap %    |
| ``data.active_cryptocurrencies``    | counter (для sanity)        |

**Спецификация API** (compliance с ``api-docs.mdc``):
- Endpoint: ``https://api.coingecko.com/api/v3/global``
- Docs: https://docs.coingecko.com/v3.0.1/reference/crypto-global
- Free tier (Demo): **10,000 calls/month, 100 calls/min**, no API key.
- При cache TTL 1h = 720 calls/month (запас 14× от лимита).
- BTC dominance двигается медленно (тренды на дни/недели), не нужно
  чаще обновлять.

──────────────────────────────────────────────────────────────────────
Research basis (для compliance с ``no-data-fitting.mdc``):

| Тезис | Источник |
|---|---|
| BTC.D current ≈60.3% (May 2026), breakout 60.88% | BYDFi May 2026 «BTC      |
| (April 2026)                                     | dominance & capital war» |
|                                                  | https://www.bydfi.com/en/cointalk/bitcoin-dominance-capital-war |
| Altcoin Season Index 35-45/100 (need 75 для     | Bitrue 2026 «BTC dominance |
| altseason)                                       | 60% altcoin season»       |
|                                                  | https://www.bitrue.com/blog/bitcoin-dominance-60-percent-altcoin-season-2026 |
| Key levels: 59.63% support / 66.06% resistance   | AInvest «altcoin season   |
|                                                  | 2026 technical patterns»  |
|                                                  | https://www.ainvest.com/news/altcoin-season-2026-technical-patterns-clash-bitcoin-dominance-2605/ |

──────────────────────────────────────────────────────────────────────
Реализация:
- Cache TTL = 3600s (1 час) по умолчанию.
- Network errors → возвращаем последний кэш или None.
- httpx с timeout 10s (CoinGecko occasionally slow в peak hours).
- Если parse failed (changed JSON schema) — лог + return None.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


_COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"


@dataclass
class CryptoMacroSnapshot:
    """BTC.D + total crypto market cap на момент fetch.

    Все поля могут быть None при partial parse failure (CoinGecko
    периодически меняет schema). Caller должен проверять None.
    """

    btc_dominance_pct: float | None  # 60.3 значит 60.3%
    total_market_cap_usd: float | None  # raw USD value
    total_market_cap_change_24h_pct: float | None  # 24h Δ %
    eth_dominance_pct: float | None  # ETH.D для secondary signal
    fetched_at_utc: str


class CryptoMacroProvider:
    """Кэширующий CoinGecko /global клиент.

    Cache TTL = 1h по умолчанию (BTC.D трендует на дни-недели, не часы).
    No API key required (Demo tier). При rate limit или network failure
    возвращает последний кэш.
    """

    def __init__(self, cache_ttl_sec: int = 3600) -> None:
        self._cache_ttl = cache_ttl_sec
        self._cache: CryptoMacroSnapshot | None = None
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshot(self) -> CryptoMacroSnapshot | None:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            snap = self._fetch_fresh()
        except Exception:
            log.exception(
                "CryptoMacro fetch failed (продолжаю с прошлым кэшем)"
            )
            return self._cache
        if snap is not None:
            self._cache = snap
            self._cache_ts = now
        return snap or self._cache

    def _fetch_fresh(self) -> CryptoMacroSnapshot | None:
        from datetime import UTC, datetime

        import requests

        try:
            resp = requests.get(_COINGECKO_GLOBAL_URL, timeout=10.0)
            resp.raise_for_status()
        except Exception as e:
            log.warning("CoinGecko /global HTTP failure: %s", e)
            return None

        try:
            payload = resp.json()
        except ValueError:
            log.warning("CoinGecko /global вернул не-JSON")
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            log.warning("CoinGecko /global: нет поля 'data'")
            return None

        btc_dom = _safe_float(
            (data.get("market_cap_percentage") or {}).get("btc")
        )
        eth_dom = _safe_float(
            (data.get("market_cap_percentage") or {}).get("eth")
        )
        total_cap = _safe_float(
            (data.get("total_market_cap") or {}).get("usd")
        )
        total_24h = _safe_float(
            data.get("market_cap_change_percentage_24h_usd")
        )

        if btc_dom is None and total_cap is None:
            log.warning("CoinGecko /global: ни btc_dom ни total_cap не парсятся")
            return None

        return CryptoMacroSnapshot(
            btc_dominance_pct=btc_dom,
            total_market_cap_usd=total_cap,
            total_market_cap_change_24h_pct=total_24h,
            eth_dominance_pct=eth_dom,
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _format_cap_human(usd: float) -> str:
    """`3450123456789.0` → `$3.45T`. Trillion/Billion compact."""
    if usd >= 1e12:
        return f"${usd / 1e12:.2f}T"
    if usd >= 1e9:
        return f"${usd / 1e9:.1f}B"
    return f"${usd:,.0f}"


def format_crypto_macro_snapshot(snap: CryptoMacroSnapshot | None) -> str | None:
    """Превратить snapshot в text-блок для LLM context.

    Возвращает ``None`` если snapshot пуст — caller пропускает блок.
    Формат:

        === CRYPTO MACRO (BTC dominance + total cap) ===
        BTC.D=60.3% | ETH.D=12.5% | Total crypto cap=$3.45T (24h=-1.2%)
        Reference levels (May 2026): BTC.D support 59.63% / resistance 66.06%
        Altcoin Season Index threshold >75 (currently ~35-45)
        (fetched 2026-05-28T... UTC)
    """
    if snap is None:
        return None

    parts: list[str] = []
    if snap.btc_dominance_pct is not None:
        parts.append(f"BTC.D={snap.btc_dominance_pct:.2f}%")
    if snap.eth_dominance_pct is not None:
        parts.append(f"ETH.D={snap.eth_dominance_pct:.2f}%")
    if snap.total_market_cap_usd is not None:
        total_str = _format_cap_human(snap.total_market_cap_usd)
        if snap.total_market_cap_change_24h_pct is not None:
            total_str = (
                f"Total crypto cap={total_str} "
                f"(24h={snap.total_market_cap_change_24h_pct:+.2f}%)"
            )
        else:
            total_str = f"Total crypto cap={total_str}"
        parts.append(total_str)

    if not parts:
        return None

    header = "=== CRYPTO MACRO (BTC dominance + total cap) ==="
    ref_line = (
        "Reference levels (May 2026): BTC.D support 59.63% / "
        "resistance 66.06% (AInvest); Altcoin Season Index threshold "
        ">75 currently ~35-45 (Bitrue)"
    )
    return "\n".join(
        [
            header,
            " | ".join(parts),
            ref_line,
            f"(fetched {snap.fetched_at_utc} UTC)",
        ]
    )
