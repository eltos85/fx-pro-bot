"""Friday-flat: принудительное закрытие momentum-позиций перед выходными.

Контекст (BUILDLOG 2026-06-26, tradecard_momentum, cTrader deal-list ground
truth, 77 сделок 2026-06-01..26): сделки, пережившие выходные, — avgR −0.79
против −0.08 внутри недели, WR 11% (1 из 9), net −$51; входы в пятницу —
0% WR (13 сделок, −$73). Своп за выходные копеечный (−$0.74) — убыток даёт
гэп понедельника, исполняющий SL вне планового 1R.

Предыстория: в BUILDLOG 2026-06-11 friday-flat был введён ТОЛЬКО для VP
(day-timeframe, Dalton 2007), а momentum намеренно оставили держать через
выходные — как трендовый канон Turtle/TSMOM. Но: (1) эмпирика avgR −0.79
опровергает «гэпы в сторону тренда» для текущего режима; (2) канон TSMOM
справедлив для continuous-market (commodities/index futures 24/7), а FX spot
разрывается Сб/Вс → понедельничный гэп = чистый informational gap без
price discovery, SL исполняется по первой доступной цене вне 1R. Поэтому
для FX-only momentum friday-flat теперь применяется.

─── Research basis ───
- Dalton «Mind Over Markets» (2007): день-таймфрейм профиль описывает
  сессию; через выходные позиция переезжает в «другой рынок» — профиль
  понедельника не описывает рынок пятницы. Та же аргументация, что для
  VP friday-flat (BUILDLOG 2026-06-11).
- Lyons «The Microstructure Approach to Exchange Rates» (2001): FX
  price discovery концентрируется в London/NY overlap; закрытие рынка
  Сб/Вс = информационный разрыв, понедельничный open gap не компенсирует
  непрерывным рынком, как в futures.
- Andersen/Bollerslev/Diebold/Vega (2003): запланированные разрывы сессии
  концентрируют волатильность на reopen — SL проскальзывает.

Закрываются ВСЕ открытые momentum-позиции в пятницу в окне
[flat_start, flat_end) UTC (по умолчанию 20:00–20:45 — до FX weekly close
~21:00 UTC летом). Retry в следующем цикле при неудаче (MARKET_CLOSED
дедупится в main). Сопровождение (BE/partial/trailing) и sign-decay
продолжают работать до окна flat; в окне flat приоритет у принудительного
close. Обратимо: enabled=False или вырожденное окно (start==end).
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse_hhmm(s: str) -> tuple[int, int]:
    """'HH:MM' → (hours, minutes). Поднимает ValueError при плохом формате."""
    h, m = s.split(":", 1)
    return int(h), int(m)


def friday_flat_due(
    *,
    enabled: bool,
    flat_start: str,
    flat_end: str,
    now_utc: datetime | None = None,
) -> bool:
    """True, если пора закрывать momentum-позиции перед выходными.

    True только в пятницу UTC в окне [flat_start, flat_end). Вырожденное
    окно (start==end) или enabled=False → False (правило выключено).
    Сбой парсинга конфига → False (правило выключается, не блокирует).
    """
    if not enabled:
        return False
    try:
        start_h, start_m = _parse_hhmm(flat_start)
        end_h, end_m = _parse_hhmm(flat_end)
    except Exception:
        return False
    if (start_h, start_m) == (end_h, end_m):
        return False
    now = now_utc or datetime.now(timezone.utc)
    if now.weekday() != 4:  # Friday
        return False
    now_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    return start_minutes <= now_minutes < end_minutes
