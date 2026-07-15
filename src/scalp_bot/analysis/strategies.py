"""Мультистратегийный каркас scalp_bot.

Бот гоняет несколько НЕЗАВИСИМЫХ стратегий поверх одного потока данных Bybit.
Контракт (см. обсуждение архитектуры 2026-05-30):

- Каждая стратегия сама ищет вход (``update``), помечает сигнал своим именем
  и сама сопровождает свою позицию (``should_exit``). Вход и выход — в паре:
  позицию, открытую стратегией A, НЕЛЬЗЯ закрывать логикой стратегии B.
- Универсальные выходы (TP / SL / тайм-стоп) и риск (killswitch, размер лота,
  fee-guard) — ОБЩИЕ, живут в executor/killswitch, стратегиям безразличны.
- На один символ — максимум одна позиция. Если две стратегии в один тик дают
  ПРОТИВОПОЛОЖНЫЕ направления по символу — не берём ни одну (``resolve``).

Стратегии:
- ``sweep_fade``      — свип+поглощение mean-reversion (SweepReclaimDetector).
- ``density_bounce``  — отскок ОТ плотности в стакане (стена держит → fade).
- ``density_break``   — пробой НА СНОСЕ плотности («прострел», momentum) —
                        зеркало density_bounce: выстоявшая стена пробита → вход
                        по ходу пробоя (Данилов YouTube 2026).
- ``sweep_fade_canon`` — канон-вариант sweep_fade (v0.18.20, параллельный
                        форвард-тест): значимые уровни (PDH/PDL + дневные
                        экстремумы) + full reclaim + вселенная мейджоров.
"""
from __future__ import annotations

import logging
import math
from typing import Protocol

from scalp_bot.analysis.signals import (
    Signal,
    SweepReclaimDetector,
    build_signal,
    flow_invalidated,
    ob_supportive,
    reversal_momentum,
)
from scalp_bot.data.aggregates import SymbolSnapshot

play = logging.getLogger("scalp_bot.play")

_SIDE_RU = {"long": "LONG↑", "short": "SHORT↓"}


class Strategy(Protocol):
    """Интерфейс стратегии. Реализации держат своё пер-символьное состояние."""

    name: str

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        """Поиск входа по символу snap.symbol. None — сетапа нет."""
        ...

    def armed(self, symbol: str) -> bool:
        """Стратегия «во взводе» по символу (для funnel-диагностики)."""
        ...

    def reset(self, symbol: str) -> None:
        """Сбросить состояние по символу (вызывается при открытой позиции)."""
        ...

    def ensure_symbols(self, symbols: list[str]) -> None:
        """Лениво завести пер-символьное состояние для новых символов (ротация
        вселенной), не теряя состояние уже известных."""
        ...

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        """Дискреционный выход стратегии (помимо общих TP/SL/тайм-стопа).

        Возвращает (close_reason, exit_price) или None. Вызывается executor-ом
        ТОЛЬКО для сделок этой стратегии (tr.strategy == self.name)."""
        ...


class SweepFadeStrategy:
    """Стратегия №1: свип ликвидности + поглощение (mean-reversion fade).

    Обёртка над двухфазным ``SweepReclaimDetector`` (по детектору на символ).
    Дискреционный выход (should_exit) — два триггера по развороту ленты (CVD flip
    против позиции), оба только после active_exit_min_age_sec:
      1. ПРОФИТ-ЛОК (flow_exit): ход в плюс ≥ flow_exit_activate_r × R И поток
         развернулся → фиксируем. v0.7.1: порог поднят с «≥ round-trip комиссии»
         до ≥1R (анти-клиппинг, анализ 427 сделок 2026-05-31). v0.13.0: 1.0→1.5R
         (sweep 15д data/scalp_sweep.txt — выше порог укрупняет средний винер,
         даём добежать дальше к TP=3.5R, Философия B). Ниже порога на развороте —
         ДЕРЖИМ.
      2. SCRATCH-ПРИ-ОШИБКЕ (flow_scratch): v0.13.0 — ВЫКЛЮЧЕН по умолчанию
         (scratch_on_flow_flip=False). Контрфактуал + sweep 15д: scratch при
         −0.7R маргинально вредит (режет 0.3R недохода до SL −1R, убивает ~12%
         отскоков — противоречит MR «дождаться отскока»). Полагаемся на биржевой
         SL. Если включён — режет ход в МИНУС ≥ scratch_min_adverse_r×R при флипе
         ленты и «созревании» сделки (≥ scratch_min_age_sec).

    v0.18.26 — ВХОД (изолировано от canon/density, per-detector override):
      (a) skip-round гейт — не фейдим свип у round-уровня (round00/50): бэктест
          --level-decomp 06-10..15 n=956 показал round WR 35% ХУЖЕ микро 42%
          (инверсия канона в даунтренд-режиме; sweep_fade_skip_round);
      (b) full reclaim 1.0 (CAP Rule 2, как canon; sweep_fade_reclaim_frac).
    Forward-test с откатом по env, малая выборка/1 режим (sample-size.mdc
    нарушено осознанно по решению пользователя — см. BUILDLOG_SCALP v0.18.26).
    """

    name = "sweep_fade"
    # mean-reversion: фейд ТОЛЬКО по HTF-тренду (на наших данных EMA даёт
    # +0.087R vs +0.042 без; A/B BUILDLOG 2026-06-02) и НЕ в трендовый день
    # (ADX≥25 → +15% gross edge, v0.17.0). Оба фильтра уместны.
    htf_filtered = True
    regime_gated = True

    def __init__(self, cfg, symbols: list[str]) -> None:
        self.cfg = cfg
        self._det: dict[str, SweepReclaimDetector] = {
            s: self._make_detector(s) for s in symbols
        }

    def _round_gate(self, swept: float) -> bool:
        """v0.18.26 (B): свип у round-уровня (round00/50). База НЕ фейдит такие —
        scalp_backtest_regime --level-decomp: round хуже микро (WR 35% vs 42%)."""
        return near_round_hier(
            swept, getattr(self.cfg, "density_round_frac", 0.003)) is not None

    def _make_detector(self, s: str) -> SweepReclaimDetector:
        # Изоляция: round_gate и reclaim override применяются ТОЛЬКО к базе
        # sweep_fade (canon строит детекторы своим ensure_symbols без них).
        rg = self._round_gate if getattr(
            self.cfg, "sweep_fade_skip_round", False) else None
        return SweepReclaimDetector(
            s, self.cfg, round_gate=rg,
            reclaim_frac=getattr(self.cfg, "sweep_fade_reclaim_frac", None))

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        det = self._det.get(snap.symbol)
        if det is None:
            return None
        sig = det.update(snap, now)
        if sig is not None:
            sig.strategy = self.name
        return sig

    def armed(self, symbol: str) -> bool:
        det = self._det.get(symbol)
        return bool(det and det.armed)

    def reset(self, symbol: str) -> None:
        det = self._det.get(symbol)
        if det is not None:
            det.reset()

    def ensure_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            if s not in self._det:
                self._det[s] = self._make_detector(s)

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        cfg = self.cfg
        if not getattr(cfg, "active_exit_enabled", False) or snap is None:
            return None
        if now - tr.ts_open < cfg.active_exit_min_age_sec:
            return None
        price = snap.last_price
        if price is None:
            return None
        favorable = (price - tr.entry) if tr.side == "long" else (tr.entry - price)
        # R-единица порогов = base_risk (MFE-якорь), а НЕ ширина SL. При sl_mult>1
        # стоп шире, но flow_exit/scratch срабатывают на тех же АБСОЛЮТНЫХ уровнях
        # хода-в-плюс, что при ×1.0 (синхронизация — иначе лок уезжал на 1.5×R и
        # пропускал ~16% winners). base_risk восстанавливаем из TP: tp = tpr×base_risk
        # → base_risk = tp_dist/tpr. Fallback на |entry−sl|, если tp пуст/битый.
        tpr = getattr(cfg, "take_profit_r", 0.0)
        tp_dist = abs(getattr(tr, "tp", tr.entry) - tr.entry)
        risk = (tp_dist / tpr) if (tpr > 0 and tp_dist > 0) \
            else abs(tr.entry - getattr(tr, "sl", tr.entry))
        flipped = flow_invalidated(snap, tr.side, cfg.momentum_window_sec)
        if not flipped:
            return None  # лента ещё за нас — держим
        # 1) профит-лок: фиксируем по развороту ленты ТОЛЬКО когда набрана
        #    осмысленная прибыль ≥ activate_r × R (анти-клиппинг v0.7.1). Ниже
        #    порога — ДЕРЖИМ (даём добежать к TP=3.5R), не клипаем центы.
        activate = getattr(cfg, "flow_exit_activate_r", 1.0) * risk
        if risk > 0 and favorable >= activate:
            return ("flow_exit", price)
        # 2) scratch-при-ошибке: сделка реально в минусе ≥ scratch_min_adverse_r×R
        #    (v0.9.2: не hair-trigger «≥комиссии», а порог глубины), поток против и
        #    сделка созрела → режем убыток рано (не ждём SL/тайм-стоп)
        adverse = getattr(cfg, "scratch_min_adverse_r", 0.7) * risk
        if (getattr(cfg, "scratch_on_flow_flip", False)
                and risk > 0 and favorable <= -adverse
                and now - tr.ts_open >= getattr(cfg, "scratch_min_age_sec", 20.0)):
            return ("flow_scratch", price)
        return None


class _CfgOverlay:
    """Прозрачная обёртка cfg с точечными override-полями (для пер-стратегийных
    отклонений без копирования всего конфига). Читает override, иначе — базу."""

    def __init__(self, base, **overrides) -> None:
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name: str):
        ov = object.__getattribute__(self, "_overrides")
        if name in ov:
            return ov[name]
        return getattr(object.__getattribute__(self, "_base"), name)


class SweepFadeCanonStrategy(SweepFadeStrategy):
    """Стратегия №4 (v0.18.20): КАНОН-вариант sweep_fade — параллельный
    форвард-тест A/B против базового (одобрено пользователем 2026-06-11).

    ─── Research basis ───
    Базовый sweep_fade живёт ниже канонного WR 60%+ (live n=899: WR 35%; лучшая
    неделя 52%; ETH 55% vs ZEC 28%). Три задокументированных упрощения канона
    CAP (chartwhisperer order-flow 2026) исправлены здесь:
    1. ЗНАЧИМЫЕ УРОВНИ: взвод только на свипе PDH/PDL или дневного экстремума
       (KeyLevels) — там реально стоят стопы (Osler 2003 NY Fed «stop orders
       cluster on visible levels»), а не 3-минутный микро-экстремум.
    2. FULL RECLAIM (CAP Rule 2 буквально): цена ВЕРНУЛАСЬ за свипнутый уровень
       (reclaim_frac=1.0), а не 50% пути.
    3. ВСЕЛЕННАЯ МЕЙДЖОРОВ: fade канонически живёт в ликвидных рейнджевых
       книгах (Tradeify «ES deep book → fade»; live ETH WR 55%). Канон-страта
       торгует ТОЛЬКО symbol_scope (BTC/ETH/SOL/BNB/XRP по умолчанию),
       vol-вселенная остальных страт не затронута.

    Выходы/SL/TP и ADX-режим-гейт — идентичны базовому sweep_fade
    (наследование). НАПРАВЛЕННЫЕ гейты (EMA200 HTF + DMI-лонг) у канона СНЯТЫ
    (v0.18.22, одобрено пользователем 2026-06-11): фейд свипа ДНЕВНОГО уровня
    контртрендовый по построению — свип PDH означает цену выше вчерашнего
    максимума (EMA200≈long всегда → шорт-фейд блокировался бы в 100% случаев;
    live-замер день 1: 252/252 канон-выстрелов порезано HTF-гейтом, 0 сделок),
    зеркально PDL/DMI для лонгов. Канон (Connors/Raschke 1995 «Street Smarts»
    Turtle Soup; SFP) — контртренд-сетап: защита от трендовых дней — режимный
    фильтр (ADX остаётся) + full reclaim + CVD-разворот, не направленный EMA.
    Обе версии копят выборку параллельно (атрибуция через колонку strategy
    в БД), решение по n≥100 на каждую (sample-size.mdc).
    """

    name = "sweep_fade_canon"
    # v0.18.22: направленные гейты сняты (структурный конфликт с дневными
    # уровнями, см. docstring). di_long_gated выводится из htf_filtered в
    # main.py — фиксируем явно для читаемости.
    htf_filtered = False
    di_long_gated = False
    regime_gated = True

    def __init__(self, cfg, symbols: list[str]) -> None:
        # full reclaim через overlay: детектор читает cfg.reclaim_frac
        canon_cfg = _CfgOverlay(cfg, reclaim_frac=cfg.sweep_fade_canon_reclaim_frac)
        super().__init__(canon_cfg, [])
        # KeyLevels инжектится из main (нужен REST-клиент); до инжекта детекторы
        # не взводятся (level_gate fail-closed возвращает None при нет данных).
        self.key_levels = None
        self.symbol_scope = set(cfg.sweep_fade_canon_symbol_list)
        self.ensure_symbols([s for s in symbols if s in self.symbol_scope])

    def _level_gate(self, symbol: str, side: str, swept: float) -> str | None:
        if self.key_levels is None:
            return None  # fail-closed: уровни ещё не прогреты — не торгуем
        return self.key_levels.swept_key_level(symbol, side, swept)

    def ensure_symbols(self, symbols: list[str]) -> None:
        # v0.18.24: канон-вход — taker ПО КАНОНУ Turtle Soup (Connors/Raschke
        # 1995): вход — стоп НАД/ПОД уровнем, срабатывает на возврате цены сквозь
        # уровень = активный вход ПО reclaim. Пассивный maker ниже цены инвертит
        # канон-вход (был fee-overlay v0.10.0). cfg.sweep_fade_canon_entry_order
        # _type; пусто → глобальный maker.
        otype = getattr(self.cfg, "sweep_fade_canon_entry_order_type", None) or None
        for s in symbols:
            if s not in self.symbol_scope:
                continue
            if s not in self._det:
                # ИЗОЛЯЦИЯ от base (v0.18.26): canon строит детекторы САМ, БЕЗ
                # round_gate (фейдит значимые уровни намеренно) и без reclaim
                # override — reclaim берётся из своего _CfgOverlay
                # (sweep_fade_canon_reclaim_frac). Правки base sweep_fade его не
                # касаются.
                self._det[s] = SweepReclaimDetector(s, self.cfg,
                                                    level_gate=self._level_gate,
                                                    entry_order_type=otype)


# sweep_fade_run (v0.18.27) УДАЛЕНА 2026-07-15 (v0.18.37, решение
# пользователя): форвард-тест n=176 WR 12% net −$327 — гипотеза «дай winners
# бежать» (breakeven-lock + убранный flow_exit + scratch) ОПРОВЕРГНУТА: run хуже
# и canon (n=15 WR 40%), и base (n=621 WR 9% но +$120 — крупные winners тянут).
# Exit-контракт run (BE-lock, scratch) не дал winners «бежать» в плюс —
# стратегия стабильно убыточна до и после bug-fix 058e695 (знак BE-SL). Полное
# обоснование с числами — BUILDLOG_SCALP 2026-07-15; история сделок в БД
# (strategy='sweep_fade_run') сохранена; код — git v0.18.27..v0.18.36.
# Возврат к канону: sweep_fade_canon (базовый flow_exit, значимые уровни,
# full reclaim, вселенная мейджоров) — A/B base vs canon, как в исходном
# дизайне v0.18.20.


# sweep_fade_trend (v0.18.27) УДАЛЕНА 2026-07-06 (v0.18.33, решение
# пользователя): форвард-тест n=35 WR 37% net −$121.62 — гипотеза «range-day
# fades прибыльны» опровергнута (нужен WR 46% при R:R 1.2, canon-exit).
# Полное обоснование с числами — BUILDLOG_SCALP 2026-07-06; история сделок в
# БД (strategy='sweep_fade_trend') сохранена; код — git v0.18.27..v0.18.32.


# ─── density_bounce helpers (чистые, тестируемые без WS) ───────────────────

def near_round(price: float, frac: float) -> bool:
    """Цена рядом с круглым числом (в пределах frac×price).

    Шаг круглости масштабируется к величине цены: step = 10^(порядок−1)
    (~1% от цены). Напр. 66→шаг 1 (рядом 65/66/67), 518→шаг 10 (510/520),
    2.4→шаг 0.1. Данилов: плотности на круглых уровнях держат надёжнее.
    """
    if price <= 0:
        return False
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0:
        return False
    nearest = round(price / step) * step
    return abs(price - nearest) <= frac * price


def near_round_hier(price: float, frac: float) -> str | None:
    """Иерархический round-детектор (v0.18.15) — БОНУС-фактор density_bounce.

    Канон: лимитные ордера кластеризуются на круглых уровнях ИЕРАРХИЧНО — 00
    сильнее 50 (Bloomfield-Chin-Craig 2024: integer-цены ×3.73 чаще случайного,
    кластеры на «$100, $50 и $1 increments»; Osler 2003 NY Fed: TP-ордера сильно
    кластеризуются на round). Базовый ``near_round`` распознаёт только 00-уровни
    (шаг ≈1% цены) — для дорогих монет слишком груб (BTC видит лишь $1000-кратные,
    пропуская реальные кластеры $63 500 = ½-уровень). Здесь добавлен ½-уровень.

    Возвращает 'round00' (у полного шага), 'round50' (у половинного) или None.
    ¼-уровень НЕ берём намеренно: на дорогих монетах его сетка делает «round»
    почти всегда истинным (теряет дискриминативность) — это не подгонка, а
    сохранение смысла «сильный round-кластер».

    ВАЖНО: используется ТОЛЬКО density_bounce как confluence-бонус (не гейт).
    density_break/sweep_fade продолжают использовать строгий ``near_round`` (или
    не используют вовсе) — их логика не затронута.
    """
    if price <= 0:
        return None
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0:
        return None

    def _near(s: float) -> bool:
        nearest = round(price / s) * s
        return abs(price - nearest) <= frac * price

    if _near(step):
        return "round00"
    if _near(step / 2.0):
        return "round50"
    return None


def _baseline_avg(sizes: list[float]) -> float:
    """Средний размер «обычного» уровня = mean без единственного максимума
    (Kalena: стена выражается как кратное СРЕДНЕГО, аномалию в базу не берём,
    иначе крупная стена сама раздувает свой порог при малом N уровней)."""
    if len(sizes) < 2:
        return sizes[0] if sizes else 0.0
    mx = max(sizes)
    others = list(sizes)
    others.remove(mx)
    return sum(others) / len(others)


class RollingBaseline:
    """Скользящее среднее «типичного» размера уровня за окно (Kalena: стена =
    кратное среднего за 10–15 мин, а НЕ мгновенного top-N). Каждый тик кормим
    per-snapshot baseline (``_baseline_avg``), храним (ts, val) за window_sec.

    Аудит v0.9.0: мгновенный baseline давал max-уровень лишь 2–4× среднего —
    стена 5× недостижима (0/502 входов). Time-windowed baseline нормирует к
    типичной глубине рынка, ловя реально аномальные уровни (research-каноничный
    знаменатель Kalena)."""

    __slots__ = ("window", "_samples")

    def __init__(self, window_sec: float) -> None:
        self.window = window_sec
        self._samples: list[tuple[float, float]] = []

    def add(self, ts: float, value: float) -> None:
        if value > 0:
            self._samples.append((ts, value))
        cut = ts - self.window
        if self._samples and self._samples[0][0] < cut:
            self._samples = [(t, v) for t, v in self._samples if t >= cut]

    def value(self) -> float:
        if not self._samples:
            return 0.0
        return sum(v for _, v in self._samples) / len(self._samples)

    def ready(self, min_samples: int) -> bool:
        return len(self._samples) >= min_samples


def detect_wall(levels: list[tuple[float, float]], wall_mult: float,
                min_usd: float = 0.0,
                baseline: float | None = None) -> tuple[float, float] | None:
    """Крупнейшая «стена» на стороне книги: size ≥ wall_mult × baseline.

    baseline — знаменатель: если передан (>0) — используем СКОЛЬЗЯЩИЙ (Kalena
    10–15мин, v0.9.0); иначе fallback на мгновенный ``_baseline_avg`` (warmup).
    min_usd — опциональный абсолютный пол (price×size).
    Возвращает (price, size) стены или None.
    """
    if len(levels) < 5:
        return None
    base = baseline if (baseline is not None and baseline > 0) \
        else _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return None
    price, size = max(levels, key=lambda ps: ps[1])
    if size < wall_mult * base:
        return None
    if min_usd > 0 and price * size < min_usd:
        return None
    return (price, size)


def _wall_in_range(levels: list[tuple[float, float]], lo: float, hi: float,
                   wall_mult: float, min_usd: float = 0.0,
                   baseline: float | None = None) -> bool:
    """Есть ли всё ещё квалифицирующая стена в ценовом диапазоне [lo, hi]."""
    if len(levels) < 5:
        return False
    base = baseline if (baseline is not None and baseline > 0) \
        else _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return False
    for price, size in levels:
        if lo <= price <= hi and size >= wall_mult * base:
            if min_usd <= 0 or price * size >= min_usd:
                return True
    return False


def _find_wall_near(levels: list[tuple[float, float]], anchor: float,
                    tol_frac: float, wall_mult: float, min_usd: float = 0.0,
                    baseline: float | None = None) -> tuple[float, float] | None:
    """Квалифицирующий уровень (≥wall_mult×base) в допуске |price−anchor| ≤
    tol_frac×anchor. НЕ требует быть максимумом стороны книги — продолжение
    трека стены (v0.18.30): айсберг-рефреш/перестановка на пару тиков и чужая
    крупная лимитка в другом месте книги НЕ рвут идентичность стены.
    Возвращает крупнейший подходящий уровень или None."""
    if len(levels) < 5 or anchor <= 0:
        return None
    base = baseline if (baseline is not None and baseline > 0) \
        else _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return None
    tol = tol_frac * anchor
    best: tuple[float, float] | None = None
    for price, size in levels:
        if abs(price - anchor) <= tol and size >= wall_mult * base:
            if min_usd > 0 and price * size < min_usd:
                continue
            if best is None or size > best[1]:
                best = (price, size)
    return best


class DensityBounceStrategy:
    """Стратегия №2: отскок от плотности (крупной лимитки) в стакане.

    ─── Research basis ───
    Kalena «Crypto Wall Detection» 2026: стена = ≥5–8× средний размер уровня
    за 10–15 мин (относительный порог, не абсолютный $; берём 5×). v0.9.0:
    знаменатель = СКОЛЬЗЯЩИЙ baseline за density_baseline_sec (RollingBaseline),
    а не мгновенный top-25 — мгновенный давал max-уровень 2–4× (5× недостижим,
    0/502 входов); time-windowed baseline = каноничный знаменатель Kalena. Если
    >30% стены ушло за <10с —
    остаток скоро снимут (спуфинг) → не торгуем. arXiv 2604.20949: depth-
    сигналы причинно раньше flow. Данилов (YouTube 2025): отскок от плотности
    на круглом числе, стоп сразу за стеной (короткий → хороший R:R).

    Логика (на символ):
    1. Найти стену на bid (→long) / ask (→short), близко к круглому числу.
    2. Отслеживать её: должна продержаться ≥ persist_sec (анти-спуфинг);
       если поглощается (size упал на ≥ absorb_frac за absorb_window) — снять.
    3. Когда цена подошла к стене (≤ near_bps) и стена «выстояла» → вход в
       отскок, SL сразу за стеной (build_signal swept=цена_стены), TP по R с
       общим fee-guard.
    Выход (should_exit): стена, на которую опирались, исчезла → тезис снят.

    ─── v0.18.30 (2026-07-02): редизайн трека стены ───
    Аудит: после v0.18.15 (persist 20м) — 0 сигналов за 24 дня. Причина не в
    рынке (C-05: 267 сырых стен ≥5× за замер), а в идентификации трека: стена
    обязана была быть МАКСИМУМОМ стороны книги с float-точностью каждый тик
    (1с) все 1200с подряд — любой айсберг-рефреш, чужая крупная лимитка или
    выход из топ-50 окна WS обнуляли first_seen. Канон «стена сидит 20–30 мин»
    (Secret Terminal, Bookmap) говорит о ЖИВОМ УРОВНЕ-ЗОНЕ, а не о строгом
    максимуме каждый тик; айсберги по определению рефилятся на том же уровне
    (Bookmap iceberg detection). Новый трек:
    - идентичность = якорь ± tolerance_bps (уровень-зона, не exact float);
    - «жива» = есть квалифицирующий уровень ≥mult×base у якоря (НЕ обязан
      быть максимумом книги);
    - grace-период на пропадание (WS-чурн/мгновенный рефил) до снятия трека;
    - анти-абсорбция СКОЛЬЗЯЩАЯ: ≥absorb_frac съедено за ≤absorb_window от
      недавнего пика в ЛЮБОЙ момент жизни (раньше окно проверялось только от
      first_seen → при persist 20м не работало вовсе);
    - вход taker/market (пер-стратегийно): отскок — быстрое событие, история
      maker-входа: 12/33 (36%) сигналов не налились (entry_Cancelled/timeout);
      тот же вывод, что C-06 для density_break (fill-rate 42.6% → market).
    """

    name = "density_bounce"
    # mean-reversion: отскок ОТ стены (фейд подхода к плотности) — те же фильтры,
    # что у sweep_fade (фейд по тренду + не в трендовый день).
    htf_filtered = True
    regime_gated = True

    def __init__(self, cfg, symbols: list[str]) -> None:
        self.cfg = cfg
        # на символ: {"bid": wallstate|None, "ask": wallstate|None}
        self._track: dict[str, dict[str, dict | None]] = {
            s: {"bid": None, "ask": None} for s in symbols
        }
        self._base: dict[str, dict[str, RollingBaseline]] = {
            s: self._new_base() for s in symbols
        }
        self._last_log: dict[str, float] = {}
        # v0.18.32: очередь lifecycle-событий треков (дренируется main loop →
        # БД). Стратегия не пишет в БД сама (чистый signal-generator); main
        # loop вызывает drain_lifecycle() после update().
        self._lifecycle: list[dict] = []

    def _new_base(self) -> dict[str, RollingBaseline]:
        w = getattr(self.cfg, "density_baseline_sec", 900.0)
        return {"bid": RollingBaseline(w), "ask": RollingBaseline(w)}

    def _wall_baseline(self, sym: str, book_side: str,
                       levels: list[tuple[float, float]], now: float) -> float | None:
        """Кормим rolling-baseline текущим per-snapshot средним и возвращаем
        скользящее значение (или None пока идёт прогрев → fallback на мгновенный)."""
        rb = self._base.setdefault(sym, self._new_base())[book_side]
        rb.add(now, _baseline_avg([sz for _, sz in levels]))
        min_n = getattr(self.cfg, "density_baseline_min_samples", 30)
        return rb.value() if rb.ready(min_n) else None

    def armed(self, symbol: str) -> bool:
        t = self._track.get(symbol)
        return bool(t and (t["bid"] or t["ask"]))

    def reset(self, symbol: str) -> None:
        if symbol in self._track:
            self._track[symbol] = {"bid": None, "ask": None}

    def ensure_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            self._track.setdefault(s, {"bid": None, "ask": None})
            self._base.setdefault(s, self._new_base())

    def _kill_track(self, sym: str, book_side: str, cur: dict, now: float,
                    reason: str, last: float | None = None) -> None:
        """Снять трек + телеметрия (форвард-замер жизни стен для тюнинга по
        данным). Смерти короче 60с не логируем в play (чурн — не сигнал), но
        в БД lifecycle пишутся ВСЕ (анализу нужна полная кривая выживаемости)."""
        self._emit_lifecycle(sym, book_side, cur, now, reason, last)
        self._track[sym][book_side] = None
        life = now - cur["first_seen"]
        if life >= 60.0:
            play.info("🧱 [%s] трек стены %s %.6f умер: %s (жил %.0fс из "
                      "%.0fс persist)", sym, book_side, cur["price"], reason,
                      life, self._persist())

    def _emit_lifecycle(self, sym: str, book_side: str, cur: dict,
                        now: float, reason: str, last: float | None) -> None:
        """Собрать строку lifecycle трека в очередь (main loop запишет в БД).
        v0.18.32 — телеметрия для офлайн-анализа persist-порога."""
        self._lifecycle.append({
            "ts_start": cur["first_seen"], "ts_end": now,
            "symbol": sym, "book_side": book_side,
            "anchor_price": cur["price"], "life_sec": now - cur["first_seen"],
            "death_reason": reason,
            "reached_persist": 1 if cur.get("persisted_ts") is not None else 0,
            "persisted_ts": cur.get("persisted_ts"),
            "price_start": cur.get("price_start"),
            "price_persist": cur.get("price_persist"),
            "price_end": last,
            "did_price_approach": cur.get("did_price_approach", 0),
            "max_size": cur.get("max_size"),
            "round_tier": cur.get("round"),
        })

    def drain_lifecycle(self) -> list[dict]:
        """Вернуть накопленные lifecycle-события треков и очистить очередь.
        Main loop вызывает после update() и пишет в density_tracks."""
        out = self._lifecycle
        self._lifecycle = []
        return out

    def _persist(self) -> float:
        p = getattr(self.cfg, "density_bounce_persist_sec", None)
        return p if p is not None else self.cfg.density_persist_sec

    def _update_track(self, sym: str, book_side: str,
                      levels: list[tuple[float, float]], now: float,
                      last: float | None = None) -> None:
        cfg = self.cfg
        t = self._track[sym]
        base = self._wall_baseline(sym, book_side, levels, now)
        cur = t[book_side]
        # ── нет трека: ищем новую стену (максимум стороны, как раньше) ──
        if cur is None:
            wall = detect_wall(levels, cfg.density_wall_mult,
                               cfg.density_min_wall_usd, baseline=base)
            if wall is None:
                return
            price, size = wall
            # v0.18.15: near_round НЕ гейт → score-бонус (Bookmap/Secret
            # Terminal/QuantStrategy.io: гейт = размер+persist+absorption;
            # прежний AND-гейт резал 83.5% стен — C-05,
            # data/scalp_density_nearround_audit.txt).
            t[book_side] = {"price": price, "size0": size, "last_size": size,
                            "first_seen": now, "miss_since": None,
                            # скользящее окно (ts, size) для анти-абсорбции
                            "recent": [(now, size)],
                            "round": near_round_hier(price, cfg.density_round_frac),
                            # v0.18.32: lifecycle-телеметрия
                            "price_start": last, "persisted_ts": None,
                            "price_persist": None, "did_price_approach": 0,
                            "max_size": size}
            return
        # ── трек есть: жив, если у ЯКОРЯ есть уровень ≥mult×base (v0.18.30:
        # допуск tolerance_bps, НЕ обязан быть максимумом книги) ──
        tol = getattr(cfg, "density_wall_tolerance_bps", 5.0) / 1e4
        lvl = _find_wall_near(levels, cur["price"], tol, cfg.density_wall_mult,
                              cfg.density_min_wall_usd, baseline=base)
        if lvl is None:
            # grace-период: WS-чурн/мгновенный айсберг-рефил не рвёт трек
            grace = getattr(cfg, "density_track_grace_sec", 10.0)
            if cur["miss_since"] is None:
                cur["miss_since"] = now
            elif now - cur["miss_since"] > grace:
                self._kill_track(sym, book_side, cur, now,
                                 "уровень исчез > grace (снят/пробит/ушёл из окна)",
                                 last)
            return
        price, size = lvl
        cur["miss_since"] = None
        cur["last_size"] = size
        cur["round"] = near_round_hier(price, cfg.density_round_frac)
        if size > cur.get("max_size", size):
            cur["max_size"] = size
        # анти-абсорбция СКОЛЬЗЯЩАЯ (Kalena: ≥30% съедено за <10с → спуф/пробой).
        # Сравниваем с ПИКОМ за absorb_window, а не с size0 от first_seen —
        # иначе при persist 20м проверка не работала после первых 10с жизни.
        win = cfg.density_absorb_window_sec
        cur["recent"] = [(ts, sz) for ts, sz in cur["recent"]
                         if ts >= now - win]
        cur["recent"].append((now, size))
        peak = max(sz for _, sz in cur["recent"])
        eaten = (peak - size) / peak if peak > 0 else 0.0
        if eaten >= cfg.density_absorb_frac:
            play.info("🧱 [%s] стена %s %.6f поглощается (%.0f%% за ≤%.0fс, "
                      "жила %.0fс) — снимаю наблюдение (спуфинг/пробой)", sym,
                      book_side, price, eaten * 100, win,
                      now - cur["first_seen"])
            self._emit_lifecycle(sym, book_side, cur, now, "absorbed", last)
            t[book_side] = None

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        cfg = self.cfg
        if snap.stale or snap.last_price is None:
            return None
        sym = snap.symbol
        if sym not in self._track:
            self._track[sym] = {"bid": None, "ask": None}
        last = snap.last_price
        self._update_track(sym, "bid", snap.bids, now, last)
        self._update_track(sym, "ask", snap.asks, now, last)
        near = cfg.density_near_bps / 1e4
        # v0.18.15: пер-стратегийный persist (канон density-фейда 20–30+ мин).
        persist = self._persist()
        # bid-стена → отскок ВВЕРХ (long); ask-стена → отскок ВНИЗ (short)
        for book_side, side in (("bid", "long"), ("ask", "short")):
            w = self._track[sym][book_side]
            if w is None:
                continue
            if now - w["first_seen"] < persist:
                continue  # ещё не выстояла (анти-спуфинг, канон ≥20–30м)
            # телеметрия: стена дожила до persist (раз на трек)
            if not w.get("persisted_logged"):
                w["persisted_logged"] = True
                w["persisted_ts"] = now  # v0.18.32: lifecycle
                w["price_persist"] = last
                play.info("🧱 [%s] стена %s %.6f ВЫСТОЯЛА %.0fс — жду подхода "
                          "цены (≤%.1f б.п.)", sym, book_side, w["price"],
                          persist, cfg.density_near_bps)
            if w.get("miss_since") is not None:
                continue  # уровень в grace-паузе (невидим) — не входим вслепую
            if abs(last - w["price"]) > near * w["price"]:
                continue  # цена ещё не подошла к стене
            w["did_price_approach"] = 1  # v0.18.32: lifecycle
            # density + persist — обязательные; round — confluence-бонус (v0.18.15,
            # не гейт). score = число факторов: 2 (без round) / 3 (round00/50).
            reasons = ["density", "persist"]
            if w.get("round"):
                reasons.append(w["round"])
            # v0.18.30: taker-вход (пер-стратегийно). Отскок — быстрое событие:
            # maker-лимитка не успевала в очередь (12/33 сигналов не налились);
            # тот же вывод C-06 у density_break. None → глобальный maker.
            otype = (getattr(cfg, "density_bounce_entry_order_type", None)
                     or getattr(cfg, "entry_order_type", "market"))
            sig = build_signal(snap, side, w["price"], cfg, len(reasons), reasons,
                               order_type=otype)
            if sig is None:
                continue  # fee-guard / risk не прошли
            sig.strategy = self.name
            play.info("🧱 [%s] ОТСКОК %s от стены %.6f (выстояла %.0fс, цена "
                      "%.6f) → вход @%.4f SL %.4f TP %.4f", sym,
                      _SIDE_RU.get(side, side), w["price"],
                      now - w["first_seen"], last, sig.entry_ref,
                      sig.sl_level, sig.tp_level)
            return sig
        return None

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        """Стена, на которую опирались, исчезла → тезис снят, выходим.

        Якорь стены ≈ возле SL (SL ставился сразу за стеной). Для long ищем
        bid-уровень в (sl, entry], для short — ask-уровень в [entry, sl)."""
        cfg = self.cfg
        if snap is None or snap.last_price is None:
            return None
        if now - tr.ts_open < cfg.active_exit_min_age_sec:
            return None
        if tr.side == "long":
            base = self._wall_baseline(snap.symbol, "bid", snap.bids, now)
            present = _wall_in_range(snap.bids, tr.sl, tr.entry,
                                     cfg.density_wall_mult, cfg.density_min_wall_usd,
                                     baseline=base)
        else:
            base = self._wall_baseline(snap.symbol, "ask", snap.asks, now)
            present = _wall_in_range(snap.asks, tr.entry, tr.sl,
                                     cfg.density_wall_mult, cfg.density_min_wall_usd,
                                     baseline=base)
        if not present:
            return ("density_gone", snap.last_price)
        return None


class DensityBreakStrategy:
    """Стратегия №3: пробой на сносе плотности («прострел»). Зеркало density_bounce.

    ─── Research basis ───
    Руслан Данилов (YouTube 2026, «Разгон депозита» / «Все рабочие стратегии»):
    плотность, которая ДЕРЖАЛА цену, при снятии/пробое даёт «прострел» — *«если
    его снимут, прострел будет хороший»*, *«стопы за плотностью выбивают + крупный
    игрок → импульс»*. Order-flow канон (Bookmap «liquidity void»; Kalena 2026
    wall-detection — removal/absorption; arXiv 2604.20949 — depth раньше flow):
    когда крупная resting-liquidity ПОГЛОЩЕНА (price punched through), за ней
    разрежение → цена ускоряется. Анти-спуфинг (ключ!): торгуем снос ТОЛЬКО у
    стены, которая реально ВЫСТОЯЛА ≥ persist_sec; стена, мелькнувшая <persist —
    спуфинг, НЕ сигнал (в density_bounce то же событие = инвалидация; здесь
    выстоявшая+пробитая = вход ПО ХОДУ пробоя). Знаменатель стены — СКОЛЬЗЯЩИЙ
    baseline за 10–15 мин (RollingBaseline, v0.9.0), как в Kalena, а не
    мгновенный top-25 (тот давал 0/502 входов — стена 5× недостижима).

    Логика (на символ, momentum/breakout — ПРОТИВОПОЛОЖНА fade):
    1. Наблюдаем крупную стену у круглого числа (detect_wall + near_round).
    2. Стена «выстояла» (persisted) если продержалась ≥ density_persist_sec.
    3. Стена ИСЧЕЗЛА с своего уровня И цена ПРОБИЛА его по ходу:
       ask-стена (сопротивление сверху) пробита ВВЕРХ → LONG;
       bid-стена (поддержка снизу) пробита ВНИЗ → SHORT.
       SL за пробитым уровнем (build_signal swept=цена_стены: ложный пробой =
       возврат за уровень), TP по R с общим fee-guard.
    Снос БЕЗ пробоя цены (спуфинг-пулл, цена не дошла) — v1 НЕ торгуем (цена не
    пересекла уровень → нет подтверждения). Выход (should_exit): v1 — только
    универсальные TP/SL/тайм-стоп (ложный пробой режет hard SL); flow-based
    выход — отдельная итерация после валидации базового эджа (no-data-fitting).
    """

    name = "density_break"
    # momentum/breakout в ОБЕ стороны (снос стены вверх ИЛИ вниз). НЕ под полными
    # MR-фильтрами (v0.18.1):
    #  • htf_filtered=False — СИММЕТРИЧНЫЙ EMA-фильтр направления НЕ ставим: режет
    #    прибыльные контртренд-пробои (Quant Signals, 175 backtests: «London
    #    Breakout — universal failure с трендовым фильтром, убирает ~½ сигналов вкл.
    #    profitable counter-trend»). Шорт-пробои здесь прибыльны (live: +11.77 net)
    #    — их сохраняем.
    #  • regime_gated=False — пробой ХОЧЕТ сильного тренда (ADX≥25), MR-гейт «не
    #    торговать в тренд» здесь backwards (резал бы лучшие условия для momentum).
    #  • di_long_gated=True (v0.18.18, C-08) — АСИММЕТРИЧНЫЙ DMI-гейт ТОЛЬКО для
    #    ЛОНГОВ (та же логика, что валидирована на sweep_fade v0.18.4). Контртренд-
    #    ЛОНГ-пробои = bull traps: live n=17 лонгов WR 5.9% / net −158 (p<0.02 при
    #    H0 WR=30%), концентрация BTC/ZEC. Канон: «breakouts against the dominant
    #    trend carry significantly higher probability of bull traps» (NYC Servers,
    #    ToS Indicators, PhotonTrading — 5 источников); асимметрия (лонги ≫ хуже
    #    шортов) совпадает с Kalena 2026 — контртренд-лонги на альт-перпах опаснее
    #    (ликвидационные каскады жёстче на лонг-стороне). Примирение с Quant-Signals:
    #    режем ТОЛЬКО сломанную лонг-сторону, симметричный фильтр НЕ ставим →
    #    profitable counter-trend ШОРТЫ сохранены. Реверсивно (cfg.htf_di_long_gate).
    # Риск-контроль свой: wall_break+persist+round + hard SL за уровнем (ложный
    # пробой = SL) + биржевые TP/SL. Философия B (winners run, без дискреции).
    htf_filtered = False
    regime_gated = False
    di_long_gated = True

    def __init__(self, cfg, symbols: list[str]) -> None:
        self.cfg = cfg
        self._track: dict[str, dict[str, dict | None]] = {
            s: {"bid": None, "ask": None} for s in symbols
        }
        self._base: dict[str, dict[str, RollingBaseline]] = {
            s: self._new_base() for s in symbols
        }
        # v0.18.25 (V1, close-confirmation): пробитый уровень, ждущий ЗАКРЫТИЯ
        # бара за уровнем (канон «реальный пробой = закрытие за уровнем, не
        # first-touch», C-06). {sym: {"side","level","bar"}}. _bar — индекс
        # текущего confirm-бара по символу (граница = закрытие свечи).
        self._pending: dict[str, dict] = {}
        self._bar: dict[str, int] = {}
        # v0.18.29: per-strategy no-trade blacklist (изолировано от вселенной).
        # См. density_break_no_trade_symbols в settings — data-justified.
        self._no_trade: set[str] = set(getattr(cfg, "density_break_no_trade_list", []))
        self._no_trade_logged: dict[str, bool] = {}

    def _new_base(self) -> dict[str, RollingBaseline]:
        w = getattr(self.cfg, "density_baseline_sec", 900.0)
        return {"bid": RollingBaseline(w), "ask": RollingBaseline(w)}

    def _wall_baseline(self, sym: str, book_side: str,
                       levels: list[tuple[float, float]], now: float) -> float | None:
        rb = self._base.setdefault(sym, self._new_base())[book_side]
        rb.add(now, _baseline_avg([sz for _, sz in levels]))
        min_n = getattr(self.cfg, "density_baseline_min_samples", 30)
        return rb.value() if rb.ready(min_n) else None

    def armed(self, symbol: str) -> bool:
        t = self._track.get(symbol)
        return bool(t and ((t["bid"] and t["bid"]["persisted"])
                           or (t["ask"] and t["ask"]["persisted"])))

    def reset(self, symbol: str) -> None:
        if symbol in self._track:
            self._track[symbol] = {"bid": None, "ask": None}

    def ensure_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            self._track.setdefault(s, {"bid": None, "ask": None})
            self._base.setdefault(s, self._new_base())

    def _track_side(self, sym: str, book_side: str,
                    levels: list[tuple[float, float]], now: float) -> float | None:
        """Сопровождаем стену на стороне книги; помечаем «выстоявшую» (persisted).
        Возвращает уровень стены, если она ТОЛЬКО ЧТО исчезла, выстояв ≥persist
        (кандидат на пробой), иначе None. Спуфинг (<persist) → тихо снимаем."""
        cfg = self.cfg
        t = self._track[sym]
        cur = t[book_side]
        base = self._wall_baseline(sym, book_side, levels, now)
        wall = detect_wall(levels, cfg.density_wall_mult, cfg.density_min_wall_usd,
                           baseline=base)
        if cur is None:
            if wall is not None and near_round(wall[0], cfg.density_round_frac):
                t[book_side] = {"price": wall[0], "size0": wall[1],
                                "first_seen": now, "persisted": False}
            return None
        same = (wall is not None
                and abs(wall[0] - cur["price"]) <= cfg.density_round_frac * cur["price"])
        if same:
            if (not cur["persisted"]
                    and now - cur["first_seen"] >= cfg.density_persist_sec):
                cur["persisted"] = True
                play.info("🧱 [%s] плотность %s %.6f выстояла ≥%.0fс — слежу за "
                          "пробоем (снос → прострел)", sym, book_side, cur["price"],
                          cfg.density_persist_sec)
            return None
        # стены на cur.price больше нет → снятие/поглощение
        level = cur["price"]
        persisted = cur["persisted"]
        t[book_side] = None
        return level if persisted else None

    def _try_fire(self, snap: SymbolSnapshot, side: str, level: float,
                  now: float) -> Signal | None:
        """Гейты подтверждения пробоя (CVD follow-through + ob-абсорбция) и
        построение сигнала. Вызывается на ПОДТВЕРЖДЁННОМ пробое (после
        close-confirm, либо сразу в legacy-режиме bar_sec=0)."""
        cfg = self.cfg
        sym = snap.symbol
        last = snap.last_price
        # v0.18.16 (C-06): confirmation ложного пробоя по FOLLOW-THROUGH потоку.
        # Канон (eplanetbrokers/fntradinglab/GrandAlgo): настоящий пробой держит
        # объём/CVD в свою сторону; liquidity-grab = спайк с затуханием и возврат.
        # reversal_momentum(side) = CVD растёт(long)/падает(short) за окно. Это та
        # же функция и то же окно (momentum_window_sec), что sweep_fade использует
        # для tape-shift — НЕ новое число, а существующий канон-параметр «лента
        # качнулась в сторону сделки». Фильтрует grab'ы НА ВСЕХ монетах.
        if getattr(cfg, "density_break_confirm_cvd", False):
            win = getattr(cfg, "momentum_window_sec", 30.0)
            if not reversal_momentum(snap.cvd_samples, side, win):
                play.info("🧱 [%s] пробой %s стены %.6f БЕЗ follow-through CVD "
                          "(вероятно liquidity-grab) — пропускаю", sym,
                          _SIDE_RU.get(side, side), level)
                return None
        # v0.18.16 (C-06 #3): КАНОН-гейт абсорбции. Пробой на глубокой/слоистой
        # книге = grab (resting-ликвидность поглощает движение; Tradeify ES-deep→
        # fade, Bookmap absorption). Структурный сигнал — resting ob_imbalance: не
        # входим, если книга застакана ПРОТИВ пробоя (на круглом уровне глубокого
        # мейджора там жирная resting-ликвидность). Едино для ВСЕХ монет (≠ скип BTC).
        if getattr(cfg, "density_break_require_ob", False):
            ob_min = getattr(cfg, "ob_imbalance_min", 0.58)
            if not ob_supportive(snap.ob_imbalance, side, ob_min):
                play.info("🧱 [%s] пробой %s стены %.6f, но resting-стакан "
                          "застакан против (ob_imb=%s, абсорбция/deep-book grab) "
                          "— пропускаю", sym, _SIDE_RU.get(side, side), level,
                          snap.ob_imbalance)
                return None
        reasons = ["wall_break", "persist", "round"]
        # TP=density_break_take_profit_r (v0.18.10: = глобальный канон 3.5R,
        # Философия B «winners run»; откат подгонки 2.5R на n=25, no-data-fitting).
        # sl_mult=1.0 ЯВНО: стоп density_break СТРУКТУРНЫЙ (за пробитой стеной =
        # инвалидация ложного пробоя, Данилов/Bookmap/Brooks). MAE-расширение
        # (как у sweep_fade) ломает её канон — держали бы провалившиеся пробои.
        # Явный 1.0 иммунизирует от глобального SCALP_SL_RISK_MULT≠1.0.
        # v0.18.16: пер-стратегийный тип входа (taker для пробоя). getattr-
        # резолв: density_break_entry_order_type → fallback на глобальный.
        otype = (getattr(cfg, "density_break_entry_order_type", None)
                 or getattr(cfg, "entry_order_type", "market"))
        sig = build_signal(snap, side, level, cfg, len(reasons), reasons,
                           tp_r=cfg.density_break_take_profit_r, sl_mult=1.0,
                           order_type=otype)
        if sig is None:
            play.info("⛔ [%s] пробой %s стены %.6f, но fee-guard — ход мал, "
                      "комиссия не покрыта", sym, _SIDE_RU.get(side, side), level)
            return None
        sig.strategy = self.name
        play.info("🚀 [%s] ПРОБОЙ %s: плотность %.6f выстояла и пробита (цена "
                  "%.6f) → вход @%.4f SL %.4f TP %.4f", sym,
                  _SIDE_RU.get(side, side), level, last, sig.entry_ref,
                  sig.sl_level, sig.tp_level)
        return sig

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        cfg = self.cfg
        if snap.stale or snap.last_price is None:
            return None
        sym = snap.symbol
        if sym not in self._track:
            self._track[sym] = {"bid": None, "ask": None}
        # v0.18.29: per-strategy no-trade — монета в blacklist → не торгуем
        # (изолировано: другие страты этот символ торгуют). Лог — 1 раз/символ.
        if sym in self._no_trade:
            if not self._no_trade_logged.get(sym):
                play.info("🧱 [%s] density_break: монета в no-trade (data: "
                          "BTC 90%%/ZEC 79%%/TAO 88%% SL) — пропускаю", sym)
                self._no_trade_logged[sym] = True
            return None
        last = snap.last_price
        # ── close-confirmation (v0.18.25, V1) ──────────────────────────────
        # Канон C-06: «реальный пробой = ЗАКРЫТИЕ за уровнем + ретест; avoid
        # entering on the first touch». bar_sec>0 → пробой АРМИТСЯ и подтверждается
        # на ЗАКРЫТИИ бара (цена всё ещё за уровнем = настоящий пробой; вернулась
        # = first-touch фейкаут, отбой). bar_sec=0 → legacy: вход на первом тике.
        # confirm_bar_sec=60с — прецедент v0.11.0 (bar-close для тикового бота).
        bar_sec = getattr(cfg, "density_break_confirm_bar_sec", 0.0) or 0.0
        cur_bar = int(now // bar_sec) if bar_sec > 0 else 0
        prev_bar = self._bar.get(sym)
        self._bar[sym] = cur_bar
        pend = self._pending.get(sym)
        if pend is not None:
            bar_closed = prev_bar is not None and cur_bar != pend["bar"]
            if bar_closed:
                p_side, p_level = pend["side"], pend["level"]
                self._pending.pop(sym, None)
                still = last > p_level if p_side == "long" else last < p_level
                if not still:
                    play.info("🧱 [%s] ЛОЖНЫЙ пробой %s %.6f: цена %.6f вернулась за "
                              "уровень к закрытию бара (first-touch фейкаут) — "
                              "пропускаю", sym, _SIDE_RU.get(p_side, p_side),
                              p_level, last)
                else:
                    sig = self._try_fire(snap, p_side, p_level, now)
                    if sig is not None:
                        return sig
            else:
                return None  # ждём закрытия бара — новых пробоев не армим
        # ask-стена пробита ВВЕРХ → LONG; bid-стена пробита ВНИЗ → SHORT
        for book_side, levels, side in (("ask", snap.asks, "long"),
                                        ("bid", snap.bids, "short")):
            level = self._track_side(sym, book_side, levels, now)
            if level is None:
                continue
            broke = last > level if side == "long" else last < level
            if not broke:
                play.info("🧱 [%s] плотность %s %.6f снята, но цена %.6f не пробила "
                          "— пропускаю (возможно спуфинг-пулл)", sym, book_side,
                          level, last)
                continue
            if bar_sec > 0:
                # АРМ: ждём закрытия бара за уровнем (не входим по first-touch)
                self._pending[sym] = {"side": side, "level": level, "bar": cur_bar}
                play.info("🧱 [%s] %s пробой уровня %.6f (цена %.6f) — жду ЗАКРЫТИЯ "
                          "%.0fс-бара за уровнем (канон: не входим по first-touch)",
                          sym, _SIDE_RU.get(side, side), level, last, bar_sec)
                return None
            # legacy (bar_sec=0): вход на первом тике пробоя
            sig = self._try_fire(snap, side, level, now)
            if sig is not None:
                return sig
        return None

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        # v1: дискреционного выхода нет — пробой ведём общими TP/SL/тайм-стопом
        # (ложный пробой = возврат за уровень режет hard SL). Flow-выход —
        # отдельная итерация после валидации базового эджа (no-data-fitting.mdc).
        return None


def build_strategies(cfg, symbols: list[str]) -> list[Strategy]:
    """Фабрика стратегий по cfg.enabled_strategies (CSV). Неизвестные — скип."""
    enabled = getattr(cfg, "strategy_list", ["sweep_fade"])
    registry: dict[str, type] = {
        SweepFadeStrategy.name: SweepFadeStrategy,
        DensityBounceStrategy.name: DensityBounceStrategy,
        DensityBreakStrategy.name: DensityBreakStrategy,
        SweepFadeCanonStrategy.name: SweepFadeCanonStrategy,
    }
    out: list[Strategy] = []
    for name in enabled:
        cls = registry.get(name)
        if cls is None:
            play.info("⚠️ неизвестная стратегия в конфиге: %s — пропускаю", name)
            continue
        out.append(cls(cfg, symbols))
    if not out:  # защита: всегда хотя бы sweep_fade
        out.append(SweepFadeStrategy(cfg, symbols))
    return out


# v0.18.28: round-robin состояние для resolve (per-symbol). [fingerprint, idx]
# fingerprint отличает сигнальные кластеры (свип разных уровней — разные fp);
# idx — какая страта по счёту берёт этот кластер. Внутри кластера победитель
# стабилен (страта успевает реально войти), следующий кластер — следующая страта.
_RR_STATE: dict[str, list] = {}


def _rr_fingerprint(sig: Signal) -> tuple:
    """Идентичность сигнального кластера: символ + сторона + причины + уровень
    входа. canon-наследники (run) генерят вход canon → одинаковые fp на одном
    свипе; новый свип (другой уровень) → другой fp → кластер → ход следующей."""
    return (sig.symbol, sig.side, tuple(sorted(sig.reasons)),
            round(sig.entry_ref, 6))


def resolve_reset_state() -> None:
    """Сброс round-robin состояния (для тестов)."""
    _RR_STATE.clear()


def resolve(signals: list[Signal]) -> Signal | None:
    """Гард на конфликт по одному символу + round-robin same-side tie-break.

    - нет сигналов → None;
    - есть и long, и short → конфликт направлений, не берём НИЧЕГО;
    - все сигналы в ОДНУ сторону → берём с максимальным score; при РАВЕНСТВЕ
      score — round-robin по кластерам (а не «первый по порядку», как раньше).

    v0.18.28 (запрос пользователя 2026-06-27): A/B-варианты canon-входа (на
    тот момент run/trend; trend удалена v0.18.33) генерят идентичные сигналы (тот же
    символ/сторона/score). Прежний tie-break max(score) при ничьей возвращал
    ПЕРВУЮ по порядку страту — всегда canon — и варианты копили 0 сделок (A/B
    сломан). Теперь среди max-score группы победитель вращается по кластерам:
    каждый новый сигнальный кластер (отличный fingerprint) достаётся следующей
    страте, внутри кластера победитель стабилен. Bybit one-way не держит >1
    позиции на символе → иначе приходится делить единый сигнал, round-robin —
    честное деление без переделки исполнителя.

    v0.18.21: same-side коллизия логируется (замер частоты для решения о
    Partial-брекетах). v0.18.28: лог троттлится per-cluster (прежде спамил
    каждый тик — 124 лога на один кластер в проде).
    """
    if not signals:
        return None
    sides = {s.side for s in signals}
    if len(sides) > 1:
        syms = signals[0].symbol
        names = ",".join(sorted({s.strategy for s in signals}))
        play.info("🛑 [%s] конфликт стратегий (%s): разные направления — "
                  "пропускаю тик", syms, names)
        return None
    max_score = max(s.score for s in signals)
    group = [s for s in signals if s.score == max_score]
    if len(group) > 1:
        # round-robin среди tied max-score: вращаем по кластерам (per-symbol).
        fp = _rr_fingerprint(group[0])
        prev = _RR_STATE.get(group[0].symbol)
        if prev is None or prev[0] != fp:
            idx = (prev[1] + 1) % len(group) if prev else 0
            _RR_STATE[group[0].symbol] = [fp, idx]
            new_cluster = True
        else:
            # Кламп по текущему размеру группы (аудит 2026-07-02): состав
            # tie-группы может СЖАТЬСЯ между тиками при том же fingerprint
            # (например, trend выпал по мигнувшему regime-гейту) — сохранённый
            # idx тогда выходит за границы → IndexError, а resolve() в main
            # вне try/except (валит весь процесс). Модуло сохраняет ротацию.
            idx = prev[1] % len(group)
            new_cluster = False
        win = group[idx]
    else:
        win = group[0]
        # одиночный победитель — не вращаем, но трогаем state для троттла лога,
        # если рядом был same-side кластер (чтобы не логировать устаревший fp).
        fp = _rr_fingerprint(win)
        prev = _RR_STATE.get(win.symbol)
        if prev is None or prev[0] != fp:
            _RR_STATE[win.symbol] = [fp, 0]
            new_cluster = True
        else:
            new_cluster = False
    if len(signals) > 1 and new_cluster:
        losers = ", ".join(f"{s.strategy}(score={s.score})"
                           for s in signals if s is not win)
        play.info("⚖️ [%s] SAME-SIDE КОЛЛИЗИЯ (%s): входит %s (score=%d), "
                  "сигнал потеряли: %s — round-robin по кластерам (v0.18.28)",
                  win.symbol, win.side, win.strategy, win.score, losers)
    return win
