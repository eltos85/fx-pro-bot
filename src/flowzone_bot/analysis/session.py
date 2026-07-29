"""Session gate flowzone_bot (STRATEGY §6.1).

Канон (C4, «The Only Orderflow Guide» 28:54): торгуем и строим профиль по
**ОДНОЙ** сессии — той, где проходит основной объём. *«I only trade in the New
York session for US indices because it's where the majority of the volume get
traded and I find it from statistical validation the London session to be
usually for US indices not so valuable to add to the profile. So I only use the
cash session profile.»* Высокая ликвидность нужна, чтобы absorption и big
trades были читаемы; ВНЕ сессии поток разрежен → методика НЕ применяется
(§6.1, §8 анти-канон).

Для крипты (24/7, «cash session» не определена) окно выбрано измерением
оборота, а не аналогией с US indices: ``scripts/flowzone_session_volume.py`` —
NY 12:00–21:00 UTC несёт 51.4% оборота за 9ч против 46.8% у London 07:00–16:00
(1000 часовых баров ≈41 день, BTC/ETH/SOL). До 2026-07-29 окна London и NY
склеивались в блок 07:00–21:00; это давало 14-часовой профиль вместо
сессионного и противоречило канону.

Модуль по-прежнему поддерживает несколько окон (``merged_segments``,
переход через полночь) — это операционная механика, настраиваемая через env
``FLOWZONE_SESSION_WINDOWS_UTC``. Функции чистые, тестируются на фикстурах.
"""
from __future__ import annotations

import calendar
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


def merged_segments(windows: list[tuple[float, float]]
                    ) -> list[tuple[float, float]]:
    """Объединить перекрывающиеся/смежные окна в непрерывные активные блоки
    (часы UTC, [start, end) на суточном круге). Окно через полночь режется на
    два сегмента (start..24 и 0..end). Пример: London 07-16 + NY 12-21 →
    один блок (7.0, 21.0)."""
    segs: list[tuple[float, float]] = []
    for start, end in windows:
        if start == end:
            continue  # пустое окно
        if start < end:
            segs.append((start, end))
        else:  # через полночь
            segs.append((start, 24.0))
            segs.append((0.0, end))
    segs.sort()
    merged: list[list[float]] = []
    for s, e in segs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _day_ts(tm: time.struct_time, start_hour: float) -> float:
    h = int(start_hour)
    m = int(round((start_hour - h) * 60))
    return calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, h, m, 0, 0, 0, 0))


def session_start_ts(ts: float, windows: list[tuple[float, float]]) -> float | None:
    """Unix-старт текущего НЕПРЕРЫВНОГО активного блока сессий для ``ts`` (UTC).

    Канон §2/§6.1: контекст = форма СЕССИОННОГО профиля. Якорь per-session
    профиля — старт непрерывного активного блока (union перекрывающихся окон):
    для London 07-16 + NY 12-21 это 07:00 на весь день до 21:00. Раньше якорь
    брался от ПЕРВОГО совпавшего окна: в 16:00 он прыгал 07:00 → 12:00, профиль
    обнулялся (терялся объём перекрытия 12-16) и контекст ежедневно уходил в
    warming посреди NY. Возвращает None, если ``ts`` вне активной сессии
    (профиль не строим — не торгуем).

    Блок через полночь корректен: если час < end сегмента, начинающегося с
    0:00, и есть сегмент, кончающийся в 24:00 — старт был вчера."""
    if not windows:
        return None
    segs = merged_segments(windows)
    if not segs:
        return None
    tm = time.gmtime(ts)
    hour = tm.tm_hour + tm.tm_min / 60.0 + tm.tm_sec / 3600.0
    wraps_from = next((s for s, e in segs if e >= 24.0), None)
    for start, end in segs:
        if not (start <= hour < end):
            continue
        # сегмент от полуночи, склеенный с вчерашним хвостом (22-24 + 0-02):
        # старт блока — вчера, в начале хвостового сегмента.
        if start <= 0.0 and wraps_from is not None and wraps_from > 0.0:
            prev = time.gmtime(ts - 86400.0)
            return _day_ts(prev, wraps_from)
        return _day_ts(tm, start)
    return None
