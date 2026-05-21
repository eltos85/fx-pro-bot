"""NOAA CPC 6-10 / 8-14 day temperature & precipitation outlook fetcher.

Источник: NOAA Climate Prediction Center
``https://www.cpc.ncep.noaa.gov/products/predictions/6-10_day/fxus06.html``

Это **критический** weather-driver для NG=F (Natural Gas) trading: HDD
(heating degree days) Oct–Mar и CDD (cooling degree days) Jun–Aug
определяют residential/commercial и power-generation demand. Сдвиг
прогноза CPC на 4°F = 5%+ движение NG futures за 48ч (industry rule
of thumb). См. промпт LLM, NG framework, пункты 2 (WEATHER) и в
секции NG MISTAKES TO AVOID «Ignoring weather feed».

Что возвращаем:
- Prognostic discussion (текст, обычно 6-10 day и 8-14 day разделы):
  full text с описанием температурных аномалий по регионам
  CONUS (US continental) + Hawaii + Alaska. ~3-5 KB полезного текста.
- Cache TTL по умолчанию 6 часов (CPC update раз в день 15:00-16:00 ET).

Источник публикации: CPC issues discussion daily ~15:00 ET (19-20 UTC).
Discussion text живёт между маркерами ``Prognostic Discussion`` и
``FORECAST CONFIDENCE``, оба раздела (6-10 и 8-14 day) встречаются в
одном HTML.

Запрос timeout 15s, без HTTP retry — на failure возвращаем кэш или
None (RSS news подстраховают).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


_NOAA_URL = (
    "https://www.cpc.ncep.noaa.gov/products/predictions/6-10_day/fxus06.html"
)

# Markers внутри HTML/text страницы. Discussion начинается с заголовка
# «Prognostic Discussion for 6 to 10 and 8 to 14 day outlooks», а
# заканчивается одним из FORECAST CONFIDENCE блоков. Стабильный formatting
# CPC проверен на снимках 2020–2026, см. WebFetch dump 2026-05-20.
_DISCUSSION_START_PATTERNS = (
    "Prognostic Discussion for 6 to 10 and 8 to 14",
    "Prognostic Discussion",
)
_DISCUSSION_END_PATTERNS = (
    "FORECAST CONFIDENCE FOR THE 8-14 DAY PERIOD",
    "$$",  # CPC footer marker
    "End of",
)


@dataclass
class NoaaOutlookSnapshot:
    """Параметры NOAA CPC outlook на 6-10 и 8-14 дней."""

    discussion_text: str  # сырой текст discussion, готов к подаче LLM
    fetched_at_utc: str  # ISO timestamp когда мы получили
    source_url: str = _NOAA_URL


class NoaaOutlookProvider:
    """Кэширующий клиент NOAA CPC outlook. Cache TTL = 6 часов."""

    def __init__(
        self,
        cache_ttl_sec: int = 21600,
        request_timeout_sec: int = 15,
        max_chars: int = 6000,
    ) -> None:
        self._cache_ttl = cache_ttl_sec
        self._timeout = request_timeout_sec
        self._max_chars = max_chars
        self._cache: NoaaOutlookSnapshot | None = None
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshot(self) -> NoaaOutlookSnapshot | None:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            snap = self._fetch_fresh()
        except Exception:
            log.exception("NOAA CPC fetch failed (продолжаю с прошлым кэшем)")
            return self._cache
        if snap is not None:
            self._cache = snap
            self._cache_ts = now
        return snap or self._cache

    def _fetch_fresh(self) -> NoaaOutlookSnapshot | None:
        resp = requests.get(
            _NOAA_URL,
            timeout=self._timeout,
            headers={
                "User-Agent": "fx-ai-trader/1.0 (https://github.com; market data)",
            },
        )
        resp.raise_for_status()
        raw = resp.text
        discussion = self._extract_discussion(raw)
        if not discussion:
            log.warning("NOAA CPC: discussion section not found in fetched HTML")
            return None
        # Усечь до max_chars (защита от gigantic context).
        if len(discussion) > self._max_chars:
            discussion = discussion[: self._max_chars] + "\n[... truncated ...]"
        from datetime import datetime, timezone
        return NoaaOutlookSnapshot(
            discussion_text=discussion,
            fetched_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _extract_discussion(self, html: str) -> str:
        """Достать текст discussion из HTML.

        CPC fxus06.html — это **table-wrapped** HTML, где основная
        информация — это **inline text** между HTML тагами. Простой подход:
        Strip HTML tags + regex по start/end markers.
        """
        # Strip HTML tags. Очень упрощённо — оставляем только текстовую
        # часть. CPC strips формат остаётся читаемым.
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse multiple whitespace, including \n.
        text = re.sub(r"\s+", " ", text).strip()
        # Find start marker.
        start_idx = -1
        for marker in _DISCUSSION_START_PATTERNS:
            idx = text.find(marker)
            if idx >= 0:
                start_idx = idx
                break
        if start_idx < 0:
            return ""
        # Find end marker.
        end_idx = len(text)
        for marker in _DISCUSSION_END_PATTERNS:
            idx = text.find(marker, start_idx)
            if idx > start_idx:
                end_idx = idx
                break
        snippet = text[start_idx:end_idx].strip()
        # Cleanup: restore paragraph breaks at common sentinels for
        # readability (LLM context).
        snippet = re.sub(
            r"\s+(6-10 DAY OUTLOOK|8-14 DAY OUTLOOK|FORECAST CONFIDENCE)",
            r"\n\n\1",
            snippet,
        )
        return snippet


def format_noaa_snapshot(snap: NoaaOutlookSnapshot | None) -> str | None:
    """Превратить snapshot в готовый текстовый блок для LLM-context.

    Возвращает ``None`` если данных нет.
    """
    if snap is None or not snap.discussion_text:
        return None
    return (
        f"NOAA CPC 6-10 / 8-14 day Prognostic Discussion "
        f"(fetched {snap.fetched_at_utc} UTC, source: {snap.source_url}):\n"
        f"\n{snap.discussion_text}"
    )
