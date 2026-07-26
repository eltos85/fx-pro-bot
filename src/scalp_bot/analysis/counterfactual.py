"""Каузальный live-трекер контрфактуальных scalp-сетапов.

Tracker принимает уже сформированные стратегиями shadow-candidates и измеряет
их путь только по последующим WS snapshots. Он не создаёт ордера, не читает
будущие бары и не участвует в resolve/gates/sizing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("scalp_bot.counterfactual")


@dataclass(frozen=True)
class CounterfactualCandidate:
    """Typed boundary между стратегией/executor и observational tracker."""

    candidate_key: str
    setup_type: str
    variant: str
    strategy: str
    symbol: str
    side: str
    ts_candidate: float
    ts_entry: float
    entry: float
    sl: float
    tp: float
    target_r: float = 1.5
    horizon_sec: float = 10_800.0
    checkpoint_sec: float = 3_600.0
    state: str = "pending"
    retest_timeout_sec: float | None = None
    legacy_trade_id: int | None = None
    source_trade_id: int | None = None
    source_track_key: str | None = None
    level_type: str | None = None
    level_price: float | None = None
    level_age_sec: float | None = None
    level_touches: int | None = None
    sweep_depth_bps: float | None = None
    outside_duration_sec: float | None = None
    reclaim_duration_sec: float | None = None
    cvd_magnitude: float | None = None
    cvd_divergence_magnitude: float | None = None
    cvd_reversal_magnitude: float | None = None
    cvd_window_sec: float | None = None
    approach_ts: float | None = None
    approach_distance_bps: float | None = None
    retest_delay_sec: float | None = None
    retest_distance_bps: float | None = None
    retest_hold_sec: float | None = None
    retest_tolerance_bps: float | None = None
    wall_persist_sec: float | None = None
    v1_signal_created: bool | None = None
    actual_gate: str | None = None

    def as_row(self) -> dict[str, Any]:
        row = dict(self.__dict__)
        row["risk"] = abs(self.entry - self.sl)
        row["v1_signal_created"] = (
            None if self.v1_signal_created is None else int(self.v1_signal_created)
        )
        return row


def advance_counterfactual(
    row: dict, price: float | None, sample_ts: float, now: float,
) -> bool:
    """Продвинуть outcome одним причинно допустимым price sample.

    Samples с timestamp до hypothetical entry и повтор того же snapshot
    игнорируются. При одной точке first-hit определяется только первым
    наблюдённым пересечением; внутритиковый порядок не выдумывается.
    """
    if row.get("state") != "pending":
        return False
    entry_ts = float(row["ts_entry"])
    if sample_ts < entry_ts or now < entry_ts:
        return False
    last_sample_ts = row.get("last_sample_ts")
    if last_sample_ts is not None and sample_ts <= float(last_sample_ts):
        return False
    risk = float(row.get("risk") or 0.0)
    if risk <= 0:
        return False

    milestone = False
    if price is not None and price > 0:
        entry = float(row["entry"])
        if row["side"] == "long":
            favorable = (price - entry) / risk
            adverse = (entry - price) / risk
        else:
            favorable = (entry - price) / risk
            adverse = (price - entry) / risk
        row["mfe_r"] = max(float(row.get("mfe_r") or 0.0), favorable)
        row["mae_r"] = max(float(row.get("mae_r") or 0.0), adverse)
        row["sample_count"] = int(row.get("sample_count") or 0) + 1
        row["last_price"] = price
        row["last_sample_ts"] = sample_ts
        row["last_update"] = now

        if row.get("outcome_target") is None:
            if adverse >= 1.0:
                row["outcome_target"] = "sl"
                row["ts_outcome_target"] = sample_ts
                milestone = True
            elif favorable >= float(row["target_r"]):
                row["outcome_target"] = "target"
                row["ts_outcome_target"] = sample_ts
                milestone = True

        tp_r = abs(float(row["tp"]) - entry) / risk
        if row.get("outcome_tp") is None:
            if adverse >= 1.0:
                row["outcome_tp"] = "sl"
                row["ts_outcome_tp"] = sample_ts
                milestone = True
            elif favorable >= tp_r:
                row["outcome_tp"] = "tp"
                row["ts_outcome_tp"] = sample_ts
                milestone = True

    elapsed = now - entry_ts
    checkpoints = ((60, 3_600.0), (90, 5_400.0),
                   (120, 7_200.0), (180, 10_800.0))
    configured = float(row.get("checkpoint_sec") or 0.0)
    for minutes, sec in checkpoints:
        # Всегда поддерживаем canonical grid; configured checkpoint может быть
        # короче 60m в тестах и тогда записывается в первый слот.
        due = min(sec, configured) if minutes == 60 and configured > 0 else sec
        key = f"mfe_r_{minutes}"
        if elapsed >= due and row.get(key) is None:
            row[key] = float(row.get("mfe_r") or 0.0)
            row[f"mae_r_{minutes}"] = float(row.get("mae_r") or 0.0)
            milestone = True

    horizon = float(row.get("horizon_sec") or 10_800.0)
    if elapsed >= horizon:
        # Final всегда сохраняет последнюю доступную геометрию.
        row["state"] = "final"
        row["ts_end"] = now
        milestone = True
    return milestone


def advance_retest_entry(row: dict, snap, now: float) -> bool:
    """Каузальный retest-limit state machine по текущим/future snapshots.

    После подтверждения CVD лимитка только *выставляется*. Если цена к этому
    моменту уже ушла от уровня, мы не приписываем ей ретроспективный fill:
    ``waiting_entry_fill`` ждёт следующего реального касания уровня.
    """
    if row.get("state") not in (
            "waiting_retest", "holding", "waiting_entry_fill"):
        return False
    timeout = float(row.get("retest_timeout_sec") or 180.0)
    confirmed = float(row["ts_candidate"])
    if now - confirmed > timeout:
        row["state"] = "expired"
        row["ts_end"] = now
        row["last_update"] = now
        return True
    level = float(row["level_price"])
    price = float(getattr(snap, "last_price"))
    # Момент наблюдения — wall-clock `now`, а НЕ snap.ts (см. контракт часов в
    # CounterfactualTracker.update_snapshot).
    sample_ts = now
    tol_bps = float(row.get("retest_tolerance_bps") or 5.0)
    tol = tol_bps / 1e4 * level
    side = row["side"]
    valid = price >= level - tol if side == "long" else price <= level + tol
    distance = abs(price - level) / level * 1e4
    if row["state"] == "waiting_entry_fill":
        # Buy LIMIT@level исполним только на наблюдаемой цене <= level;
        # Sell LIMIT — >= level. Это запрещает ретроспективный fill на первом
        # касании, которое случилось ДО CVD-confirm.
        filled = price <= level if side == "long" else price >= level
        if filled:
            row["state"] = "pending"
            row["ts_entry"] = now
            row["retest_delay_sec"] = now - confirmed
            row["retest_distance_bps"] = distance
            row["last_sample_ts"] = None
            row["last_update"] = now
            return True
        return False
    if row["state"] == "waiting_retest":
        if abs(price - level) <= tol and valid:
            row["state"] = "holding"
            row["approach_ts"] = now
            row["approach_distance_bps"] = distance
            row["last_sample_ts"] = sample_ts
            row["last_update"] = now
            return True
        return False
    if sample_ts <= float(row.get("last_sample_ts") or 0.0):
        return False
    if not valid:
        row["state"] = "invalidated"
        row["ts_end"] = now
        row["last_update"] = now
        return True
    samples = list(getattr(snap, "cvd_samples", []) or [])
    window = float(row.get("cvd_window_sec") or 30.0)
    if len(samples) < 2:
        return False
    cutoff = samples[-1].ts - window
    recent = [s for s in samples if s.ts >= cutoff]
    if len(recent) < 2:
        return False
    raw = recent[-1].cvd - recent[0].cvd
    magnitude = raw if side == "long" else -raw
    if magnitude <= 0:
        return False
    # CVD подтверждает сетап сейчас; hypothetical LIMIT@wall начинает
    # существовать только с этого момента. Не считаем прошлое касание филлом.
    filled_now = price <= level if side == "long" else price >= level
    row["state"] = "pending" if filled_now else "waiting_entry_fill"
    if filled_now:
        row["ts_entry"] = now
        row["retest_delay_sec"] = now - confirmed
        row["retest_distance_bps"] = distance
    row["retest_hold_sec"] = now - float(row["approach_ts"])
    row["approach_distance_bps"] = min(
        float(row.get("approach_distance_bps") or distance), distance)
    row["cvd_magnitude"] = magnitude
    row["cvd_reversal_magnitude"] = magnitude
    row["last_sample_ts"] = None if filled_now else sample_ts
    row["last_update"] = now
    return True


class CounterfactualTracker:
    """SQLite-backed, idempotent и bounded live tracker."""

    def __init__(self, db, settings, *, now=time.time) -> None:
        self._db = db
        self._cfg = settings
        self._now = now
        self._rows: dict[int, dict] = {}
        self._last_flush: dict[int, float] = {}
        if not getattr(settings, "counterfactual_enabled", True) or db is None:
            return
        try:
            limit = int(getattr(settings, "counterfactual_max_active", 5000))
            for row in db.pending_counterfactual_setups(limit=limit):
                cid = int(row["id"])
                self._rows[cid] = row
                self._last_flush[cid] = float(
                    row.get("last_update") or row["ts_entry"])
        except Exception:
            log.exception("counterfactual resume failed")

    @property
    def active_count(self) -> int:
        return len(self._rows)

    def add(self, candidate: CounterfactualCandidate | dict) -> int | None:
        if not getattr(self._cfg, "counterfactual_enabled", True):
            return None
        row = candidate.as_row() if isinstance(candidate, CounterfactualCandidate) \
            else dict(candidate)
        row.setdefault("risk", abs(float(row["entry"]) - float(row["sl"])))
        if row["risk"] <= 0 or float(row.get("target_r") or 0.0) <= 0:
            return None
        try:
            cid, stored = self._db.insert_counterfactual_setup(row)
            if cid is None or stored is None:
                return None
            if stored.get("state") in (
                    "pending", "waiting_retest", "holding",
                    "waiting_entry_fill"):
                self._rows[cid] = stored
                self._last_flush.setdefault(
                    cid, float(stored.get("last_update") or stored["ts_entry"]))
                self._bound_memory()
            return cid
        except Exception:
            log.exception("counterfactual candidate %s failed",
                          row.get("candidate_key"))
            return None

    def update_snapshot(self, snap, now: float | None = None) -> None:
        if not self._rows or snap is None or getattr(snap, "stale", False):
            return
        current = self._now() if now is None else now
        price = getattr(snap, "last_price", None)
        # ─── Контракт часов ───────────────────────────────────────────────
        # ts_candidate/ts_entry/outcome-таймстемпы живут в wall-clock
        # (time.time), а SymbolSnapshot.ts идёт по time.monotonic: окна
        # CVD/liquidation намеренно защищены от прыжков NTP. Смешивать эти
        # шкалы нельзя — monotonic всегда меньше epoch, поэтому causality-guard
        # `sample_ts < ts_entry` отбрасывал бы КАЖДЫЙ sample (баг v0.18.42,
        # 4927 строк зависли в pending с нулём сэмплов).
        # snap.ts — это момент снятия снимка, а не время тика, поэтому «когда мы
        # посмотрели» корректно выражается через current: те же микросекунды,
        # но в правильной шкале и без разрыва при рестарте.
        sample_ts = current
        flush_sec = float(getattr(
            self._cfg, "counterfactual_flush_sec", 60.0))
        done: list[int] = []
        for cid, row in list(self._rows.items()):
            if row["symbol"] != getattr(snap, "symbol", None):
                continue
            entry_transition = advance_retest_entry(row, snap, current)
            # Snapshot, подтвердивший entry, не используется одновременно как
            # post-entry outcome sample.
            milestone = entry_transition
            if not entry_transition:
                milestone = advance_counterfactual(
                    row, float(price) if price is not None else None,
                    sample_ts, current) or milestone
            due = current - self._last_flush.get(cid, float(row["ts_entry"])) \
                >= flush_sec
            if milestone or due:
                self._db.update_counterfactual_setup(row)
                self._last_flush[cid] = current
            if row.get("state") in (
                    "final", "expired", "invalidated", "overflow"):
                done.append(cid)
        for cid in done:
            self._rows.pop(cid, None)
            self._last_flush.pop(cid, None)

    def update_states(self, states: dict, now: float | None = None) -> None:
        current = self._now() if now is None else now
        symbols = {row["symbol"] for row in self._rows.values()}
        for symbol in symbols:
            state = states.get(symbol)
            if state is not None:
                self.update_snapshot(state.snapshot(), current)
        self._sweep_unobserved(states, current)

    def _sweep_unobserved(self, states: dict, now: float) -> None:
        """Закрыть кандидатов, за которыми больше некому наблюдать.

        Символ мог уйти из вселенной при ротации — тогда update_snapshot по нему
        не вызовется уже никогда, и строка висела бы pending вечно, занимая слот
        в counterfactual_max_active. Закрываем как ``abandoned`` и только после
        истечения горизонта: outcome_* остаются как есть, поэтому недонаблюдённая
        строка не попадёт в статистику (отчёты фильтруют по outcome_*).
        """
        done: list[int] = []
        for cid, row in list(self._rows.items()):
            if row["symbol"] in states:
                continue
            horizon = float(row.get("horizon_sec") or 10_800.0)
            if now - float(row["ts_entry"]) < horizon:
                continue
            row["state"] = "abandoned"
            row["ts_end"] = now
            row["last_update"] = now
            try:
                self._db.update_counterfactual_setup(row)
            except Exception:
                log.exception("counterfactual abandon #%s failed", cid)
            done.append(cid)
        for cid in done:
            self._rows.pop(cid, None)
            self._last_flush.pop(cid, None)

    def flush_all(self) -> None:
        for row in self._rows.values():
            self._db.update_counterfactual_setup(row)

    def _bound_memory(self) -> None:
        maximum = max(1, int(getattr(
            self._cfg, "counterfactual_max_active", 5000)))
        if len(self._rows) <= maximum:
            return
        oldest = sorted(self._rows, key=lambda cid: (
            float(self._rows[cid]["ts_entry"]), cid))
        for cid in oldest[:len(self._rows) - maximum]:
            # Честный terminal marker лучше, чем pending-строка, путь которой
            # фактически перестали наблюдать.
            row = self._rows[cid]
            row["state"] = "overflow"
            row["ts_end"] = self._now()
            self._db.update_counterfactual_setup(row)
            self._rows.pop(cid, None)
            self._last_flush.pop(cid, None)
