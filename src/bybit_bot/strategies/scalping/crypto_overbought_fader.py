"""Crypto Overbought Fader (COF) — ансамбль SHORT-mean-reversion.

═══════════════════════════════════════════════════════════════════════════
ПРОИСХОЖДЕНИЕ ГИПОТЕЗЫ (BUILDLOG_BYBIT.md, 2026-04-23)
═══════════════════════════════════════════════════════════════════════════

Скрипт `scripts/mine_ensembles.py` показал: когда сигналы `scalp_turtle`
(fade 20-бар пробоя) и `scalp_vwap` (mean-reversion к VWAP) **оба** срабатывают
в одну сторону на одном символе в окне ±15 мин — edge резко возрастает.

Ансамблевый анализ 90-дневной истории (6279 сделок, 8 символов):

  Solo (одна страта):   N=4552, WR=45.3%, PF=0.65  → убыток
  Duo (две страты):     N=1592, WR=56.8%, PF=0.99  → break-even
  Duo × SHORT:          N= 789, WR=59.7%, PF=1.15  → +26% Σ на истории

Дополнительные фильтры (вариант E) подняли PF до 1.98:

  Duo × SHORT × NY-сессия × RSI14 ≥ 65 × ATR% ≥ 0.3:
    N=139, WR=66.2%, EXP=+0.264%/trade, PF=1.98, Σ=+36.7%
    Прибыльных недель: 69.2% (9 из 13)
    Max loss streak: 1 неделя подряд
    OOS (последние 30 дней): PF=2.05 (даже выше TRAIN 1.97 — не overfit)

Обе страты-источника (turtle/vwap) индивидуально **убыточны** (PF 0.74-0.78
на истории). Edge возникает именно от согласия: вход ТОЛЬКО когда обе
согласны в шорт на перекупленном рынке.

═══════════════════════════════════════════════════════════════════════════
ЛОГИКА ENTRY
═══════════════════════════════════════════════════════════════════════════

Стратегия включает ОБЕ механики внутри себя (без зависимости от того,
включены ли turtle/vwap как отдельные страты), и требует совпадения:

1. **Turtle-Short-сигнал** (fade восходящего пробоя):
   - trap-бар в окне 4 последних сделал new 20-bar HIGH (> hist_high + 0.3×ATR)
   - последующий close вернулся ниже hist_high - 0.1×ATR
   - RSI на момент пробоя > 70

2. **VWAP-Short-сигнал** (отклонение вверх от VWAP):
   - price > VWAP + 2.0×ATR
   - RSI14 > 70
   - 1h-EMA50-slope не сильно вверх (<= +0.0005)

3. **COF-фильтры** (Variant E):
   - **Сессия NY**: 13:00–21:00 UTC
   - **RSI14 ≥ 65** на момент входа (overbought — не обязательно >70 как у
     компонентов, но хотя бы 65)
   - **ATR% ≥ 0.3**: ATR14 / price * 100 >= 0.3 — достаточная волатильность
     чтобы TP успел отработать

4. **ADX ≤ 30** — фильтр унаследован от turtle (выше = тренд, reversion плохо
   работает)

**Направление: ТОЛЬКО SHORT.** LONG-вариант дал отрицательный EXP на истории.

═══════════════════════════════════════════════════════════════════════════
ЛОГИКА EXIT
═══════════════════════════════════════════════════════════════════════════

Простой ATR-based exit (как у turtle, где на истории работало):
  - SL = entry + 1.5 × ATR (SHORT → SL выше)
  - TP = entry - 2.5 × ATR (RR ≈ 1.67)
  - Time-stop: в executor глобально (2 часа, см. settings.scalping_time_stop_hours)

═══════════════════════════════════════════════════════════════════════════
ИЗОЛЯЦИЯ
═══════════════════════════════════════════════════════════════════════════

Модуль импортирует только `bybit_bot.*`. Без внешних зависимостей.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone

from bybit_bot.analysis.signals import Direction, atr, ema, rsi
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx, ema_slope, vwap

log = logging.getLogger(__name__)

# ── Параметры (зафиксированы, изменение требует одобрения пользователя) ──

# Turtle-ногa
T_LOOKBACK = 20
T_BREAK_DEPTH_ATR = 0.3
T_RECLAIM_WINDOW = 4
T_RECLAIM_BUFFER_ATR = 0.1
T_RSI_OVERBOUGHT = 70.0

# VWAP-нога
V_DEVIATION_THRESHOLD = 2.0
V_VWAP_WINDOW = 50
V_RSI_CONFIRM_HIGH = 70
V_HTF_SLOPE_FLAT = 0.0005

# Общие
ADX_MAX = 30.0
ATR_PERIOD = 14
MIN_BARS = 80  # max(50 для vwap, 50 для rsi+adx+lookback+reclaim)

# COF-специфичные фильтры (Variant E)
COF_RSI_MIN = 65.0
COF_ATR_PCT_MIN = 0.3           # ATR / price * 100
COF_SESSION_HOURS = range(13, 21)  # NY: 13:00–20:59 UTC

# Exit (как у turtle, там работало)
SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.5


@dataclass(frozen=True, slots=True)
class CofSignal:
    """Сигнал Crypto Overbought Fader — всегда SHORT."""
    symbol: str
    direction: Direction  # всегда SHORT
    entry_price: float
    atr_value: float
    rsi: float
    atr_pct: float
    vwap_price: float
    deviation_atr: float
    turtle_depth_atr: float


class CryptoOverboughtFaderStrategy:
    """Crypto Overbought Fader — ансамбль SHORT-mean-reversion.

    Сканирует каждый символ. Для входа должны выполниться ВСЕ условия:
    turtle-short + vwap-short + фильтры COF (сессия/RSI/ATR%).
    """

    # Ключи воронки: каждое значение — счётчик per-symbol-scan.
    # "scans" — сколько символов вообще пытались сканить (не обрезаны
    # по len(bars) < MIN_BARS+...). "passed" — сколько отдали сигнал.
    _FUNNEL_KEYS = (
        "scans",
        "outside_session",
        "low_atr_pct",
        "high_adx",
        "low_rsi",
        "vwap_short_failed",
        "turtle_short_failed",
        "passed",
    )

    def __init__(self) -> None:
        self._htf_slopes: dict[str, float] = {}
        self._funnel: dict[str, int] = {k: 0 for k in self._FUNNEL_KEYS}

    def set_htf_slopes(self, slopes: dict[str, float]) -> None:
        """Передать 1h EMA(50)-slope-ы (считаются в main.py)."""
        self._htf_slopes = slopes

    def get_funnel_and_reset(self) -> dict[str, int]:
        """Вернуть накопленные счётчики filter-funnel и обнулить.

        Используется main.py для почасового сводного лога — позволяет
        отслеживать подходит ли рыночный режим под COF без grep-парсинга
        DEBUG-логов. Не влияет на торговую логику (`sample-size.mdc`:
        технические улучшения / логирование).
        """
        snapshot = dict(self._funnel)
        for key in self._FUNNEL_KEYS:
            self._funnel[key] = 0
        return snapshot

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[CofSignal]:
        signals: list[CofSignal] = []
        for symbol, bars in bars_map.items():
            sig = self._scan_symbol(symbol, bars)
            if sig is not None:
                signals.append(sig)
        signals.sort(key=lambda s: s.deviation_atr, reverse=True)
        return signals

    def _scan_symbol(self, symbol: str, bars: list[Bar]) -> CofSignal | None:
        if len(bars) < MIN_BARS + T_LOOKBACK + T_RECLAIM_WINDOW + 1:
            return None

        self._funnel["scans"] += 1

        # ── COF-фильтр 1: сессия NY (13–21 UTC) ────────────────────────
        last = bars[-1]
        # bars timestamp может быть naive или с tz — нормализуем
        ts = last.ts
        if ts.tzinfo is None:
            hour = ts.hour
        else:
            hour = ts.astimezone(timezone.utc).hour
        if hour not in COF_SESSION_HOURS:
            self._funnel["outside_session"] += 1
            log.debug("%s COF: час %02d UTC вне NY-сессии 13-20", symbol, hour)
            return None

        atr_val = atr(bars, period=ATR_PERIOD)
        if atr_val <= 0:
            return None

        price = last.close

        # ── COF-фильтр 2: ATR% ≥ 0.3 ──────────────────────────────────
        atr_pct = atr_val / price * 100.0 if price > 0 else 0.0
        if atr_pct < COF_ATR_PCT_MIN:
            self._funnel["low_atr_pct"] += 1
            log.debug("%s COF: ATR%%=%.3f < %.2f — низкая волатильность",
                      symbol, atr_pct, COF_ATR_PCT_MIN)
            return None

        # ── Общий фильтр ADX ──────────────────────────────────────────
        adx_val = compute_adx(bars, period=14)
        if adx_val > ADX_MAX:
            self._funnel["high_adx"] += 1
            log.debug("%s COF: ADX=%.1f > %.1f — сильный тренд", symbol, adx_val, ADX_MAX)
            return None

        closes = [b.close for b in bars]
        rsi_val = rsi(closes, 14)

        # ── COF-фильтр 3: RSI14 ≥ 65 (overbought) ─────────────────────
        if rsi_val < COF_RSI_MIN:
            self._funnel["low_rsi"] += 1
            log.debug("%s COF: RSI=%.1f < %.1f — не overbought", symbol, rsi_val, COF_RSI_MIN)
            return None

        # ── VWAP-short сигнал ─────────────────────────────────────────
        vwap_val = vwap(bars[-V_VWAP_WINDOW:])
        deviation = (price - vwap_val) / atr_val
        vwap_short_ok = (
            deviation > V_DEVIATION_THRESHOLD
            and rsi_val > V_RSI_CONFIRM_HIGH
        )
        if vwap_short_ok:
            htf_slope = self._htf_slopes.get(symbol)
            if htf_slope is not None and htf_slope > V_HTF_SLOPE_FLAT:
                log.debug("%s COF: HTF slope=%.6f > +%.6f (up) — блокируем VWAP-short",
                          symbol, htf_slope, V_HTF_SLOPE_FLAT)
                vwap_short_ok = False

        if not vwap_short_ok:
            self._funnel["vwap_short_failed"] += 1
            log.debug("%s COF: VWAP-short не выполнен (dev=%.2f, rsi=%.1f)",
                      symbol, deviation, rsi_val)
            return None

        # ── Turtle-short сигнал ───────────────────────────────────────
        turtle_depth = self._turtle_short_signal(bars, atr_val)
        if turtle_depth is None:
            self._funnel["turtle_short_failed"] += 1
            log.debug("%s COF: Turtle-short не выполнен", symbol)
            return None

        # Оба сигнала согласны + все фильтры прошли
        self._funnel["passed"] += 1
        return CofSignal(
            symbol=symbol,
            direction=Direction.SHORT,
            entry_price=price,
            atr_value=atr_val,
            rsi=rsi_val,
            atr_pct=atr_pct,
            vwap_price=vwap_val,
            deviation_atr=abs(deviation),
            turtle_depth_atr=turtle_depth,
        )

    def _turtle_short_signal(self, bars: list[Bar], atr_val: float) -> float | None:
        """Проверяет Turtle-SHORT (fade восходящего пробоя).
        Возвращает depth_atr ложного пробоя, или None если сигнала нет."""
        break_filter = T_BREAK_DEPTH_ATR * atr_val
        reclaim_buf = T_RECLAIM_BUFFER_ATR * atr_val
        last = bars[-1]
        trap_zone = bars[-(T_RECLAIM_WINDOW + 1):-1]
        for trap_idx, trap_bar in enumerate(trap_zone):
            global_trap_idx = len(bars) - (T_RECLAIM_WINDOW + 1) + trap_idx
            history = bars[global_trap_idx - T_LOOKBACK:global_trap_idx]
            if len(history) < T_LOOKBACK:
                continue
            hist_high = max(b.high for b in history)
            rsi_at_break = rsi([b.close for b in bars[:global_trap_idx + 1]])
            if (
                trap_bar.high > hist_high + break_filter
                and rsi_at_break > T_RSI_OVERBOUGHT
                and last.close < hist_high - reclaim_buf
            ):
                return round((trap_bar.high - hist_high) / atr_val, 2)
        return None
