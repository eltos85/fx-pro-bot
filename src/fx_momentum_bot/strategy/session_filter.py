"""Session-фильтр: блок НОВЫХ входов вне ликвидных FX-сессий.

Контекст (BUILDLOG 2026-06-26, tradecard_momentum weekly, cTrader deal-list
ground truth, 77 сделок 2026-06-01..26): Asian session (00–07 UTC) — 0% WR
по GBPUSD (n=6, −$60) и AUDUSD (n=8, −$49), суммарно −$109 при нулевой
победе; при этом NY session по AUDUSD = 60% WR, +$45. Тонкая ликвидность
Asia превращает momentum-сигналы в ложные: вход на импульсе, который
разворачивается на малом объёме. У momentum-бота liquid-session фильтра
не было — данные подтвердили его отсутствие эмпирически.

─── Research basis ───
- STRATEGIES.md стр.173 (Liquid session filter, канон для FX): «вход только
  London 07:00–15:59 UTC или NY 12:00–20:59 UTC. В Asian session и в час
  NY close тонкая ликвидность превращает mean-reversion в ловлю падающего
  ножа». Тот же механизм для momentum: ложный импульс на малом объёме.
- Lyons «The Microstructure Approach to Exchange Rates» (2001, ch.3–4):
  FX-ликвидность и информационная эффективность концентрируются в
  overlapping London/NY; Asia — доминируют институциональные кэрри-потоки
  и тихие сессии, momentum-сигналы слабее и шумовее.
- BIS triennial / Andersen et al. (2003): пики волатильности и объёма —
  London open + NY overlap; вне них spread шире, R-multiple ожидание ниже.

Блокируются только ВХОДЫ. Сопровождение (BE/partial/trailing), sign-decay
выход и SL продолжают работать — канон управления риском важнее канона
входа (тот же принцип что event_guard.py). Это фильтр ликвидности, НЕ
торговый параметр (threshold/ATR/lookback не трогает) — обратим через env.

Диапазон по умолчанию [07, 21) UTC покрывает London (07–12) + NY (12–21).
Late (21–24) и Asia (00–07) исключаются. Час входа = hour_utc закрытого
бара, по которому взят сигнал (не текущее время цикла) — соответствует
логике _drop_forming_bar (сигнал на close бара).
"""
from __future__ import annotations

from datetime import datetime, timezone


def session_skip_reason(
    *,
    hour_utc: int,
    enabled: bool,
    start_hour_utc: int,
    end_hour_utc: int,
) -> str | None:
    """Причина скипа входа вне ликвидной сессии, либо None (вход разрешён).

    None == вход разрешён. Строка == вход блокируется (текст для лога).
    Диапазон [start, end) полуоткрытый: 07..21 → часы 7..20 включительно.
    enabled=False или вырожденный диапазон (start==end) → фильтр выключен.
    """
    if not enabled:
        return None
    if start_hour_utc == end_hour_utc:
        return None
    if start_hour_utc < end_hour_utc:
        in_window = start_hour_utc <= hour_utc < end_hour_utc
    else:
        # Обёртка через полночь (на случай если зададут night-only диапазон).
        in_window = hour_utc >= start_hour_utc or hour_utc < end_hour_utc
    if in_window:
        return None
    return f"off-session(h={hour_utc:02d}UTC, liquid=[{start_hour_utc:02d},{end_hour_utc:02d}))"


def current_hour_utc(now: datetime | None = None) -> int:
    """Текущий час UTC (для логов/тестов; точка входа использует час сигнала)."""
    return (now or datetime.now(timezone.utc)).hour
