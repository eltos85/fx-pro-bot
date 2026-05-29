"""GDELT news-tone feed — Enhancement D (2026-05-29).

Наши RSS-новости дают заголовки, но НЕ дают агрегированного sentiment по
всему новостному потоку. GDELT агрегирует мировые СМИ и считает «average
tone» (эмоциональная окраска) — это структурный sentiment-сигнал поверх
наших точечных заголовков. Резкий разворот тона часто опережает цену.

Что подаём (НЕ интерпретируем за LLM):
- avg_tone (среднее по окну), latest_tone (последняя точка), trend
  (улучшение/ухудшение: последняя треть окна vs первая треть).
Шкала GDELT tone: ~ −10…+10, 0 ≈ нейтрально, минус = негатив. Пороги
«сильный/слабый» НЕ зашиваем (no-data-fitting) — решает LLM.

Research basis:
- Leetaru & Schrodt (2013) «GDELT: Global Data on Events, Location and
  Tone» — методология tone-скоринга глобального новостного потока.
- Tetlock (2007, J. Finance) «Giving Content to Investor Sentiment» —
  media tone предсказывает доходности/развороты.

Источник (free, без ключа): GDELT DOC 2.0 API, mode=timelinetone
(офиц. дока: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# internal symbol → GDELT query (quoted phrases via OR, чтобы избежать шума
# одиночного слова "gold"/"gas").
_SYMBOL_TO_QUERY: dict[str, str] = {
    "XAUUSD": '("gold price" OR "gold futures" OR "gold market")',
    "BZ=F": '("brent crude" OR "crude oil" OR "oil prices")',
    "NG=F": '("natural gas price" OR "henry hub" OR "natural gas futures")',
}


@dataclass
class GdeltToneSnapshot:
    symbol: str
    avg_tone: float
    latest_tone: float
    n_points: int
    trend: str  # "improving" | "deteriorating" | "stable"


class GdeltProvider:
    """Кэширующий GDELT tone-клиент. TTL по умолчанию 3 часа."""

    def __init__(
        self,
        cache_ttl_sec: int = 10800,
        timeout: int = 20,
        timespan: str = "3d",
        request_spacing_sec: float = 1.5,
    ) -> None:
        self._cache_ttl = cache_ttl_sec
        self._timeout = timeout
        self._timespan = timespan
        # GDELT просит не «долбить» API (429 при бёрсте). Разносим
        # per-symbol запросы паузой. Кэш 3ч → в проде 3 запроса/3ч.
        self._spacing = request_spacing_sec
        self._cache: dict[str, GdeltToneSnapshot] = {}
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshots(
        self, symbols: tuple[str, ...]
    ) -> dict[str, GdeltToneSnapshot]:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return {s: self._cache[s] for s in symbols if s in self._cache}
        fresh: dict[str, GdeltToneSnapshot] = {}
        first = True
        for sym in symbols:
            query = _SYMBOL_TO_QUERY.get(sym)
            if not query:
                continue
            if not first and self._spacing > 0:
                time.sleep(self._spacing)
            first = False
            try:
                snap = self._fetch_one(sym, query)
            except Exception:
                log.exception("GDELT fetch failed для %s", sym)
                snap = self._cache.get(sym)
            if snap is not None:
                fresh[sym] = snap
        if fresh:
            self._cache = fresh
            self._cache_ts = now
        return {s: self._cache[s] for s in symbols if s in self._cache}

    def _fetch_one(self, symbol: str, query: str) -> GdeltToneSnapshot | None:
        import requests

        params = {
            "query": query,
            "mode": "timelinetone",
            "format": "json",
            "timespan": self._timespan,
        }
        resp = requests.get(_GDELT_URL, params=params, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json()
        timeline = payload.get("timeline") or []
        if not timeline:
            return None
        data = timeline[0].get("data") or []
        vals = [
            float(d["value"])
            for d in data
            if d.get("value") is not None
        ]
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        latest = vals[-1]
        trend = _classify_trend(vals)
        return GdeltToneSnapshot(
            symbol=symbol,
            avg_tone=avg,
            latest_tone=latest,
            n_points=len(vals),
            trend=trend,
        )


def _classify_trend(vals: list[float]) -> str:
    """Сравнивает среднее последней трети окна с первой третью.

    Порог 0.5 tone-пункта — НЕ торговый параметр, а шумовой фильтр для
    лейбла направления (чтобы микро-дрейф не назывался trend'ом).
    """
    if len(vals) < 6:
        return "stable"
    third = max(1, len(vals) // 3)
    first = sum(vals[:third]) / third
    last = sum(vals[-third:]) / third
    diff = last - first
    if diff > 0.5:
        return "improving"
    if diff < -0.5:
        return "deteriorating"
    return "stable"


def format_gdelt_snapshots(snaps: dict[str, GdeltToneSnapshot]) -> str | None:
    """Text-блок GDELT tone для LLM. None если нет данных."""
    if not snaps:
        return None
    lines = [
        "=== GDELT NEWS TONE (global media sentiment; scale ~−10..+10, "
        "0=neutral; reversals can lead price) ==="
    ]
    for sym, s in snaps.items():
        lines.append(
            f"[{sym}] avg={s.avg_tone:+.2f} latest={s.latest_tone:+.2f} "
            f"trend={s.trend} (n={s.n_points} pts)"
        )
    return "\n".join(lines)
