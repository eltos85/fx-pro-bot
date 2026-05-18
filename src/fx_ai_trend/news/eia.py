"""EIA Open Data API client для macro-context по нефти и газу.

Источник: https://api.eia.gov/v2/ (free, регистрация на eia.gov).

Что собираем:

Oil (PET-серии):
- Weekly U.S. Ending Stocks of Crude Oil (последняя точка + change vs
  предыдущая)
- Refinery Utilization Rate (%)
- Strategic Petroleum Reserve (SPR) inventory

Natural Gas (NG-серии):
- Weekly Working Natural Gas in Underground Storage (Lower 48), Bcf:
  headline number EIA Weekly Natural Gas Storage Report (Thu 10:30 ET).
  Build vs draw vs 5y average — основной supply-индикатор для NG.

Обновление:
- Crude / refinery / SPR — среды 10:30 ET (14:30/15:30 UTC).
- NG storage — четверги 10:30 ET (14:30/15:30 UTC).

6-часовой кэш достаточен (даже агрессивный — 24h хватило бы для weekly).

NB: для XAUUSD EIA не релевантен. LLM получит соответствующие блоки
только если в symbols есть BZ=F (oil) и/или NG=F (gas).

Если ``api_key`` пустой — провайдер deactivates сам, RSS остаётся работать.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


# Series IDs EIA v2 API:
# - PET.WCESTUS1.W: weekly U.S. ending stocks of crude oil, thousand barrels
# - PET.WGIRIUS2.W: weekly refinery operable capacity utilization rate, percent
# - PET.WCSSTUS1.W: weekly SPR ending stocks, thousand barrels
# - NG.NW2_EPG0_SWO_R48_BCF.W: Weekly Working Underground Storage, Lower 48
#   States, Bcf. Headline series EIA Weekly Natural Gas Storage Report
#   (Thursday 10:30 ET / 14:30 UTC).
#   Source: https://www.eia.gov/dnav/ng/ng_stor_wkly_s1_w.htm
_SERIES_CRUDE_STOCKS = "PET.WCESTUS1.W"
_SERIES_REFINERY_UTIL = "PET.WGIRIUS2.W"
_SERIES_SPR = "PET.WCSSTUS1.W"
_SERIES_NG_STORAGE = "NG.NW2_EPG0_SWO_R48_BCF.W"

_BASE_URL = "https://api.eia.gov/v2"


@dataclass
class EiaSnapshot:
    crude_stocks_kbarrels: float | None
    crude_stocks_change_kbarrels: float | None  # vs предыдущая неделя
    crude_stocks_date: str | None  # ISO date
    refinery_util_pct: float | None
    refinery_util_date: str | None
    spr_kbarrels: float | None
    spr_date: str | None
    # Gas: Working Natural Gas in Underground Storage, Lower 48, Bcf.
    # change_bcf — недельная разница (build > 0, draw < 0).
    ng_storage_bcf: float | None = None
    ng_storage_change_bcf: float | None = None
    ng_storage_date: str | None = None


class EiaProvider:
    """Кэширующий EIA-клиент. По дефолту cache TTL = 6 часов."""

    def __init__(
        self,
        api_key: str,
        cache_ttl_sec: int = 21600,
        request_timeout_sec: int = 15,
    ) -> None:
        self._api_key = api_key
        self._cache_ttl = cache_ttl_sec
        self._timeout = request_timeout_sec
        self._cache: EiaSnapshot | None = None
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def get_snapshot(self) -> EiaSnapshot | None:
        if not self.enabled:
            return None
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            snap = self._fetch_fresh()
        except Exception:
            log.exception("EIA fetch failed (продолжаю с прошлым кэшем)")
            return self._cache
        self._cache = snap
        self._cache_ts = now
        return snap

    def _fetch_fresh(self) -> EiaSnapshot:
        stocks_data = self._fetch_latest_two(_SERIES_CRUDE_STOCKS)
        util_data = self._fetch_latest_two(_SERIES_REFINERY_UTIL)
        spr_data = self._fetch_latest_two(_SERIES_SPR)
        # NG storage не критичен: если series ID окажется недоступен или
        # сменён EIA, ловим Exception и пропускаем blok (RSS подстрахует).
        try:
            ng_data = self._fetch_latest_two(_SERIES_NG_STORAGE)
        except Exception:
            log.exception("EIA NG storage fetch failed (продолжаю без газ-блока)")
            ng_data = []

        cs_value = cs_change = cs_date = None
        if stocks_data:
            cs_value = stocks_data[0][1]
            if len(stocks_data) >= 2:
                cs_change = stocks_data[0][1] - stocks_data[1][1]
            cs_date = stocks_data[0][0]

        ru_value = ru_date = None
        if util_data:
            ru_value = util_data[0][1]
            ru_date = util_data[0][0]

        spr_value = spr_date = None
        if spr_data:
            spr_value = spr_data[0][1]
            spr_date = spr_data[0][0]

        ng_value = ng_change = ng_date = None
        if ng_data:
            ng_value = ng_data[0][1]
            if len(ng_data) >= 2:
                ng_change = ng_data[0][1] - ng_data[1][1]
            ng_date = ng_data[0][0]

        return EiaSnapshot(
            crude_stocks_kbarrels=cs_value,
            crude_stocks_change_kbarrels=cs_change,
            crude_stocks_date=cs_date,
            refinery_util_pct=ru_value,
            refinery_util_date=ru_date,
            spr_kbarrels=spr_value,
            spr_date=spr_date,
            ng_storage_bcf=ng_value,
            ng_storage_change_bcf=ng_change,
            ng_storage_date=ng_date,
        )

    def _fetch_latest_two(self, series_id: str) -> list[tuple[str, float]]:
        """Возвращает последние 2 точки [(period, value), ...] sorted by period desc.

        EIA v2 API endpoint: /v2/seriesid/{series_id}/data/?api_key=...&sort[]=...&length=2
        """
        url = f"{_BASE_URL}/seriesid/{series_id}"
        params = {
            "api_key": self._api_key,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 2,
        }
        resp = requests.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("response", {}).get("data") or []
        out: list[tuple[str, float]] = []
        for row in data[:2]:
            period = row.get("period", "")
            value_raw = row.get("value")
            if value_raw is None:
                continue
            try:
                value = float(value_raw)
            except (TypeError, ValueError):
                continue
            out.append((period, value))
        return out


def format_eia_snapshot(snap: EiaSnapshot | None) -> str | None:
    """Превращает EiaSnapshot в краткий human-readable блок для LLM-context.

    Возвращает ``None`` если данных нет (тогда блок пропускается в
    context-builder).
    """
    if snap is None:
        return None
    petroleum_lines: list[str] = []
    if snap.crude_stocks_kbarrels is not None:
        change_note = ""
        if snap.crude_stocks_change_kbarrels is not None:
            sign = "+" if snap.crude_stocks_change_kbarrels >= 0 else ""
            change_note = (
                f" ({sign}{snap.crude_stocks_change_kbarrels:.0f}k vs prev week)"
            )
        petroleum_lines.append(
            f"Crude oil stocks: {snap.crude_stocks_kbarrels:.0f}k barrels"
            f"{change_note} as of {snap.crude_stocks_date or '?'}"
        )
    if snap.refinery_util_pct is not None:
        petroleum_lines.append(
            f"Refinery utilization: {snap.refinery_util_pct:.1f}% "
            f"as of {snap.refinery_util_date or '?'}"
        )
    if snap.spr_kbarrels is not None:
        petroleum_lines.append(
            f"SPR: {snap.spr_kbarrels:.0f}k barrels as of {snap.spr_date or '?'}"
        )
    gas_lines: list[str] = []
    if snap.ng_storage_bcf is not None:
        change_note = ""
        if snap.ng_storage_change_bcf is not None:
            sign = "+" if snap.ng_storage_change_bcf >= 0 else ""
            note = "build" if snap.ng_storage_change_bcf >= 0 else "draw"
            change_note = (
                f" ({sign}{snap.ng_storage_change_bcf:.0f} Bcf {note} vs prev week)"
            )
        gas_lines.append(
            f"Working gas in storage (Lower 48): "
            f"{snap.ng_storage_bcf:.0f} Bcf"
            f"{change_note} as of {snap.ng_storage_date or '?'}"
        )

    blocks: list[str] = []
    if petroleum_lines:
        blocks.append(
            "\n".join(
                ["EIA Weekly Petroleum (Wednesday update):"] + petroleum_lines
            )
        )
    if gas_lines:
        blocks.append(
            "\n".join(["EIA Weekly Natural Gas (Thursday update):"] + gas_lines)
        )
    if not blocks:
        return None
    return "\n\n".join(blocks)
