"""Economic-calendar / event-proximity feed — Enhancement E (2026-05-29).

Бот не знал о предстоящих high-impact релизах. SYSTEM_PROMPT прямо
требует «scale to half size through FOMC release», но без календаря это
было невозможно соблюсти. Модуль вычисляет ближайшие события и подаёт
proximity (через сколько часов, какие символы затронуты). LLM сам решает,
резать ли размер / ждать — мы только сообщаем факт близости.

Источники дат (api-docs.mdc — официальные календари):
- FOMC 2026 (8 заседаний; decision day = второй день, 14:00 ET):
  federalreserve.gov/newsevents/pressreleases/monetary20240809a.htm
- CPI 2026 (08:30 ET): bls.gov/schedule/news_release/cpi.htm
- EIA Weekly Petroleum Status: среда 10:30 ET;
  EIA Natural Gas Storage: четверг 10:30 ET (eia.gov release schedule).
- US Nonfarm Payrolls (NFP): первая пятница месяца 08:30 ET (BLS).

Время релизов хранится в ET и конвертируется в UTC через zoneinfo
(America/New_York) — корректный учёт DST без magic-offset'ов.

Примечание (no-data-fitting): это НЕ торговый параметр, а фактический
календарь. Recurring-события (EIA/NFP) выводятся из правил; FOMC/CPI —
статический sourced-список на 2026 (по исчерпании graceful-degrade на
одни recurring).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# FOMC 2026 decision days (Wednesday — второй день заседания), 14:00 ET.
# Источник: federalreserve.gov press release 2024-08-09.
_FOMC_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
]
# CPI 2026 release dates, 08:30 ET. Источник: bls.gov CPI schedule.
_CPI_2026 = [
    date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11),
    date(2026, 4, 10), date(2026, 5, 12), date(2026, 6, 10),
    date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 11),
    date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10),
]


@dataclass
class EconEvent:
    name: str
    when_utc: datetime
    impact: str  # "HIGH" | "MED"
    symbols: tuple[str, ...]  # () = all symbols

    def affects(self, symbol: str) -> bool:
        return not self.symbols or symbol in self.symbols

    def hours_until(self, now_utc: datetime) -> float:
        return (self.when_utc - now_utc).total_seconds() / 3600.0


def _et_to_utc(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=_ET).astimezone(_UTC)


def _next_weekday_et(now_utc: datetime, weekday: int, hh: int, mm: int) -> datetime:
    """Ближайшая (>= now) дата-время на заданный weekday в ET → UTC.

    weekday: Mon=0 … Sun=6.
    """
    now_et = now_utc.astimezone(_ET)
    days_ahead = (weekday - now_et.weekday()) % 7
    candidate = _et_to_utc(now_et.date() + timedelta(days=days_ahead), hh, mm)
    if candidate < now_utc:
        candidate = _et_to_utc(
            now_et.date() + timedelta(days=days_ahead + 7), hh, mm
        )
    return candidate


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    # Fri = weekday 4
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _next_nfp(now_utc: datetime) -> datetime:
    """NFP: первая пятница месяца, 08:30 ET. Возвращает ближайшую >= now."""
    now_et = now_utc.astimezone(_ET)
    ff = _first_friday(now_et.year, now_et.month)
    when = _et_to_utc(ff, 8, 30)
    if when < now_utc:
        nxt_month = now_et.month % 12 + 1
        nxt_year = now_et.year + (1 if now_et.month == 12 else 0)
        ff = _first_friday(nxt_year, nxt_month)
        when = _et_to_utc(ff, 8, 30)
    return when


def upcoming_events(
    now_utc: datetime, symbols: tuple[str, ...], horizon_hours: float = 168.0
) -> list[EconEvent]:
    """Все события в окне [now, now+horizon], затрагивающие symbols, sorted."""
    events: list[EconEvent] = []

    # Recurring (rule-based).
    events.append(
        EconEvent("US NFP (nonfarm payrolls)", _next_nfp(now_utc), "HIGH", ())
    )
    if "BZ=F" in symbols:
        events.append(
            EconEvent(
                "EIA Weekly Petroleum Status",
                _next_weekday_et(now_utc, 2, 10, 30),  # Wed 10:30 ET
                "MED", ("BZ=F",),
            )
        )
    if "NG=F" in symbols:
        events.append(
            EconEvent(
                "EIA Natural Gas Storage",
                _next_weekday_et(now_utc, 3, 10, 30),  # Thu 10:30 ET
                "MED", ("NG=F",),
            )
        )

    # Static sourced (FOMC / CPI).
    for d in _FOMC_2026:
        when = _et_to_utc(d, 14, 0)
        if when >= now_utc:
            events.append(EconEvent("FOMC decision", when, "HIGH", ()))
            break
    for d in _CPI_2026:
        when = _et_to_utc(d, 8, 30)
        if when >= now_utc:
            events.append(EconEvent("US CPI", when, "HIGH", ()))
            break

    horizon = now_utc + timedelta(hours=horizon_hours)
    out = [
        e
        for e in events
        if now_utc <= e.when_utc <= horizon
        and any(e.affects(s) for s in symbols)
    ]
    out.sort(key=lambda e: e.when_utc)
    return out


class EconCalendarProvider:
    """Pure-compute провайдер (без сети). horizon_hours — окно проксимити."""

    def __init__(self, horizon_hours: float = 168.0) -> None:
        self._horizon = horizon_hours

    @property
    def enabled(self) -> bool:
        return True

    def get_block(
        self, symbols: tuple[str, ...], now_utc: datetime | None = None
    ) -> str | None:
        now = now_utc or datetime.now(_UTC)
        events = upcoming_events(now, symbols, self._horizon)
        return format_econ_events(events, now)


def format_econ_events(
    events: list[EconEvent], now_utc: datetime
) -> str | None:
    """Text-блок календаря для LLM. None если в окне ничего нет."""
    if not events:
        return None
    lines = [
        "=== ECONOMIC CALENDAR (upcoming high-impact releases; scale size / "
        "avoid fresh entries into HIGH-impact events) ==="
    ]
    for e in events:
        h = e.hours_until(now_utc)
        eta = f"{h:.1f}h" if h >= 1 else f"{h * 60:.0f}min"
        scope = "all" if not e.symbols else ",".join(e.symbols)
        lines.append(
            f"[{e.impact}] {e.name} in {eta} "
            f"({e.when_utc:%Y-%m-%d %H:%M} UTC; affects {scope})"
        )
    return "\n".join(lines)
