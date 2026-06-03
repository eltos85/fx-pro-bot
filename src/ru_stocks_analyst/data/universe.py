"""Ликвидная вселенная акций MOEX (широкий скринер, с фильтрами)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ru_stocks_analyst.tinkoff.rest_client import TinkoffRestClient, quotation_to_float

log = logging.getLogger("ru_stocks.universe")


@dataclass(frozen=True)
class ShareInstrument:
    figi: str
    ticker: str
    name: str
    uid: str
    currency: str
    exchange: str
    liquidity_flag: bool


def _is_moex_rub_share(raw: dict[str, Any]) -> bool:
    if raw.get("currency", "").lower() != "rub":
        return False
    class_code = (raw.get("classCode") or "").upper()
    exchange = (raw.get("exchange") or "").upper()
    if class_code != "TQBR" and exchange not in ("MOEX", "MOEX_EVENING_WEEKEND"):
        return False
    if not raw.get("apiTradeAvailableFlag", True):
        return False
    return True


def load_moex_shares(client: TinkoffRestClient) -> list[ShareInstrument]:
    raw_list = client.get_shares()
    out: list[ShareInstrument] = []
    for s in raw_list:
        if not _is_moex_rub_share(s):
            continue
        ticker = (s.get("ticker") or "").upper()
        if not ticker or len(ticker) > 12:
            continue
        uid = s.get("uid") or s.get("figi") or ""
        if not uid:
            continue
        out.append(
            ShareInstrument(
                figi=s.get("figi") or "",
                ticker=ticker,
                name=(s.get("name") or ticker)[:80],
                uid=uid,
                currency="rub",
                exchange=(s.get("exchange") or "MOEX"),
                liquidity_flag=bool(s.get("liquidityFlag", False)),
            )
        )
    log.info("MOEX RUB акций после фильтра: %d (из %d)", len(out), len(raw_list))
    return out


def rank_by_last_price(
    client: TinkoffRestClient,
    shares: list[ShareInstrument],
    *,
    min_price_rub: float,
    top_n: int,
    batch_size: int = 100,
) -> list[tuple[ShareInstrument, float]]:
    """Отбор top_n: сначала liquidity_flag, затем цена >= min, сортировка по цене."""
    liquid = [s for s in shares if s.liquidity_flag]
    pool = liquid if len(liquid) >= 20 else shares

    price_map: dict[str, float] = {}
    ids = [s.uid for s in pool]
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        try:
            rows = client.get_last_prices(chunk)
        except Exception:
            log.exception("GetLastPrices batch %d", i)
            continue
        for row in rows:
            iid = row.get("instrumentId") or row.get("figi") or ""
            price_map[iid] = quotation_to_float(row.get("price"))

    scored: list[tuple[ShareInstrument, float]] = []
    for s in pool:
        px = price_map.get(s.uid) or price_map.get(s.figi) or 0.0
        if px < min_price_rub:
            continue
        scored.append((s, px))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]
