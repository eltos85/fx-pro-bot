"""Собственная SQLite tradecard_bybit: темы, гипотезы, частоты, маленькие победы.

Это **единственное** хранилище, в которое tradecard пишет. В БД ботов он НЕ
пишет ничего (read-only инвариант §11). Хранит:

- ``themes``      — повторяющиеся убыточные паттерны, ставшие «темами» (§4/§7).
- ``hypotheses``  — кандидат-гипотезы решения из 5 Why (advisory, §6/§8).
- ``theme_freq``  — частота темы по неделям (паттерн/100 trades) для momentum.
- ``small_wins``  — OOS-подтверждённые снижения частоты ПОСЛЕ внедрения (§7).

Продвижение гипотезы в реальное изменение конфига бота = только человек,
отдельным одобренным коммитом (strategy-guard.mdc). tradecard лишь трекает статус.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot TEXT NOT NULL,
    mode TEXT NOT NULL,
    code TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    strategy TEXT,
    first_seen_week TEXT NOT NULL,
    last_seen_week TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'observed',
    created_at REAL NOT NULL,
    UNIQUE(bot, mode, code, scope)
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL,
    bot TEXT NOT NULL,
    text TEXT NOT NULL,
    five_why TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    implemented_week TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(theme_id) REFERENCES themes(id)
);
CREATE TABLE IF NOT EXISTS theme_freq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL,
    bot TEXT NOT NULL,
    mode TEXT NOT NULL,
    week TEXT NOT NULL,
    n_pattern INTEGER NOT NULL,
    n_trades INTEGER NOT NULL,
    freq_per_100 REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(theme_id, mode, week)
);
CREATE TABLE IF NOT EXISTS small_wins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER NOT NULL,
    theme_id INTEGER NOT NULL,
    bot TEXT NOT NULL,
    mode TEXT NOT NULL,
    validated_week TEXT NOT NULL,
    baseline_freq REAL NOT NULL,
    oos_freq REAL NOT NULL,
    p_value REAL NOT NULL,
    n_oos INTEGER NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(id)
);
CREATE INDEX IF NOT EXISTS idx_themes_bot ON themes(bot, mode);
CREATE INDEX IF NOT EXISTS idx_freq_theme ON theme_freq(theme_id, mode, week);
"""


@dataclass
class Theme:
    id: int
    bot: str
    mode: str
    code: str
    scope: dict
    strategy: str | None
    first_seen_week: str
    last_seen_week: str
    status: str


@dataclass
class Hypothesis:
    id: int
    theme_id: int
    bot: str
    text: str
    five_why: str | None
    status: str
    implemented_week: str | None


class TradecardDB:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ─── themes ──────────────────────────────────────────────────────────

    def upsert_theme(self, *, bot: str, mode: str, code: str, scope: dict,
                     week: str, strategy: str | None = None) -> int:
        """Зафиксировать/обновить тему. Возвращает theme_id.

        Идемпотентно по (bot, mode, code, scope): первая фиксация ставит
        first_seen_week, повторные — двигают last_seen_week.
        """
        scope_json = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        now = time.time()
        cur = self._conn.execute(
            "SELECT id FROM themes WHERE bot=? AND mode=? AND code=? AND scope=?",
            (bot, mode, code, scope_json))
        row = cur.fetchone()
        if row:
            tid = int(row["id"])
            self._conn.execute(
                "UPDATE themes SET last_seen_week=? WHERE id=?", (week, tid))
            self._conn.commit()
            return tid
        cur = self._conn.execute(
            "INSERT INTO themes (bot,mode,code,scope,strategy,first_seen_week,"
            "last_seen_week,status,created_at) VALUES (?,?,?,?,?,?,?,'observed',?)",
            (bot, mode, code, scope_json, strategy, week, week, now))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_theme(self, theme_id: int) -> Theme | None:
        r = self._conn.execute(
            "SELECT * FROM themes WHERE id=?", (theme_id,)).fetchone()
        return self._theme(r) if r else None

    def set_theme_status(self, theme_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE themes SET status=? WHERE id=?", (status, theme_id))
        self._conn.commit()

    # ─── hypotheses ──────────────────────────────────────────────────────

    def add_hypothesis(self, *, theme_id: int, bot: str, text: str,
                       five_why: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO hypotheses (theme_id,bot,text,five_why,status,created_at)"
            " VALUES (?,?,?,?,'open',?)",
            (theme_id, bot, text, five_why, time.time()))
        self._conn.commit()
        return int(cur.lastrowid)

    def set_hypothesis_status(self, hyp_id: int, status: str,
                              implemented_week: str | None = None) -> None:
        if implemented_week is not None:
            self._conn.execute(
                "UPDATE hypotheses SET status=?, implemented_week=? WHERE id=?",
                (status, implemented_week, hyp_id))
        else:
            self._conn.execute(
                "UPDATE hypotheses SET status=? WHERE id=?", (status, hyp_id))
        self._conn.commit()

    def hypotheses_for_theme(self, theme_id: int) -> list[Hypothesis]:
        rows = self._conn.execute(
            "SELECT * FROM hypotheses WHERE theme_id=? ORDER BY id", (theme_id,)
        ).fetchall()
        return [self._hyp(r) for r in rows]

    def implemented_hypotheses(self, bot: str) -> list[Hypothesis]:
        rows = self._conn.execute(
            "SELECT * FROM hypotheses WHERE bot=? AND implemented_week IS NOT NULL "
            "AND status IN ('implemented','validating') ORDER BY id", (bot,)
        ).fetchall()
        return [self._hyp(r) for r in rows]

    # ─── theme frequency (momentum tracking) ─────────────────────────────

    def record_freq(self, *, theme_id: int, bot: str, mode: str, week: str,
                    n_pattern: int, n_trades: int) -> None:
        freq = (n_pattern / n_trades * 100.0) if n_trades else 0.0
        self._conn.execute(
            "INSERT INTO theme_freq (theme_id,bot,mode,week,n_pattern,n_trades,"
            "freq_per_100,created_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(theme_id,mode,week) DO UPDATE SET "
            "n_pattern=excluded.n_pattern, n_trades=excluded.n_trades, "
            "freq_per_100=excluded.freq_per_100",
            (theme_id, bot, mode, week, n_pattern, n_trades, freq, time.time()))
        self._conn.commit()

    def freq_history(self, theme_id: int, mode: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM theme_freq WHERE theme_id=? AND mode=? ORDER BY week",
            (theme_id, mode)).fetchall()

    # ─── small wins ──────────────────────────────────────────────────────

    def add_small_win(self, *, hypothesis_id: int, theme_id: int, bot: str,
                      mode: str, validated_week: str, baseline_freq: float,
                      oos_freq: float, p_value: float, n_oos: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO small_wins (hypothesis_id,theme_id,bot,mode,"
            "validated_week,baseline_freq,oos_freq,p_value,n_oos,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (hypothesis_id, theme_id, bot, mode, validated_week, baseline_freq,
             oos_freq, p_value, n_oos, time.time()))
        self._conn.commit()
        return int(cur.lastrowid)

    def small_wins(self, bot: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM small_wins WHERE bot=? ORDER BY created_at", (bot,)
        ).fetchall()

    def small_win_count(self, bot: str) -> int:
        r = self._conn.execute(
            "SELECT COUNT(*) AS c FROM small_wins WHERE bot=?", (bot,)).fetchone()
        return int(r["c"] or 0)

    def has_small_win(self, hypothesis_id: int, week: str) -> bool:
        r = self._conn.execute(
            "SELECT 1 FROM small_wins WHERE hypothesis_id=? AND validated_week=?",
            (hypothesis_id, week)).fetchone()
        return r is not None

    # ─── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _theme(r: sqlite3.Row) -> Theme:
        return Theme(
            id=int(r["id"]), bot=r["bot"], mode=r["mode"], code=r["code"],
            scope=json.loads(r["scope"] or "{}"), strategy=r["strategy"],
            first_seen_week=r["first_seen_week"], last_seen_week=r["last_seen_week"],
            status=r["status"])

    @staticmethod
    def _hyp(r: sqlite3.Row) -> Hypothesis:
        return Hypothesis(
            id=int(r["id"]), theme_id=int(r["theme_id"]), bot=r["bot"],
            text=r["text"], five_why=r["five_why"], status=r["status"],
            implemented_week=r["implemented_week"])
