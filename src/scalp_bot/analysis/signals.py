"""Движок orderflow-сигналов scalp_bot (детерминированные правила).

Сетап — «свип ликвидности + поглощение» (mean-reversion fade), канон CAP
(chartwhisperer order-flow 2026, Kalena CVD): сигнал = свип стопов за уровень
+ CVD-дивергенция (поглощение) + reclaim (CHoCH, возврат за уровень) +
разворот ленты. Это и есть вход — без структурного контекста CVD-дивергенция
сама по себе шум (CAP), но лишних факторов канон не добавляет: «HF-скальпинг
выигрывает на 2 факторах; 5+ конфлюенсов недобирают» (traderssecondbrain 2026).

Живой путь (``SweepReclaimDetector``) двухфазный:
  ВЗВОД (arm):  SWEEP (свежий экстремум, собрал стопы) + CVD_DIVERGENCE
                (цена ↓ low / CVD ↑ low; зеркально для short). [оба ОБЯЗ.]
  ВЫСТРЕЛ (fire): RECLAIM (цена вернулась ≥ reclaim_frac за уровень) +
                  REVERSAL_MOMENTUM (CVD качнулся) — вход по ленте. [все ОБЯЗ.]
                  BAR-CLOSE опционален (confirm_bar_sec, v0.14.0 default=0).
  OB_IMBALANCE — гейт стороны (v0.10.0 require_ob_imbalance=True: score≥5).

Аудит v0.9.0 (2026-05-31): funding-перекос и ликвидационный flush УБРАНЫ как
факторы входа — на 502 реальных входах они появлялись в 1 сделке каждый (0.2%),
не гейтили и не каноничны для разворота на 90–120с (funding — 8ч-метрика). Это
устранение factor-noise (канон: «убери фактор — если WR не падает, он был шумом»).

ТФ-выравнивание v0.11.0 (2026-06-01): после удаления time_stop медиана холда
198с, цель TP 3.5R. v0.11.0 добавлял BAR-CLOSE подтверждение (confirm_bar_sec=60с)
для denoise тиков. v0.14.0 ВЫКЛЮЧИЛ его (default=0): канон order-flow прямо против —
«waiting for a candle close can price you out of the move» (Kalena 2026; TradeAlgo),
подтверждать надо ЛЕНТОЙ (разворот CVD + ob_imbalance — у нас уже есть). Механика
bar-close сохранена как опция (confirm_bar_sec>0 = fallback на ожидание бара).

Все функции чистые → юнит-тестируемы без WS.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from scalp_bot.data.aggregates import CvdSample, SymbolSnapshot

# Отдельный логгер-«плейбук»: пошаговый нарратив торговли простым языком,
# чтобы на пальцах видеть, где бот идёт по стратегии верно, а где буксует.
play = logging.getLogger("scalp_bot.play")

_SIDE_RU = {"long": "LONG↑", "short": "SHORT↓"}


@dataclass
class Signal:
    symbol: str
    side: str  # "long" | "short"
    entry_ref: float
    sl_level: float
    tp_level: float
    score: int
    reasons: list[str]
    strategy: str = "sweep_fade"  # какая стратегия породила сигнал (атрибуция)
    # v0.18.16: тип входа, выбранный стратегией (пер-стратегийно). None →
    # executor берёт глобальный cfg.entry_order_type. density_break ставит "market"
    # (taker): пробой не наливается maker-лимиткой (C-06, fill-rate 42.6%).
    entry_order_type: str | None = None
    # regime-фичи на момент сигнала (dict из analysis/regime.py). ТОЛЬКО логирование
    # в таблицу regime_features (meta-labeling, Lopez de Prado AFML Ch3). На торговую
    # логику НЕ влияет. Заполняется в main loop (где есть snap+htf+key_levels).
    regime: dict | None = None
    # v0.18.40: setup-specific observational telemetry (геометрия sweep/wall).
    # Executor/main только сохраняют словарь в setup_features; ни один гейт,
    # resolve, sizing или торговое решение его не читает.
    setup: dict | None = None
    # v0.18.41: отдельный preregistered shadow meta-score. Заполняется main
    # ПОСЛЕ resolve/режим-гейтов и только сохраняется executor; не является
    # Signal.score и не читается торговым контуром.
    meta_label: dict | None = None


def _split_halves(samples: list[CvdSample]) -> tuple[list[CvdSample], list[CvdSample]]:
    """Делит окно на раннюю и позднюю половины по времени."""
    if len(samples) < 4:
        return ([], [])
    mid = len(samples) // 2
    return (samples[:mid], samples[mid:])


def detect_sweep(samples: list[CvdSample], side: str) -> bool:
    """Свежий экстремум: поздняя половина пробила экстремум ранней."""
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    if side == "long":
        return min(s.price for s in late) < min(s.price for s in early)
    return max(s.price for s in late) > max(s.price for s in early)


def cvd_divergence(samples: list[CvdSample], side: str, min_late: int = 0) -> bool:
    """Дивергенция цена↔CVD (поглощение).

    LONG  (bull): late price-min < early price-min, но late cvd-min > early cvd-min.
    SHORT (bear): late price-max > early price-max, но late cvd-max < early cvd-max.

    Строгое неравенство по CVD (>/<, не ≥/≤): на «тонком» окне почти плоский CVD
    давал ложную дивергенцию при равенстве. ``min_late`` — минимум сделок в
    поздней половине (анти «пустота»: дивергенция на 2-3 тиках = шум).
    """
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    if min_late and len(late) < min_late:
        return False
    if side == "long":
        price_lower_low = min(s.price for s in late) < min(s.price for s in early)
        cvd_higher_low = min(s.cvd for s in late) > min(s.cvd for s in early)
        return price_lower_low and cvd_higher_low
    price_higher_high = max(s.price for s in late) > max(s.price for s in early)
    cvd_lower_high = max(s.cvd for s in late) < max(s.cvd for s in early)
    return price_higher_high and cvd_lower_high


def reclaimed(samples: list[CvdSample], side: str, frac: float) -> bool:
    """Reclaim (CAP Rule 2): цена ушла за уровень фитилём, но вернулась внутрь.

    LONG: свипнутый low в поздней половине; цена восстановилась ≥ frac пути
    от свип-экстремума обратно к свипнутому уровню (min ранней половины).
    SHORT — зеркально.
    """
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    last_price = samples[-1].price
    if side == "long":
        prior = min(s.price for s in early)      # свипнутый уровень (поддержка)
        swept = min(s.price for s in late)       # свип-экстремум (ниже)
        excursion = prior - swept
        if excursion <= 0:
            return False
        return last_price >= swept + frac * excursion
    prior = max(s.price for s in early)
    swept = max(s.price for s in late)
    excursion = swept - prior
    if excursion <= 0:
        return False
    return last_price <= swept - frac * excursion


def reversal_momentum(samples: list[CvdSample], side: str, window_sec: float) -> bool:
    """Разворот ленты (CAP Rule 5 / tape-shift): CVD качнулся в сторону сделки.

    LONG: за последние window_sec CVD растёт (агрессия перетекает в buy).
    SHORT: CVD падает. Подтверждает, что разворот НАЧАЛСЯ (не входим в нож).
    """
    if len(samples) < 2:
        return False
    cutoff = samples[-1].ts - window_sec
    recent = [s for s in samples if s.ts >= cutoff]
    if len(recent) < 2:
        return False
    delta_cvd = recent[-1].cvd - recent[0].cvd
    return delta_cvd > 0 if side == "long" else delta_cvd < 0


def ob_supportive(imbalance: float | None, side: str, min_imb: float) -> bool:
    """Стакан накапливается в сторону сделки (top-N bid/(bid+ask))."""
    if imbalance is None:
        return False
    return imbalance >= min_imb if side == "long" else imbalance <= (1.0 - min_imb)


def diagnose(snap: SymbolSnapshot, cfg) -> dict | None:
    """Флаги правил ЖИВОГО детектора для лучшей стороны — funnel-диагностика.

    Возвращает dict булевых флагов (наблюдаемость, НЕ влияет на торговлю):
    sweep/div/reclaim/momentum/ob. Отражает реальные фазы SweepReclaimDetector
    (взвод sweep+div → выстрел reclaim+momentum, ob — бонус), а НЕ legacy-скоринг.
    """
    if snap.stale or snap.last_price is None or len(snap.cvd_samples) < 6:
        return None
    s = snap.cvd_samples
    best = None
    for side in ("long", "short"):
        d = {
            "side": side,
            "sweep": detect_sweep(s, side),
            "div": cvd_divergence(s, side, getattr(cfg, "div_min_late_trades", 0)),
            "ob": ob_supportive(snap.ob_imbalance, side, cfg.ob_imbalance_min),
            "reclaim": reclaimed(s, side, cfg.reclaim_frac),
            "momentum": reversal_momentum(s, side, cfg.momentum_window_sec),
        }
        # «качество» стороны: совпали ли обе фазы детектора (взвод+выстрел).
        d["score"] = sum(1 for k in ("sweep", "div", "reclaim", "momentum") if d[k])
        if best is None or d["score"] > best["score"]:
            best = d
    return best


def flow_invalidated(snap: SymbolSnapshot, side: str, window_sec: float) -> bool:
    """Hard invalidation: ордер-флоу (CVD) развернулся ПРОТИВ позиции.

    LONG-позиция инвалидируется, если лента качнулась в short (CVD падает).
    Все скальп-источники: «exit immediately when order flow flips».
    """
    opp = "short" if side == "long" else "long"
    return reversal_momentum(snap.cvd_samples, opp, window_sec)


def build_signal(snap: SymbolSnapshot, side: str, swept: float, cfg,
                 score: int, reasons: list[str],
                 tp_r: float | None = None,
                 sl_mult: float | None = None,
                 order_type: str | None = None) -> Signal | None:
    """Строит Signal: entry по книге, SL за свипнутым уровнем + буфер,
    TP = tp_r × R (или cfg.take_profit_r если tp_r=None), с fee-guard (цель ≥
    min_target_fee_mult × издержки). tp_r — пер-стратегийный override (density_break
    v0.18.3 использует свой 2.5R, остальные — глобальный 3.5R).

    Цена входа зависит от типа ордера:
    - post_only_limit (maker): ставим по СВОЕЙ стороне книги (long→best_bid,
      short→best_ask). Иначе лимитка пересекает спред и Bybit отменяет
      post-only (баг до v0.3.2: вход брался с чужой стороны → entry_Cancelled).
    - market (taker): референс = цена, по которой реально исполнимся
      (long→best_ask, short→best_bid)."""
    # order_type — пер-стратегийный override (v0.18.16, density_break=market);
    # None → глобальный cfg.entry_order_type.
    otype = order_type if order_type is not None else getattr(
        cfg, "entry_order_type", "market")
    maker = otype == "post_only_limit"
    if maker:
        entry = snap.best_bid if side == "long" else snap.best_ask
    else:
        entry = snap.best_ask if side == "long" else snap.best_bid
    if entry is None or entry <= 0:
        entry = snap.last_price
    if entry is None or entry <= 0:
        return None
    buf = cfg.sl_buffer_bps / 1e4
    if side == "long":
        sl = swept * (1.0 - buf)
        risk = entry - sl
    else:
        sl = swept * (1.0 + buf)
        risk = sl - entry
    if risk <= 0:
        return None
    # Мин-R пол: R ≥ min_risk_fee_mult × round-trip fee, чтобы комиссия была
    # малой долей риска (research: издержки 50-80% профита при тугом стопе; стоп
    # = структура + буфер). Если структурный R меньше пола — отодвигаем SL ЗА
    # уровень (canon «beyond swing + ATR buffer»), TP пересчитываем от итог. R.
    min_risk = getattr(cfg, "min_risk_fee_mult", 0.0) * cfg.round_trip_fee_frac * entry
    if min_risk > 0 and risk < min_risk:
        risk = min_risk
    # base_risk — R-ЕДИНИЦА (структура + мин-R пол). От неё считаем TP, fee-guard
    # и (в should_exit sweep_fade) пороги flow_exit/scratch — «всё про цель/выход».
    # Канон MFE (Sweeney 1988 «Maximum Favorable Excursion»; NexusFi/traders-
    # secondbrain MFE-distribution): цель и профит-лок меряются ходом В ПЛЮС,
    # который от ширины стопа не зависит. Анализ 734 sweep_fade-сделок (30.05–05.06):
    # медиана winner-MFE 2.33R, уровень 1.5R ловит 68% winners — «плечо».
    base_risk = risk
    # Множитель РАСШИРЯЕТ ТОЛЬКО SL (буфер от шума, канон MAE/Sweeney 1988). TP и
    # пороги выхода остаются на base_risk → синхронны с ×1.0 (иначе цель уезжала бы
    # вместе со стопом — это была подгонка v0.18.x). sl_mult — пер-стратегийный
    # override (sweep_fade ×1.5); None → глобальный sl_risk_mult (харнес --sl-mult).
    # density_break ПЕРЕДАЁТ 1.0 явно: его стоп СТРУКТУРНЫЙ (за пробитой стеной =
    # инвалидация тезиса), MAE-расширение ломает её канон «ложный пробой режет SL».
    mult = getattr(cfg, "sl_risk_mult", 1.0) if sl_mult is None else sl_mult
    sl_risk = base_risk * mult if (mult and mult > 0) else base_risk
    sl = entry - sl_risk if side == "long" else entry + sl_risk
    tpr = cfg.take_profit_r if tp_r is None else tp_r
    tp = entry + tpr * base_risk if side == "long" else entry - tpr * base_risk
    # Fee-guard: ход до TP ≥ min_target_fee_mult × round-trip издержек (на base_risk).
    tp_move_frac = (tpr * base_risk) / entry
    if tp_move_frac < cfg.min_target_fee_mult * cfg.round_trip_fee_frac:
        return None
    return Signal(
        symbol=snap.symbol, side=side, entry_ref=entry,
        sl_level=sl, tp_level=tp, score=score, reasons=reasons,
        entry_order_type=order_type,
    )


class SweepReclaimDetector:
    """Двухфазный детектор свип-разворота (канон CAP, разнесённый во времени).

    Проблема одношагового evaluate: sweep/дивергенция («свежий минимум») и
    reclaim («цена вернулась») истинны в РАЗНЫЕ моменты — в один снимок почти
    никогда не совпадают. Поэтому ловим как состояние:

    Фаза ВЗВОД (arm): sweep + CVD-дивергенция у экстремума → запоминаем
      сторону, свипнутый уровень и амплитуду прокола.
    Фаза ВЫСТРЕЛ (fire): в течение arm_timeout_sec, если цена сделала reclaim
      (вернулась ≥ reclaim_frac пути за уровень) И CVD развернулся (momentum) →
      вход по ленте (v0.14.0: без ожидания закрытия бара — канон order-flow).
      ob — бонус-подтверждение стороны (в reasons; гейт при require_ob_imbalance).
      confirm_bar_sec>0 (default 0) — опц. fallback: ждать закрытия N-сек бара.
    """

    def __init__(self, symbol: str, cfg, level_gate=None,
                 entry_order_type: str | None = None,
                 round_gate=None, reclaim_frac: float | None = None) -> None:
        self.symbol = symbol
        self.cfg = cfg
        # v0.18.26 (B): round_gate(swept)->bool. Если задан и вернул True —
        # свип у round-уровня, НЕ взводимся (база sweep_fade фейдит у round хуже
        # микро, scalp_backtest_regime --level-decomp). None — не гейтим (canon
        # фейдит значимые уровни намеренно; density этот детектор не использует).
        self.round_gate = round_gate
        # v0.18.26 (шаг 2): пер-детекторный reclaim_frac override. None →
        # глобальный cfg.reclaim_frac. База sweep_fade ставит 1.0 (full reclaim),
        # canon — свой sweep_fade_canon_reclaim_frac. Изолирует значение по страте.
        self._reclaim_frac = reclaim_frac
        # v0.18.20 (sweep_fade_canon): опциональный гейт значимого уровня.
        # callable(symbol, side, swept) -> str|None (имя уровня). Если задан —
        # ВЗВОД разрешён только когда свип took out ключевой уровень (PDH/PDL/
        # дневной экстремум) — канон CAP «sweep of liquidity pool», а не
        # 3-минутный микро-экстремум. None (default) — поведение базового
        # sweep_fade не изменено.
        self.level_gate = level_gate
        # v0.18.24 (sweep_fade_canon): пер-детекторный тип входа. None →
        # глобальный maker (база sweep_fade, не трогаем). Канон ставит "market"
        # (taker) ПО КАНОНУ Turtle Soup: вход — buy-stop НАД уровнем, срабатывает
        # на возврате цены сквозь уровень = активный вход ПО reclaim (Connors/
        # Raschke 1995 «Street Smarts»; SFP). Пассивная maker-лимитка ниже цены
        # = вход «купить откат вниз», ПРОТИВОПОЛОЖНЫЙ канон-входу вверх (maker
        # был fee-overlay v0.10.0, не из канона). Подтверждение на данных (не
        # причина): канон-maker наливался 0/4 за сутки.
        self.entry_order_type = entry_order_type
        self._armed: dict | None = None
        self._last_wait_log = 0.0
        self._last_bar: int | None = None  # индекс текущего confirm-бара (bar-close)

    @property
    def armed(self) -> bool:
        return self._armed is not None

    def reset(self) -> None:
        self._armed = None

    def _rf(self) -> float:
        """Эффективный reclaim_frac (per-detector override или глобальный)."""
        return (self._reclaim_frac if self._reclaim_frac is not None
                else self.cfg.reclaim_frac)

    def _target(self, a: dict) -> float:
        """Цена reclaim — куда должна вернуться цена, чтобы дать выстрел."""
        if a["side"] == "long":
            return a["swept"] + self._rf() * a["exc"]
        return a["swept"] - self._rf() * a["exc"]

    @staticmethod
    def _setup_features(a: dict, samples: list[CvdSample], now: float,
                        last: float) -> dict:
        """Снимок геометрии sweep/reclaim; только observational telemetry."""
        side = a["side"]
        prior = a["prior"]
        swept = a["swept"]
        cutoff = samples[-1].ts - a["momentum_window_sec"]
        recent = [x for x in samples if x.ts >= cutoff]
        reversal = None
        if len(recent) >= 2:
            raw = recent[-1].cvd - recent[0].cvd
            reversal = raw if side == "long" else -raw
        crossed_level = last >= prior if side == "long" else last <= prior
        return {
            "setup_type": "sweep_reclaim",
            "level_type": a.get("key_level") or "micro_extreme",
            "level_price": a.get("key_level_price") or prior,
            # История формирования/касания уровня в live cache отсутствует.
            "level_age_sec": None,
            "level_touches": None,
            "prior_price": prior,
            "swept_price": swept,
            "sweep_depth_bps": abs(prior - swept) / prior * 1e4
            if prior > 0 else None,
            "outside_duration_sec": now - a["ts"] if crossed_level else None,
            "reclaim_duration_sec": now - a["ts"],
            "cvd_divergence_magnitude": a.get("cvd_divergence_magnitude"),
            "cvd_reversal_magnitude": reversal,
        }

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        cfg = self.cfg
        if snap.stale or snap.last_price is None or len(snap.cvd_samples) < 6:
            return None
        # Bar-close подтверждение (v0.11.0): отмечаем момент закрытия confirm-бара
        # (1м). Выстрел разрешён только на закрытии бара, не на тиках. _last_bar
        # тикаем КАЖДЫЙ вызов, чтобы граница ловилась независимо от arm-состояния.
        bar_sec = getattr(cfg, "confirm_bar_sec", 0.0) or 0.0
        cur_bar = int(now // bar_sec) if bar_sec > 0 else 0
        bar_closed = (bar_sec > 0 and self._last_bar is not None
                      and cur_bar != self._last_bar)
        self._last_bar = cur_bar
        # истечение взвода: reclaim/разворот так и не пришли за таймаут
        if self._armed and now - self._armed["ts"] > cfg.arm_timeout_sec:
            a = self._armed
            play.info("💤 [%s] взвод %s истёк (%.0fс): reclaim %.4f и разворот CVD "
                      "не пришли — снимаю наблюдение",
                      self.symbol, _SIDE_RU.get(a["side"], a["side"]),
                      cfg.arm_timeout_sec, self._target(a))
            self._armed = None
        s = snap.cvd_samples
        # ── фаза ВЗВОД (или переарм на более свежий/глубокий свип) ──
        for side in ("long", "short"):
            if not (detect_sweep(s, side)
                    and cvd_divergence(s, side, getattr(cfg, "div_min_late_trades", 0))):
                continue
            early, late = _split_halves(s)
            if side == "long":
                swept = min(x.price for x in late)
                prior = min(x.price for x in early)
                exc = prior - swept
            else:
                swept = max(x.price for x in late)
                prior = max(x.price for x in early)
                exc = swept - prior
            if exc > 0:
                if side == "long":
                    div_mag = min(x.cvd for x in late) - min(x.cvd for x in early)
                else:
                    div_mag = max(x.cvd for x in early) - max(x.cvd for x in late)
                # v0.18.26 (B): база sweep_fade не фейдит у round-уровня (хуже
                # микро). canon round_gate=None → не задет; density детектор не юзает.
                if self.round_gate is not None and self.round_gate(swept):
                    continue
                key_level = None
                key_level_price = None
                if self.level_gate is not None:
                    level_result = self.level_gate(self.symbol, side, swept)
                    if level_result is None:
                        continue  # свип не took out значимый уровень — не взводимся
                    # Backward-compatible: старые/test callbacks возвращают str;
                    # canon callback v0.18.40 возвращает (type, price) для telemetry.
                    if isinstance(level_result, tuple):
                        key_level, key_level_price = level_result
                    else:
                        key_level = level_result
                was = self._armed
                self._armed = {"side": side, "swept": swept, "exc": exc,
                               "prior": prior, "ts": now,
                               "key_level": key_level,
                               "key_level_price": key_level_price,
                               "cvd_divergence_magnitude": div_mag,
                               "momentum_window_sec": cfg.momentum_window_sec}
                # лог только на НОВЫЙ взвод или смену уровня (не каждый тик)
                if was is None or was["side"] != side or abs(was["swept"] - swept) > 1e-9:
                    absorb = "продавцов выдыхают" if side == "long" else "покупателей выдыхают"
                    key_note = f" [key={key_level}]" if key_level else ""
                    play.info("🎯 [%s] ВЗВОД %s: свип уровня %.4f%s + дивергенция CVD "
                              "(%s). Жду reclaim %.4f (%.0f%% отката) и разворот CVD, "
                              "таймаут %.0fс",
                              self.symbol, _SIDE_RU.get(side, side), swept, key_note,
                              absorb, self._target(self._armed),
                              self._rf() * 100, cfg.arm_timeout_sec)
                    self._last_wait_log = now
            break
        if not self._armed:
            return None
        # ── фаза ВЫСТРЕЛ ──
        a = self._armed
        side = a["side"]
        last = snap.last_price
        target = self._target(a)
        reclaimed_now = last >= target if side == "long" else last <= target
        mom = reversal_momentum(s, side, cfg.momentum_window_sec)
        if not (reclaimed_now and mom):
            # ожидание — троттлим, чтобы не флудить (раз в narrate_interval_sec)
            iv = getattr(cfg, "narrate_interval_sec", 15.0)
            if now - self._last_wait_log >= iv:
                self._last_wait_log = now
                if not reclaimed_now:
                    gap = target - last if side == "long" else last - target
                    play.info("⏳ [%s] жду %s: цена %.4f, до reclaim %.4f не хватает "
                              "%.4f; разворот CVD: %s", self.symbol,
                              _SIDE_RU.get(side, side), last, target, gap,
                              "есть" if mom else "нет")
                else:
                    play.info("⏳ [%s] жду %s: reclaim ✓ (%.4f), но CVD ещё не "
                              "развернулся — вход держу", self.symbol,
                              _SIDE_RU.get(side, side), last)
            return None
        # bar-close подтверждение (v0.11.0): reclaim+разворот есть, но входим
        # только на ЗАКРЫТИИ confirm-бара (denoise тиковых проколов; ТФ входа =
        # ТФ холда/цели). bar_sec=0 → старый тиковый режим.
        if bar_sec > 0 and not bar_closed:
            iv = getattr(cfg, "narrate_interval_sec", 15.0)
            if now - self._last_wait_log >= iv:
                self._last_wait_log = now
                play.info("⏳ [%s] %s: reclaim+разворот ✓ — жду закрытия %.0fс-бара "
                          "для подтверждения (не входим по тиковому проколу)",
                          self.symbol, _SIDE_RU.get(side, side), bar_sec)
            return None
        # стакан как подтверждение стороны сделки. По умолчанию — БОНУС (не
        # блокирует, как в исходном дизайне v0.3.1); гейт включается только при
        # require_ob_imbalance=True. v0.7.0: вернули в бонус — ob-гейт отсекал
        # «жирные» вины (асимметричный payoff важнее WR, см. settings).
        ob_ok = ob_supportive(snap.ob_imbalance, side, cfg.ob_imbalance_min)
        if getattr(cfg, "require_ob_imbalance", False) and not ob_ok:
            iv = getattr(cfg, "narrate_interval_sec", 15.0)
            if now - self._last_wait_log >= iv:
                self._last_wait_log = now
                play.info("⏳ [%s] жду %s: reclaim+разворот ✓, но стакан не "
                          "подтверждает (imb<%.2f) — придерживаю вход",
                          self.symbol, _SIDE_RU.get(side, side),
                          cfg.ob_imbalance_min)
            return None
        # reclaim + разворот совпали → ob_imb как единственный бонус (funding/liq
        # убраны в аудите v0.9.0: 0.2% присутствия на 502 входах, factor-noise).
        reasons = ["sweep", "cvd_div", "reclaim", "mom"]
        if a.get("key_level"):
            reasons.append("key_" + a["key_level"])  # канон-гейт значимого уровня
        if ob_ok:
            reasons.append("ob_imb")
        bonus = [r for r in reasons if r in ("ob_imb",)]
        # Пер-стратегийный множитель ширины SL для sweep_fade (MAE/Sweeney:
        # структурный+fee стоп всё ещё в шумовой зоне ~30% сделок). None →
        # fallback на глобальный sl_risk_mult (харнес --sl-mult работает как был).
        sf_mult = getattr(cfg, "sweep_fade_sl_risk_mult", None)
        sig = build_signal(snap, side, a["swept"], cfg, len(reasons), reasons,
                           sl_mult=sf_mult, order_type=self.entry_order_type)
        if sig is None:
            # reclaim+разворот были, но риск/комиссии не прошли fee-guard
            play.info("⛔ [%s] %s: reclaim+разворот ✓, но fee-guard — цель не "
                      "покрывает комиссии (стоп близко). Снимаю взвод",
                      self.symbol, _SIDE_RU.get(side, side))
            self._armed = None
            return None
        try:
            sig.setup = self._setup_features(a, s, now, last)
        except Exception:
            # Телеметрия не должна менять факт выстрела или ломать вход.
            sig.setup = None
        play.info("🔫 [%s] ВЫСТРЕЛ %s: reclaim ✓ (%.4f≥%.4f) + CVD развернулся ✓ | "
                  "бонусы: %s | score=%d → сигнал на вход @%.4f SL %.4f TP %.4f",
                  self.symbol, _SIDE_RU.get(side, side), last, target,
                  ",".join(bonus) if bonus else "нет", sig.score,
                  sig.entry_ref, sig.sl_level, sig.tp_level)
        self._armed = None
        return sig
