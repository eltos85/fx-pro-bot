"""Direction-telemetry flowzone_bot — НАБЛЮДАЕМОСТЬ устойчивости аукциона.

НЕ гейтит вход и НЕ меняет торговое решение (no-data-fitting.mdc /
strategy-guard.mdc: новые фильтры — только после OOS-валидации на ≥100 сделках,
sample-size.mdc). Модуль пишет в ``reasons`` сделки и в лог переворота латча
четыре фичи-кандидата, отличающие «эталон» (устойчивое направление, кейс #530
+$54.94) от «флапа» (ложный переворот, кейсы #531/#532 06.07.2026 12:25-12:34):

1. **init** — последний initiative-импульс (направление + возраст + alignment
   со стороной сделки). Канон: направление, рождённое инициативной волной
   (объём ×10-20, односторонняя дельта), живёт долго; переворот ПРОТИВ волны —
   анти-паттерн. Детектор — ``orderflow.detect_initiative`` (D7).
2. **dwell** — сколько секунд цена НЕПРЕРЫВНО держится за пробитым
   swing-экстремумом. Канон «accepted after the breakout» = процесс (время ×
   объём), а не мгновенный снимок acc%: честный пробой живёт минуты, ложный
   отскок — секунды.
3. **dHi/dLo** — дистанция (bps) до экстремумов ДНЯ. Переворот по пробою
   ближайшего M5-фрактала на 700+ bps ниже дневного максимума не ломает
   реальную структуру (кейс #532: «пробой» на 62139 при дневном 62900).
4. **shock** — возраст последнего объёмного шока (тики ≫ базовой EMA) и его
   направление. После шока профиль перестраивается 30-60 мин, мгновенный
   classify ненадёжен; направление ПО шоку устойчивее, чем против.

Технические пороги ниже (EMA halflife, множитель шока, троттлинг) — параметры
НАБЛЮДАЕМОСТИ (anti-noise), не торговые: они влияют только на содержимое
лог-строки. Помечены [НАШЕ]-tech.
"""
from __future__ import annotations

import math
import time

from flowzone_bot.analysis.auction import _recent_extreme
from flowzone_bot.analysis.orderflow import big_trade_threshold, detect_initiative
from flowzone_bot.analysis.swings import Swing

# ─── [НАШЕ]-tech: параметры наблюдаемости (не торговые пороги) ────────────
# Троттлинг detect_initiative: окно trades при шоке = десятки тысяч принтов,
# прогонять два прохода каждый цикл (1с) дорого; раз в 5с достаточно для
# телеметрии (initiative-волна живёт минуты).
_INITIATIVE_REFRESH_SEC = 5.0
# EMA-полужизнь базового темпа ленты (тиков в rolling-окне). 10 мин — медленная
# база, не подстраивается под сам шок.
_RATE_HALFLIFE_SEC = 600.0
# Шок = тиков в окне ≥ mult × EMA (кейс 06.07: 44-93K тиков при базе 2-5K,
# т.е. ×10-20; порог ×4 ловит и менее экстремальные шоки) и ≥ абсолютного пола
# (тонкая лента не должна «шокать» на шуме).
_SHOCK_MULT = 4.0
_SHOCK_MIN_TRADES = 1000
# Минимум EMA-прогрева перед детекцией шока (секунд наблюдения).
_WARMUP_SEC = 120.0


class DirectionTelemetry:
    """Копит per-symbol фичи устойчивости направления; отдаёт строку для
    ``reasons``/лога. Обновляется из scan-цикла (только когда символ сканируется:
    при открытой позиции/cooldown возможны пропуски — приемлемо для
    наблюдаемости, dt-aware EMA это учитывает)."""

    def __init__(self, *, big_trade_pct: float = 0.90,
                 wall_now=time.time) -> None:
        self._big_pct = big_trade_pct
        self._wall_now = wall_now
        # symbol → state
        self._day: dict[str, int] = {}
        self._day_hi: dict[str, float] = {}
        self._day_lo: dict[str, float] = {}
        self._rate_ema: dict[str, float] = {}
        self._rate_ts: dict[str, float] = {}      # ts последнего EMA-апдейта
        self._rate_started: dict[str, float] = {}  # ts первого апдейта (warmup)
        self._shock: dict[str, tuple[float, str]] = {}   # (ts, 'up'|'down')
        self._init: dict[str, tuple[float, str]] = {}    # (ts, 'up'|'down')
        self._init_ts: dict[str, float] = {}       # ts последней проверки initiative
        self._dwell_up: dict[str, float] = {}      # ts первого тика выше swing high
        self._dwell_dn: dict[str, float] = {}      # ts первого тика ниже swing low

    # ── обновление состояния ────────────────────────────────────────────
    def update(self, symbol: str, now: float, snap,
               swings: list[Swing]) -> None:
        px = snap.last_price
        if px is None:
            return
        self._update_day_extremes(symbol, now, px)
        self._update_shock(symbol, now, snap)
        self._update_initiative(symbol, now, snap)
        self._update_dwell(symbol, now, px, swings)

    def _update_day_extremes(self, symbol: str, now: float, px: float) -> None:
        day = int(now // 86400)
        if self._day.get(symbol) != day:
            self._day[symbol] = day
            self._day_hi[symbol] = px
            self._day_lo[symbol] = px
        else:
            if px > self._day_hi[symbol]:
                self._day_hi[symbol] = px
            if px < self._day_lo[symbol]:
                self._day_lo[symbol] = px

    def _update_shock(self, symbol: str, now: float, snap) -> None:
        n = float(len(snap.trades))
        prev_ts = self._rate_ts.get(symbol)
        ema = self._rate_ema.get(symbol)
        started = self._rate_started.setdefault(symbol, now)
        warmed = (now - started) >= _WARMUP_SEC and ema is not None and ema > 0
        # детекция ДО апдейта EMA — шок не должен мгновенно раздувать базу
        if warmed and n >= _SHOCK_MIN_TRADES and n >= _SHOCK_MULT * ema:
            trades = snap.trades
            direction = ("up" if trades[-1].price >= trades[0].price
                         else "down")
            # пока условие держится — ts обновляется; возраст = время с конца шока
            self._shock[symbol] = (now, direction)
        if ema is None:
            self._rate_ema[symbol] = n
        else:
            dt = max(now - (prev_ts or now), 0.0)
            alpha = 1.0 - math.pow(0.5, dt / _RATE_HALFLIFE_SEC) if dt > 0 else 0.0
            self._rate_ema[symbol] = ema + alpha * (n - ema)
        self._rate_ts[symbol] = now

    def _update_initiative(self, symbol: str, now: float, snap) -> None:
        if now - self._init_ts.get(symbol, 0.0) < _INITIATIVE_REFRESH_SEC:
            return
        self._init_ts[symbol] = now
        trades = snap.trades
        if not trades:
            return
        thr = big_trade_threshold(trades, pct=self._big_pct)
        for side, direction in (("long", "up"), ("short", "down")):
            res = detect_initiative(trades, side, big_threshold=thr)
            if res.confirmed:
                self._init[symbol] = (now, direction)
                break

    def _update_dwell(self, symbol: str, now: float, px: float,
                      swings: list[Swing]) -> None:
        hi = _recent_extreme(swings, "high")
        lo = _recent_extreme(swings, "low")
        if hi is not None and px > hi:
            self._dwell_up.setdefault(symbol, now)
        else:
            self._dwell_up.pop(symbol, None)
        if lo is not None and px < lo:
            self._dwell_dn.setdefault(symbol, now)
        else:
            self._dwell_dn.pop(symbol, None)

    # ── выдача фич ──────────────────────────────────────────────────────
    def features(self, symbol: str, now: float,
                 last_price: float | None) -> dict:
        out: dict = {}
        init = self._init.get(symbol)
        if init is not None:
            out["init_dir"] = init[1]
            out["init_age_sec"] = now - init[0]
        shock = self._shock.get(symbol)
        if shock is not None:
            out["shock_dir"] = shock[1]
            out["shock_age_sec"] = now - shock[0]
        if symbol in self._dwell_up:
            out["dwell_up_sec"] = now - self._dwell_up[symbol]
        if symbol in self._dwell_dn:
            out["dwell_dn_sec"] = now - self._dwell_dn[symbol]
        px = last_price
        if px and symbol in self._day_hi:
            out["day_hi_bps"] = (self._day_hi[symbol] - px) / px * 1e4
            out["day_lo_bps"] = (px - self._day_lo[symbol]) / px * 1e4
        return out

    def fmt(self, symbol: str, now: float, side: str | None,
            last_price: float | None) -> str:
        """Компактная строка для reasons/лога. Разделитель полей — запятая
        (reasons сделки склеиваются через '+', не смешиваем).

        Пример: ``tele=init:down:412s:same,dwell_dn:35s,dHi:-123bp,dLo:18bp,
        shock:down:1520s``"""
        f = self.features(symbol, now, last_price)
        parts: list[str] = []
        if "init_dir" in f:
            align = ""
            if side in ("long", "short"):
                want = "up" if side == "long" else "down"
                align = ":same" if f["init_dir"] == want else ":counter"
            parts.append(f"init:{f['init_dir']}:{f['init_age_sec']:.0f}s{align}")
        if "dwell_up_sec" in f:
            parts.append(f"dwell_up:{f['dwell_up_sec']:.0f}s")
        if "dwell_dn_sec" in f:
            parts.append(f"dwell_dn:{f['dwell_dn_sec']:.0f}s")
        if "day_hi_bps" in f:
            parts.append(f"dHi:{f['day_hi_bps']:.0f}bp")
            parts.append(f"dLo:{f['day_lo_bps']:.0f}bp")
        if "shock_dir" in f:
            parts.append(f"shock:{f['shock_dir']}:{f['shock_age_sec']:.0f}s")
        return "tele=" + ",".join(parts) if parts else "tele=none"
