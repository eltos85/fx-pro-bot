"""Read-only доступ к БД ``fx_momentum_bot`` (traceability сигналов входа).

Открываем строго read-only через SQLite URI ``mode=ro`` — запись физически
невозможна (read-only инвариант). tradecard НИЧЕГО не пишет в БД бота.

Используется ТОЛЬКО для контекста входа: ``momentum_decisions`` (executed=1)
даёт ``momentum_value`` / ``atr`` на момент открытия. По P&L БД momentum
**не источник** (она вообще не хранит realized PnL) — истина у брокера
(stats-collection.mdc).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class EntryDecision:
    """Executed-решение momentum-бота (момент открытия сделки)."""
    ts: float                 # epoch сек (UTC) когда залогировано (≈ время открытия)
    symbol_yf: str            # yfinance-символ (EURUSD=X, ...)
    direction: str            # "long" | "short"
    momentum_value: float
    atr: float
    note: str
    # ctx_* — метрики контекста входа (пишутся ботом с 2026-07-03,
    # BUILDLOG). None для старых строк / недоступных данных.
    ctx_ema_dist_atr: float | None = None   # (close−EMA200)/ATR на входе
    ctx_adx: float | None = None            # ADX(14) режим
    ctx_with_htf: bool | None = None        # вход по стороне EMA200?
    ctx_spread_pips: float | None = None    # live-спред на входе


def _parse_dt(raw: str) -> float | None:
    """SQLite ``datetime('now')`` → epoch сек (UTC). None при ошибке."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


class MomentumDBReadOnly:
    """Тонкая read-only обёртка над ``momentum_decisions`` БД бота."""

    def __init__(self, db_path: str) -> None:
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"momentum db not found (read-only): {db_path}")
        self._path = db_path
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                     timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MomentumDBReadOnly":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def executed_decisions(self, *, since_ts: float = 0.0,
                           until_ts: float | None = None) -> list[EntryDecision]:
        """Решения с executed=1 (реальные открытия) в [since_ts, until_ts).

        Берём с запасом по времени (вызывающий match'ит к opening-deal'ам).
        Сортировка по времени.
        """
        # SELECT * — ctx_* колонки появились 2026-07-03 (миграция на стороне
        # бота); в старых копиях БД их может не быть, читаем defensively.
        rows = self._conn.execute(
            "SELECT * FROM momentum_decisions WHERE executed=1 ORDER BY id"
        ).fetchall()

        def _opt_float(row: sqlite3.Row, key: str) -> float | None:
            val = row[key] if key in row.keys() else None
            return float(val) if val is not None else None

        out: list[EntryDecision] = []
        for r in rows:
            ts = _parse_dt(r["created_at"])
            if ts is None:
                continue
            if ts < since_ts:
                continue
            if until_ts is not None and ts >= until_ts:
                continue
            with_htf_raw = r["ctx_with_htf"] if "ctx_with_htf" in r.keys() else None
            out.append(EntryDecision(
                ts=ts, symbol_yf=r["symbol"],
                direction=str(r["direction"]),
                momentum_value=float(r["momentum_value"] or 0.0),
                atr=float(r["atr"] or 0.0), note=str(r["note"] or ""),
                ctx_ema_dist_atr=_opt_float(r, "ctx_ema_dist_atr"),
                ctx_adx=_opt_float(r, "ctx_adx"),
                ctx_with_htf=(None if with_htf_raw is None else bool(with_htf_raw)),
                ctx_spread_pips=_opt_float(r, "ctx_spread_pips")))
        return out
