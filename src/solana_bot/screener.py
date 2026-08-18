"""Скринер Solana: GeckoTerminal trending + Dexscreener цена открытой.

Офдок:
  GeckoTerminal/CoinGecko onchain trending:
    https://docs.coingecko.com/reference/trending-pools-network
    публичное зеркало https://api.geckoterminal.com/api/v2/
    (лимит 10 вызовов/мин — https://www.geckoterminal.com/dex-api)
  Dexscreener tokens:
    https://docs.dexscreener.com/api/reference
    GET /tokens/v1/{chainId}/{tokenAddresses}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from solana_bot.signals import Shield

log = logging.getLogger("solana_bot.screener")

_GT = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
_DS = "https://api.dexscreener.com/tokens/v1/solana"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _age_sec(created: str | None) -> float:
    if not created:
        return 0.0
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except ValueError:
        return 0.0


def trending_shields() -> list[Shield]:
    """duration=5m — тот же срез, что volume ≥$100k / 5 мин."""
    try:
        resp = requests.get(
            _GT,
            params={"duration": "5m", "include": "base_token,quote_token"},
            timeout=20,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        log.exception("geckoterminal trending")
        return []
    tokens = {}
    for inc in payload.get("included") or []:
        if inc.get("type") != "token":
            continue
        attrs = inc.get("attributes") or {}
        tokens[inc.get("id")] = {
            "address": attrs.get("address") or "",
            "symbol": attrs.get("symbol") or "",
        }
    out: list[Shield] = []
    for row in payload.get("data") or []:
        attrs = row.get("attributes") or {}
        rel = row.get("relationships") or {}
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id")
        quote_id = ((rel.get("quote_token") or {}).get("data") or {}).get("id")
        base = tokens.get(base_id) or {}
        quote = tokens.get(quote_id) or {}
        vol = attrs.get("volume_usd") or {}
        chg = attrs.get("price_change_percentage") or {}
        mint = base.get("address") or ""
        if not mint:
            continue
        out.append(Shield(
            mint=mint,
            symbol=base.get("symbol") or mint[:6],
            quote_mint=quote.get("address") or "",
            volume_m5=_f(vol.get("m5")),
            move_m5_pct=_f(chg.get("m5")),
            liquidity_usd=_f(attrs.get("reserve_in_usd")),
            age_sec=_age_sec(attrs.get("pool_created_at")),
            price_usd=_f(attrs.get("base_token_price_usd")),
        ))
    return out


def token_price_usd(mint: str) -> float:
    try:
        resp = requests.get(f"{_DS}/{mint}", timeout=15)
        resp.raise_for_status()
        pairs = resp.json()
    except Exception:
        log.exception("dexscreener price %s", mint[:8])
        return 0.0
    if not isinstance(pairs, list):
        return 0.0
    best = 0.0
    best_liq = -1.0
    for p in pairs:
        if (p.get("chainId") or "") != "solana":
            continue
        liq = _f((p.get("liquidity") or {}).get("usd"))
        px = _f(p.get("priceUsd"))
        if px > 0 and liq >= best_liq:
            best, best_liq = px, liq
    return best
