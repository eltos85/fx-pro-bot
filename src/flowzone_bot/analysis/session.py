"""Session gate flowzone_bot (STRATEGY §6.1).

Канон: входы привязаны к активным сессиям — **London** и **New York**. Высокая
ликвидность сессий нужна, чтобы absorption и big trades были читаемы; ВНЕ
активных сессий поток разрежен → методика НЕ применяется (§6.1, §8 анти-канон).

Окна заданы в UTC (биржа Bybit — UTC). Каноничные определения сессий FX
(BIS Triennial Survey; Investopedia «Forex Trading Sessions»):
- **London** ≈ 07:00–16:00 UTC.
- **New York** ≈ 12:00–21:00 UTC (перекрытие с London 12:00–16:00 — пик
  ликвидности).

Окна — операционные (не торговый эдж-порог), настраиваются через env
``FLOWZONE_SESSION_WINDOWS_UTC``. Функции чистые, тестируются на фикстурах.
"""
from __future__ import annotations

import time


def parse_windows(spec: str) -> list[tuple[float, float]]:
    """Разобрать "HH:MM-HH:MM,HH:MM-HH:MM" в список (start_hour, end_hour) в
    часах-с-дробью UTC. Некорректные сегменты пропускаются."""
    windows: list[tuple[float, float]] = []
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg or "-" not in seg:
            continue
        a, _, b = seg.partition("-")
        try:
            start = _to_hours(a)
            end = _to_hours(b)
        except ValueError:
            continue
        windows.append((start, end))
    return windows


def _to_hours(hhmm: str) -> float:
    parts = hhmm.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(hhmm)
    return h + m / 60.0


def in_session(ts: float, windows: list[tuple[float, float]]) -> bool:
    """В активной сессии ли момент ``ts`` (unix UTC). Окно с end ≤ start
    трактуется как переход через полночь. Пустой список окон → всегда True
    (гейт фактически выключен)."""
    if not windows:
        return True
    tm = time.gmtime(ts)
    hour = tm.tm_hour + tm.tm_min / 60.0 + tm.tm_sec / 3600.0
    for start, end in windows:
        if start <= end:
            if start <= hour < end:
                return True
        else:  # окно через полночь (напр. 22:00-02:00)
            if hour >= start or hour < end:
                return True
    return False
