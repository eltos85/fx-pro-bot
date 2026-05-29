"""CFTC Commitments of Traders (COT) feed — Enhancement A (2026-05-29).

SYSTEM_PROMPT в иерархии gold-драйверов прямо называет «ETF/COT», но COT
никогда не подавался в context. Этот модуль закрывает пробел: тянет
**Managed Money** (спекулятивные фонды) net-позиционирование по нашим
инструментам и недельную динамику. Экстремумы / развороты MM-нетто —
классический контрарный сигнал (перекос толпы перед разворотом).

Что подаём (НЕ интерпретируем за LLM):
- net = long − short (контракты), share = net / open_interest,
- неделя-к-неделе Δ net (направление потока умных/спекулятивных денег).
LLM сам решает, экстремум это или нет (no-data-fitting: не зашиваем
пороги «too long / too short» в код).

Research basis:
- Working (1960); Sanders, Boris & Manfredo (2004, Energy Economics) —
  COT positioning как индикатор спекулятивного давления на commodity
  futures. Briese «The Commitments of Traders Bible» (2008) — extremes
  в net-позиции крупных спекулянтов предшествуют разворотам.

Источник (free, без ключа — CFTC public API не требует токена при
умеренном использовании; офиц. дока:
https://dev.socrata.com/foundry/publicreporting.cftc.gov/72hh-3qpy):
Disaggregated Futures-Only report, Socrata resource ``72hh-3qpy``.
Контракты (current names, проверены 2026-05): COMEX Gold, NYMEX Brent
Last Day, NYMEX Henry Hub (NAT GAS NYME).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"

# internal symbol → CFTC market_and_exchange_names (current 2026 contract).
# Имена проверены против live API (publicreporting.cftc.gov), report week
# 2026-05-19. Брент на ICE Europe CFTC не покрывает — NYMEX Brent Last Day
# это US-reported proxy (см. ограничение в docstring formatter).
_SYMBOL_TO_MARKET: dict[str, str] = {
    "XAUUSD": "GOLD - COMMODITY EXCHANGE INC.",
    "BZ=F": "BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE",
    "NG=F": "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE",
}


@dataclass
class CotSnapshot:
    symbol: str
    market_name: str
    report_date: str  # YYYY-MM-DD
    open_interest: int
    mm_long: int
    mm_short: int
    mm_net: int
    mm_net_prev: int | None  # прошлая неделя (для Δ)

    @property
    def mm_net_change(self) -> int | None:
        if self.mm_net_prev is None:
            return None
        return self.mm_net - self.mm_net_prev

    @property
    def net_share_of_oi_pct(self) -> float | None:
        if self.open_interest <= 0:
            return None
        return self.mm_net / self.open_interest * 100.0


class CotProvider:
    """Кэширующий CFTC COT-клиент. TTL по умолчанию 6 часов (отчёт
    обновляется раз в неделю, пятница 15:30 ET)."""

    def __init__(self, cache_ttl_sec: int = 21600, timeout: int = 10) -> None:
        self._cache_ttl = cache_ttl_sec
        self._timeout = timeout
        self._cache: dict[str, CotSnapshot] = {}
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshots(self, symbols: tuple[str, ...]) -> dict[str, CotSnapshot]:
        """Возвращает {symbol: CotSnapshot} для известных символов.

        Кэш общий (по времени); неизвестные символы пропускаются. При
        network failure возвращаем последний кэш (graceful degrade).
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return {s: self._cache[s] for s in symbols if s in self._cache}
        fresh: dict[str, CotSnapshot] = {}
        for sym in symbols:
            market = _SYMBOL_TO_MARKET.get(sym)
            if not market:
                continue
            try:
                snap = self._fetch_one(sym, market)
            except Exception:
                log.exception("COT fetch failed для %s (%s)", sym, market)
                snap = self._cache.get(sym)
            if snap is not None:
                fresh[sym] = snap
        if fresh:
            self._cache = fresh
            self._cache_ts = now
        return {s: self._cache[s] for s in symbols if s in self._cache}

    def _fetch_one(self, symbol: str, market: str) -> CotSnapshot | None:
        import requests

        params = {
            "$select": (
                "report_date_as_yyyy_mm_dd,open_interest_all,"
                "m_money_positions_long_all,m_money_positions_short_all"
            ),
            "$where": f"market_and_exchange_names='{market}'",
            "$order": "report_date_as_yyyy_mm_dd desc",
            "$limit": 2,
        }
        resp = requests.get(_COT_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def _i(row: dict, key: str) -> int:
            try:
                return int(float(row.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        long_ = _i(latest, "m_money_positions_long_all")
        short_ = _i(latest, "m_money_positions_short_all")
        net = long_ - short_
        net_prev = None
        if prev is not None:
            net_prev = _i(prev, "m_money_positions_long_all") - _i(
                prev, "m_money_positions_short_all"
            )
        report_date = str(latest.get("report_date_as_yyyy_mm_dd", ""))[:10]
        return CotSnapshot(
            symbol=symbol,
            market_name=market,
            report_date=report_date,
            open_interest=_i(latest, "open_interest_all"),
            mm_long=long_,
            mm_short=short_,
            mm_net=net,
            mm_net_prev=net_prev,
        )


def format_cot_snapshots(snaps: dict[str, CotSnapshot]) -> str | None:
    """Text-блок COT для LLM. None если нет данных.

    Брент: NYMEX Brent Last Day — US-reported proxy (основной Brent ликвид
    на ICE Europe, который CFTC не покрывает). LLM это сообщается в строке.
    """
    if not snaps:
        return None
    lines = [
        "=== CFTC COT — MANAGED MONEY positioning (weekly, contrarian at "
        "extremes; net=long−short) ==="
    ]
    for sym, s in snaps.items():
        net_dir = "net-LONG" if s.mm_net >= 0 else "net-SHORT"
        share = (
            f"{s.net_share_of_oi_pct:+.1f}% of OI"
            if s.net_share_of_oi_pct is not None
            else "n/a"
        )
        chg = (
            f"Δwk={s.mm_net_change:+d}"
            if s.mm_net_change is not None
            else "Δwk=n/a"
        )
        proxy = " [NYMEX Brent proxy]" if sym == "BZ=F" else ""
        lines.append(
            f"[{sym}]{proxy} {net_dir} {s.mm_net:+d} "
            f"(L={s.mm_long} S={s.mm_short}, {share}, {chg}; "
            f"OI={s.open_interest}, report {s.report_date})"
        )
    return "\n".join(lines)
