"""VWAP Mean-Reversion для крипто-скальпинга.

Цена стремится вернуться к VWAP (~70-75% времени).
Вход: отклонение > DEVIATION_THRESHOLD * ATR + RSI подтверждение + фильтр ADX.

─── Research basis (exit) ──────────────────────────────────────────────
SL: 2.0 ATR, TP: 3.0 ATR  →  RR 1:1.5

Источники для crypto VWAP mean-reversion:
  • Sword Red BTC (FMZQuant 2024) — RR 1:1.5 на BTC-perp.
  • FMZQuant ETH-perp study — RR 1:1.5 как оптимум по 90-дневной выборке.
  • BYBIT_AB_TEST.md "RESEARCH REFERENCE" 2026-04-23.

История параметра:
  • До 2026-04-28: tp=1.5 ATR (RR 1:0.75). Задокументировано в
    BYBIT_AB_TEST.md как "🟡 ниже нормы". WIFUSDT 27.04 (n=1) ушёл в SL
    с RR 0.64 — согласуется с теоретическим минимумом этой связки.
  • 2026-04-28: повышено до 3.0 ATR (RR 1:1.5) под research-anchor.
  • Параметры заданы в bybit_bot.app.main как константы _VWAP_SL_ATR_MULT
    / _VWAP_TP_ATR_MULT (применяются к Signal при конструировании из
    VwapSignal). Исполнитель — TradeExecutor.compute_trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC

from bybit_bot.analysis.signals import Direction, atr, ema, rsi
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx, ema_slope, vwap

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 2.0
RSI_CONFIRM_LOW = 30
RSI_CONFIRM_HIGH = 70
# ADX < 25 = допустимо для mean reversion (PyQuantLab 2025: 108 конфигураций).
# Крипта редко даёт ADX < 20, зона 20-25 приемлема. > 25 = сильный тренд.
ADX_MAX = 25.0

# Мягкий HTF slope-фильтр: блокируем вход только против СИЛЬНОГО старшего тренда.
# |slope| < HTF_SLOPE_FLAT → считаем боковиком, mean-reversion работает в обе стороны.
# |slope| >= HTF_SLOPE_FLAT → запрещаем контр-трендовое направление.
# 0.0005 ≈ 0.05% за 1h бар → мягче, чем полный запрет. Локальный 5m slope
# отключён — он слишком шумный и режет почти все MR-сигналы на трендовых
# участках, хотя именно там MR часто отрабатывает.
HTF_SLOPE_FLAT = 0.0005


@dataclass(frozen=True, slots=True)
class VwapSignal:
    symbol: str
    direction: Direction
    deviation_atr: float
    rsi: float
    vwap_price: float
    atr_value: float
    entry_price: float


class VwapCryptoStrategy:
    """VWAP Mean-Reversion скальпинг для крипто.

    Rolling VWAP по последним 50 барам (без привязки к FX-сессиям).
    Фильтр: ADX ≤ 20 (боковик), RSI подтверждение, EMA slope.
    HTF фильтр: 1h EMA(50) slope определяет разрешённое направление.
    """

    def __init__(
        self,
        *,
        allowed_direction: str | None = None,
        allowed_symbols: set[str] | None = None,
        allowed_hours_utc: set[int] | None = None,
        allowed_weekdays: set[int] | None = None,
    ) -> None:
        """Параметры-ограничители для Wave 6 (BUILDLOG.md 2026-04-25).

        Whitelist'ы выведены по двум независимым источникам:
        Bybit API closedPnl 11-23.04 (n=636) и 90-day backtest (n=126
        в выбранном сегменте; OOS TEST PF=1.26 +w%=80%).

        - ``allowed_direction`` — "long"/"short"/None.
          В выбранном сегменте LONG: PF 1.20 +11.06%, SHORT: PF 0.97 −1.84%.
        - ``allowed_symbols`` — whitelist символов, None=все.
          Топ-5 LONG в prime-окне: ADAUSDT, SOLUSDT, SUIUSDT, TONUSDT, WIFUSDT.
          TIAUSDT/DOTUSDT/LINKUSDT — отрицательны в этом сегменте.
        - ``allowed_hours_utc`` — set часов UTC, в которые разрешены входы.
          {14,15,16,19,20} (=17-19 и 22-23 МСК) — единственный профитный
          кластер по live-данным (+$25.94 на n=102 при WR 60.8%).
          17-18 UTC исключены: концентрат поздне-NY убытков (-$116 на n=113,
          alt-selloff zone, см. BYBIT_AB_TEST.md OBSERVATION 2026-04-22).
        - ``allowed_weekdays`` — set дней недели (0=Mon..6=Sun).
          Будни {0,1,2,3,4} — Sat+Sun дают −$201 vs −$148 на буднях.

        Лимит параллельных позиций проверяется в app/main.py через
        settings.scalping_max_positions, стратегия его не хранит.
        """
        self._allowed_direction = allowed_direction
        self._allowed_symbols = allowed_symbols
        self._allowed_hours_utc = allowed_hours_utc
        self._allowed_weekdays = allowed_weekdays
        self._htf_slopes: dict[str, float] = {}

    def set_htf_slopes(self, slopes: dict[str, float]) -> None:
        """Задать 1h EMA(50) slope для каждого символа (рассчитывается в main.py)."""
        self._htf_slopes = slopes

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[VwapSignal]:
        signals: list[VwapSignal] = []

        for symbol, bars in bars_map.items():
            if len(bars) < 51:
                continue

            if self._allowed_symbols is not None and symbol not in self._allowed_symbols:
                continue

            if not self._is_active_time(bars[-1]):
                continue

            price = bars[-1].close
            atr_val = atr(bars)
            if atr_val <= 0:
                continue

            adx = compute_adx(bars)
            if adx > ADX_MAX:
                log.debug("%s: ADX=%.1f > %.1f, пропускаю (тренд)", symbol, adx, ADX_MAX)
                continue

            vwap_val = vwap(bars[-50:])
            deviation = (price - vwap_val) / atr_val

            closes = [b.close for b in bars]
            rsi_val = rsi(closes, 14)
            ema_vals = ema(closes, 50)
            slope = ema_slope(ema_vals, 5)

            log.debug(
                "%s: VWAP=%.4f price=%.4f dev=%.2f ATR, ADX=%.1f, RSI=%.1f, slope=%.6f",
                symbol, vwap_val, price, deviation, adx, rsi_val, slope,
            )

            htf_slope = self._htf_slopes.get(symbol)

            if deviation < -DEVIATION_THRESHOLD and rsi_val < RSI_CONFIRM_LOW:
                if self._allowed_direction is not None and self._allowed_direction != "long":
                    continue
                # Мягкий HTF-фильтр: не открываем LONG, если 1h тренд сильно вниз.
                # Локальный 5m slope отключён — он слишком шумный.
                if htf_slope is not None and htf_slope < -HTF_SLOPE_FLAT:
                    log.debug("%s LONG: HTF slope=%.6f < -%.6f (сильный down), пропуск",
                              symbol, htf_slope, HTF_SLOPE_FLAT)
                    continue
                signals.append(VwapSignal(
                    symbol=symbol,
                    direction=Direction.LONG,
                    deviation_atr=abs(deviation),
                    rsi=rsi_val,
                    vwap_price=vwap_val,
                    atr_value=atr_val,
                    entry_price=price,
                ))

            elif deviation > DEVIATION_THRESHOLD and rsi_val > RSI_CONFIRM_HIGH:
                if self._allowed_direction is not None and self._allowed_direction != "short":
                    continue
                # Не открываем SHORT, если 1h тренд сильно вверх.
                if htf_slope is not None and htf_slope > HTF_SLOPE_FLAT:
                    log.debug("%s SHORT: HTF slope=%.6f > +%.6f (сильный up), пропуск",
                              symbol, htf_slope, HTF_SLOPE_FLAT)
                    continue
                signals.append(VwapSignal(
                    symbol=symbol,
                    direction=Direction.SHORT,
                    deviation_atr=abs(deviation),
                    rsi=rsi_val,
                    vwap_price=vwap_val,
                    atr_value=atr_val,
                    entry_price=price,
                ))

        return signals

    def _is_active_time(self, last_bar: Bar) -> bool:
        """Проверка фильтра дня недели и часа UTC по последнему бару.

        Берём время из бара, а не `datetime.now()` — для детерминизма в
        тестах и совпадения с временем сигнала. Бары приходят с биржи
        в UTC; если tzinfo пустой, считаем UTC.
        """
        if self._allowed_weekdays is None and self._allowed_hours_utc is None:
            return True
        ts = last_bar.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if self._allowed_weekdays is not None and ts.weekday() not in self._allowed_weekdays:
            return False
        if self._allowed_hours_utc is not None and ts.hour not in self._allowed_hours_utc:
            return False
        return True
