"""Direction-telemetry flowzone_bot — НАБЛЮДАЕМОСТЬ устойчивости аукциона.

НЕ гейтит вход и НЕ меняет торговое решение (no-data-fitting.mdc /
strategy-guard.mdc: новые фильтры — только после OOS-валидации на ≥100 сделках,
sample-size.mdc). Модуль пишет в ``reasons`` сделки и в лог переворота латча
четыре фичи-кандидата, отличающие «эталон» (устойчивое направление, кейс #530
+$54.94) от «флапа» (ложный переворот, кейсы #531/#532 06.07.2026 12:25-12:34):

1. **init_prev** — initiative-импульс ПРЕДЫДУЩЕЙ M5-ноги, до текущего
   absorption-окна (направление + возраст + alignment со стороной сделки).
   Канон: направление, рождённое инициативной волной (объём ×10-20,
   односторонняя дельта), живёт долго. В v1 initiative ошибочно считался на том
   же окне, где стратегия ищет поглощаемую контр-агрессию: 64/81 сделок были
   ``counter``, включая все top-10 wins. Исправлено 24.07.2026: источник —
   persisted prints окна ``[now-2×M5, now-M5)``.
2. **dwell_struct** — сколько секунд цена НЕПРЕРЫВНО держится за ЗНАЧИМЫМ
   структурным экстремумом: max(swing highs) / min(swing lows) всего M5-lookback,
   а не за ближайшим фракталом. Канон «accepted after the breakout» = процесс
   (время × объём), а не мгновенный снимок acc%. В v1 ближайший M5-fractal дал
   покрытие лишь 5/81 aligned-сделок и не представлял структуру.
3. **dHi/dLo** — дистанция (bps) до экстремумов ДНЯ. Переворот по пробою
   ближайшего M5-фрактала на 700+ bps ниже дневного максимума не ломает
   реальную структуру (кейс #532: «пробой» на 62139 при дневном 62900).
4. **shock** — возраст последнего объёмного шока (тики ≫ базовой EMA) и его
   направление. После шока профиль перестраивается 30-60 мин, мгновенный
   classify ненадёжен. В v1 shock не истекал: 50/81 сделок получили возраст
   >6ч (до 194 тыс. сек). Исправлено: TTL=60 мин + reset на новом session anchor.

Технические пороги ниже (EMA halflife, множитель шока, троттлинг) — параметры
НАБЛЮДАЕМОСТИ (anti-noise), не торговые: они влияют только на содержимое
лог-строки. Помечены [НАШЕ]-tech.
"""
from __future__ import annotations

import math
import time

from flowzone_bot.data.aggregates import TradePrint
from flowzone_bot.analysis.orderflow import big_trade_threshold, detect_initiative
from flowzone_bot.analysis.swings import Swing

# ─── [НАШЕ]-tech: параметры наблюдаемости (не торговые пороги) ────────────
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
# Пост-шоковый режим по исходной гипотезе длится 30–60 мин. После 60 мин событие
# больше не описывает текущий режим и удаляется из features/reasons.
_SHOCK_TTL_SEC = 3600.0


class DirectionTelemetry:
    """Копит per-symbol фичи устойчивости направления; отдаёт строку для
    ``reasons``/лога. ``update`` вызывается для ВСЕХ символов каждый eval-loop,
    включая open/cooldown: shock/dwell не теряют интервалы."""

    def __init__(self, *, big_trade_pct: float = 0.90,
                 wall_now=time.time) -> None:
        self._big_pct = big_trade_pct
        self._wall_now = wall_now
        # symbol → state
        self._day: dict[str, int] = {}
        self._day_hi: dict[str, float] = {}
        self._day_lo: dict[str, float] = {}
        self._session_anchor: dict[str, float] = {}
        self._rate_ema: dict[str, float] = {}
        self._rate_ts: dict[str, float] = {}      # ts последнего EMA-апдейта
        self._rate_started: dict[str, float] = {}  # ts первого апдейта (warmup)
        self._shock: dict[str, tuple[float, str]] = {}   # (ts, 'up'|'down')
        # Initiative только из ПРЕДЫДУЩЕЙ M5-ноги (refresh_preceding_initiative).
        self._init: dict[str, tuple[float, str]] = {}    # (last print ts, up|down)
        self._dwell_up: dict[str, float] = {}      # ts выше major swing high
        self._dwell_dn: dict[str, float] = {}      # ts ниже major swing low
        self._struct_hi: dict[str, float] = {}
        self._struct_lo: dict[str, float] = {}

    # ── обновление состояния ────────────────────────────────────────────
    def update(self, symbol: str, now: float, snap,
               swings: list[Swing]) -> None:
        px = snap.last_price
        if px is None:
            return
        self._update_day_extremes(symbol, now, px)
        anchor = getattr(snap, "vp_session_start", None)
        if anchor is None:
            self._reset_session(symbol)
            return
        if self._session_anchor.get(symbol) != anchor:
            self._reset_session(symbol)
            self._session_anchor[symbol] = anchor
        self._update_shock(symbol, now, snap)
        self._update_dwell(symbol, now, px, swings)

    def _reset_session(self, symbol: str) -> None:
        """Сброс session-scoped telemetry. Day-extremes остаются UTC-дневными."""
        self._session_anchor.pop(symbol, None)
        self._rate_ema.pop(symbol, None)
        self._rate_ts.pop(symbol, None)
        self._rate_started.pop(symbol, None)
        self._shock.pop(symbol, None)
        self._init.pop(symbol, None)
        self._dwell_up.pop(symbol, None)
        self._dwell_dn.pop(symbol, None)
        self._struct_hi.pop(symbol, None)
        self._struct_lo.pop(symbol, None)

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

    def refresh_preceding_initiative(self, symbol: str,
                                     trades: list[TradePrint]) -> None:
        """Пересчитать initiative по ЗАВЕРШЁННОЙ предыдущей M5-ноге.

        Вызывающий передаёт persisted prints ``[now-2×window, now-window)``.
        Текущее absorption-окно намеренно исключено: его контр-агрессия — часть
        entry trigger, а не направление предшествующего импульса.
        """
        self._init.pop(symbol, None)
        if not trades:
            return
        thr = big_trade_threshold(trades, pct=self._big_pct)
        for side, direction in (("long", "up"), ("short", "down")):
            res = detect_initiative(trades, side, big_threshold=thr)
            if res.confirmed:
                self._init[symbol] = (trades[-1].ts, direction)
                break

    def _update_dwell(self, symbol: str, now: float, px: float,
                      swings: list[Swing]) -> None:
        # Значимая структура = экстремальные confirmed swings всего M5-lookback,
        # не ближайший фрактал. Это наблюдаемость; торговый breakout-гейт
        # AuctionTracker остаётся неизменным.
        highs = [s.price for s in swings if s.kind == "high"]
        lows = [s.price for s in swings if s.kind == "low"]
        hi = max(highs) if highs else None
        lo = min(lows) if lows else None
        if hi is not None:
            self._struct_hi[symbol] = hi
        if lo is not None:
            self._struct_lo[symbol] = lo
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
            out["init_age_sec"] = max(now - init[0], 0.0)
        shock = self._shock.get(symbol)
        if shock is not None:
            age = now - shock[0]
            if age <= _SHOCK_TTL_SEC:
                out["shock_dir"] = shock[1]
                out["shock_age_sec"] = max(age, 0.0)
            else:
                self._shock.pop(symbol, None)
        if symbol in self._dwell_up:
            out["dwell_struct_up_sec"] = now - self._dwell_up[symbol]
        if symbol in self._dwell_dn:
            out["dwell_struct_dn_sec"] = now - self._dwell_dn[symbol]
        px = last_price
        if px and symbol in self._day_hi:
            out["day_hi_bps"] = (self._day_hi[symbol] - px) / px * 1e4
            out["day_lo_bps"] = (px - self._day_lo[symbol]) / px * 1e4
        if px and symbol in self._struct_hi:
            out["struct_hi_bps"] = (self._struct_hi[symbol] - px) / px * 1e4
        if px and symbol in self._struct_lo:
            out["struct_lo_bps"] = (px - self._struct_lo[symbol]) / px * 1e4
        return out

    def fmt(self, symbol: str, now: float, side: str | None,
            last_price: float | None) -> str:
        """Компактная строка для reasons/лога. Разделитель полей — запятая
        (reasons сделки склеиваются через '+', не смешиваем).

        Пример: ``tele=init_prev:down:412s:same,dwell_struct_dn:35s,
        dStructHi:123bp,dStructLo:-18bp,dHi:140bp,dLo:9bp,shock:down:1520s``"""
        f = self.features(symbol, now, last_price)
        parts: list[str] = []
        if "init_dir" in f:
            align = ""
            if side in ("long", "short"):
                want = "up" if side == "long" else "down"
                align = ":same" if f["init_dir"] == want else ":counter"
            parts.append(f"init_prev:{f['init_dir']}:{f['init_age_sec']:.0f}s{align}")
        if "dwell_struct_up_sec" in f:
            parts.append(f"dwell_struct_up:{f['dwell_struct_up_sec']:.0f}s")
        if "dwell_struct_dn_sec" in f:
            parts.append(f"dwell_struct_dn:{f['dwell_struct_dn_sec']:.0f}s")
        if "struct_hi_bps" in f:
            parts.append(f"dStructHi:{f['struct_hi_bps']:.0f}bp")
        if "struct_lo_bps" in f:
            parts.append(f"dStructLo:{f['struct_lo_bps']:.0f}bp")
        if "day_hi_bps" in f:
            parts.append(f"dHi:{f['day_hi_bps']:.0f}bp")
            parts.append(f"dLo:{f['day_lo_bps']:.0f}bp")
        if "shock_dir" in f:
            parts.append(f"shock:{f['shock_dir']}:{f['shock_age_sec']:.0f}s")
        return "tele=" + ",".join(parts) if parts else "tele=none"
