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
                  REVERSAL_MOMENTUM (CVD качнулся) + BAR-CLOSE подтверждение
                  (держится до закрытия confirm_bar_sec-бара). [все ОБЯЗ.]
  OB_IMBALANCE — гейт стороны (v0.10.0 require_ob_imbalance=True: score≥5).

Аудит v0.9.0 (2026-05-31): funding-перекос и ликвидационный flush УБРАНЫ как
факторы входа — на 502 реальных входах они появлялись в 1 сделке каждый (0.2%),
не гейтили и не каноничны для разворота на 90–120с (funding — 8ч-метрика). Это
устранение factor-noise (канон: «убери фактор — если WR не падает, он был шумом»).

ТФ-выравнивание v0.11.0 (2026-06-01): после удаления time_stop медиана холда
198с (вины 251с, до 16мин), цель TP 3.5R≈1.55% = 5-15м движение, а триггер
стрелял по 30с-momentum на ТИКАХ → рассинхрон. Добавлено BAR-CLOSE подтверждение
(confirm_bar_sec=60с): вход на ЗАКРЫТИИ 1м-бара, не на тиковом проколе. Канон:
подтверждать на close таймфрейма сделки (Al Brooks 2012; chartwhisperer CAP
Rule 2/5 CHoCH; StratBase 2026 — тест на 1м-барах, confirm на close).

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
                 score: int, reasons: list[str]) -> Signal | None:
    """Строит Signal: entry по книге, SL за свипнутым уровнем + буфер,
    TP = take_profit_r × R, с fee-guard (цель ≥ min_target_fee_mult × издержки).

    Цена входа зависит от типа ордера:
    - post_only_limit (maker): ставим по СВОЕЙ стороне книги (long→best_bid,
      short→best_ask). Иначе лимитка пересекает спред и Bybit отменяет
      post-only (баг до v0.3.2: вход брался с чужой стороны → entry_Cancelled).
    - market (taker): референс = цена, по которой реально исполнимся
      (long→best_ask, short→best_bid)."""
    maker = getattr(cfg, "entry_order_type", "market") == "post_only_limit"
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
        sl = entry - risk if side == "long" else entry + risk
    tp = entry + cfg.take_profit_r * risk if side == "long" else entry - cfg.take_profit_r * risk
    # Fee-guard: ход до TP ≥ min_target_fee_mult × round-trip издержек.
    tp_move_frac = (cfg.take_profit_r * risk) / entry
    if tp_move_frac < cfg.min_target_fee_mult * cfg.round_trip_fee_frac:
        return None
    return Signal(
        symbol=snap.symbol, side=side, entry_ref=entry,
        sl_level=sl, tp_level=tp, score=score, reasons=reasons,
    )


class SweepReclaimDetector:
    """Двухфазный детектор свип-разворота (канон CAP, разнесённый во времени).

    Проблема одношагового evaluate: sweep/дивергенция («свежий минимум») и
    reclaim («цена вернулась») истинны в РАЗНЫЕ моменты — в один снимок почти
    никогда не совпадают. Поэтому ловим как состояние:

    Фаза ВЗВОД (arm): sweep + CVD-дивергенция у экстремума → запоминаем
      сторону, свипнутый уровень и амплитуду прокола.
    Фаза ВЫСТРЕЛ (fire): в течение arm_timeout_sec, если цена сделала reclaim
      (вернулась ≥ reclaim_frac пути за уровень) И CVD развернулся (momentum) И
      это подтвердилось на ЗАКРЫТИИ confirm-бара (confirm_bar_sec=60с, v0.11.0 —
      denoise: тиковый прокол не триггерит, ТФ входа = ТФ холда/цели) → вход.
      ob — бонус-подтверждение стороны (в reasons; гейт при require_ob_imbalance).
    """

    def __init__(self, symbol: str, cfg) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self._armed: dict | None = None
        self._last_wait_log = 0.0
        self._last_bar: int | None = None  # индекс текущего confirm-бара (bar-close)

    @property
    def armed(self) -> bool:
        return self._armed is not None

    def reset(self) -> None:
        self._armed = None

    def _target(self, a: dict) -> float:
        """Цена reclaim — куда должна вернуться цена, чтобы дать выстрел."""
        if a["side"] == "long":
            return a["swept"] + self.cfg.reclaim_frac * a["exc"]
        return a["swept"] - self.cfg.reclaim_frac * a["exc"]

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
                was = self._armed
                self._armed = {"side": side, "swept": swept, "exc": exc, "ts": now}
                # лог только на НОВЫЙ взвод или смену уровня (не каждый тик)
                if was is None or was["side"] != side or abs(was["swept"] - swept) > 1e-9:
                    absorb = "продавцов выдыхают" if side == "long" else "покупателей выдыхают"
                    play.info("🎯 [%s] ВЗВОД %s: свип уровня %.4f + дивергенция CVD "
                              "(%s). Жду reclaim %.4f (%.0f%% отката) и разворот CVD, "
                              "таймаут %.0fс",
                              self.symbol, _SIDE_RU.get(side, side), swept, absorb,
                              self._target(self._armed), cfg.reclaim_frac * 100,
                              cfg.arm_timeout_sec)
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
        if ob_ok:
            reasons.append("ob_imb")
        bonus = [r for r in reasons if r in ("ob_imb",)]
        sig = build_signal(snap, side, a["swept"], cfg, len(reasons), reasons)
        if sig is None:
            # reclaim+разворот были, но риск/комиссии не прошли fee-guard
            play.info("⛔ [%s] %s: reclaim+разворот ✓, но fee-guard — цель не "
                      "покрывает комиссии (стоп близко). Снимаю взвод",
                      self.symbol, _SIDE_RU.get(side, side))
            self._armed = None
            return None
        play.info("🔫 [%s] ВЫСТРЕЛ %s: reclaim ✓ (%.4f≥%.4f) + CVD развернулся ✓ | "
                  "бонусы: %s | score=%d → сигнал на вход @%.4f SL %.4f TP %.4f",
                  self.symbol, _SIDE_RU.get(side, side), last, target,
                  ",".join(bonus) if bonus else "нет", sig.score,
                  sig.entry_ref, sig.sl_level, sig.tp_level)
        self._armed = None
        return sig
