"""SQLite-состояние scalp_bot: сделки + агрегаты для killswitch.

Хранится в ``{data_dir}/scalp_bot.sqlite`` (volume scalp_bot_data).
``realized_pnl_usd`` — расчётный net с учётом ``fees_usd``; для аудита
PnL ground truth = биржевая выписка (stats-collection.mdc), БД —
приблизительный источник для killswitch и трассировки.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

# Regime-фичи (общие для regime_features и shadow_signals). Порядок и состав
# должны совпадать с analysis/regime.py REGIME_COLUMNS (тест-инвариант
# test_regime_columns_match_db). session — TEXT, liq_count — INTEGER,
# остальное REAL.
_FEATURE_COLS = (
    "adx", "regime_ratio", "day_range_pct", "dist_high_pct", "dist_low_pct",
    "spread_bps", "ob_imbalance", "funding_bps", "cvd_slope", "liq_count",
    "session",
    "ret_autocorr", "price_slope_bps_min", "rv_burst", "tape_accel",
    "liq_notional_usd", "liq_buy_frac", "oi_delta_pct", "btc_ret_bps",
    "near_depth_imb", "htf_natr_pct", "htf_bb_width_pct",
)


def _feature_col_type(col: str) -> str:
    if col == "session":
        return "TEXT"
    if col == "liq_count":
        return "INTEGER"
    return "REAL"


_FEATURE_DDL = ",\n    ".join(
    f"{c} {_feature_col_type(c)}" for c in _FEATURE_COLS)

# v0.18.40: setup-specific observational telemetry. Явные SQLite-типы нужны
# для стабильного офлайн-анализа; поля разных семейств в общей строке остаются
# NULL (например wall_* у sweep и swept_* у density).
_SETUP_FEATURE_TYPES = {
    "setup_type": "TEXT",
    "level_type": "TEXT",
    "level_price": "REAL",
    "level_age_sec": "REAL",
    "level_touches": "INTEGER",
    "prior_price": "REAL",
    "swept_price": "REAL",
    "sweep_depth_bps": "REAL",
    "outside_duration_sec": "REAL",
    "reclaim_duration_sec": "REAL",
    "cvd_divergence_magnitude": "REAL",
    "cvd_reversal_magnitude": "REAL",
    "wall_book_side": "TEXT",
    "wall_age_sec": "REAL",
    "wall_initial_size": "REAL",
    "wall_max_size": "REAL",
    "wall_baseline": "REAL",
    "wall_ratio": "REAL",
    "wall_absorption_speed": "REAL",
    "wall_removal_speed": "REAL",
    "break_depth_bps": "REAL",
    "confirm_duration_sec": "REAL",
    "retest_delay_sec": "REAL",
    "retest_distance_bps": "REAL",
    "retest_hold_sec": "REAL",
}
_SETUP_FEATURE_COLS = tuple(_SETUP_FEATURE_TYPES)
_SETUP_FEATURE_DDL = ",\n    ".join(
    f"{c} {t}" for c, t in _SETUP_FEATURE_TYPES.items())

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'sweep_fade',
    status TEXT NOT NULL DEFAULT 'open',
    entry_order_id TEXT,
    ts_close REAL,
    exit REAL,
    pnl_usd REAL,
    fees_usd REAL,
    close_reason TEXT,
    pnl_provisional INTEGER NOT NULL DEFAULT 0,
    pnl_verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ts_close ON trades(ts_close);
CREATE TABLE IF NOT EXISTS regime_features (
    trade_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    {_FEATURE_DDL}
);
CREATE TABLE IF NOT EXISTS shadow_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    blocked_by TEXT NOT NULL,
    entry_ref REAL,
    sl_level REAL,
    tp_level REAL,
    score INTEGER,
    {_FEATURE_DDL}
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_signals(ts);
CREATE INDEX IF NOT EXISTS idx_shadow_strategy ON shadow_signals(strategy);
CREATE TABLE IF NOT EXISTS setup_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    trade_id INTEGER UNIQUE,
    shadow_signal_id INTEGER UNIQUE,
    {_SETUP_FEATURE_DDL},
    CHECK (
        (trade_id IS NOT NULL AND shadow_signal_id IS NULL)
        OR (trade_id IS NULL AND shadow_signal_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_setup_features_ts ON setup_features(ts);
CREATE INDEX IF NOT EXISTS idx_setup_features_strategy
    ON setup_features(strategy);
CREATE TABLE IF NOT EXISTS density_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL,
    symbol TEXT NOT NULL,
    book_side TEXT NOT NULL,
    anchor_price REAL NOT NULL,
    life_sec REAL NOT NULL,
    death_reason TEXT NOT NULL,
    reached_persist INTEGER NOT NULL DEFAULT 0,
    persisted_ts REAL,
    price_start REAL,
    price_persist REAL,
    price_end REAL,
    did_price_approach INTEGER NOT NULL DEFAULT 0,
    max_size REAL,
    round_tier TEXT
);
CREATE INDEX IF NOT EXISTS idx_density_tracks_ts ON density_tracks(ts_start);
CREATE INDEX IF NOT EXISTS idx_density_tracks_symbol ON density_tracks(symbol);
CREATE TABLE IF NOT EXISTS maker_nonfill_shadows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL UNIQUE,
    ts_signal REAL NOT NULL,
    ts_nonfill REAL NOT NULL,
    ts_end REAL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    nonfill_reason TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    risk REAL NOT NULL,
    target_r REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    outcome_1_5r TEXT,
    ts_outcome_1_5r REAL,
    outcome_tp TEXT,
    ts_outcome_tp REAL,
    mfe_r REAL NOT NULL DEFAULT 0,
    mae_r REAL NOT NULL DEFAULT 0,
    mfe_r_60 REAL,
    mae_r_60 REAL,
    mfe_r_180 REAL,
    mae_r_180 REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    last_price REAL,
    last_update REAL
);
CREATE INDEX IF NOT EXISTS idx_maker_shadow_status
    ON maker_nonfill_shadows(status);
CREATE INDEX IF NOT EXISTS idx_maker_shadow_ts
    ON maker_nonfill_shadows(ts_nonfill);
"""


@dataclass
class TradeRow:
    id: int
    ts_open: float
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    score: int
    reasons: str
    mode: str
    strategy: str
    status: str
    entry_order_id: str | None
    ts_close: float | None
    exit: float | None
    pnl_usd: float | None
    fees_usd: float | None
    close_reason: str | None
    pnl_provisional: int = 0
    pnl_verified: int = 0


@dataclass
class StrategyStat:
    """Сводка по одной стратегии за период (для постратегийного мониторинга)."""
    strategy: str
    trades: int
    wins: int
    losses: int
    pnl_usd: float

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else 0.0


class ScalpDB:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "scalp_bot.sqlite")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Идемпотентные миграции для уже существующих БД (volume на VPS)."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "strategy" not in cols:
            # старые сделки до мультистратегии — это sweep_fade
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL "
                "DEFAULT 'sweep_fade'")
        if "pnl_provisional" not in cols:
            # PnL предварительный (оценка), требует сверки с биржей
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN pnl_provisional INTEGER "
                "NOT NULL DEFAULT 0")
        if "pnl_verified" not in cols:
            # PnL сверён с биржевым closedPnl (авторитетный true-up)
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN pnl_verified INTEGER "
                "NOT NULL DEFAULT 0")
        # индекс создаём после миграции (на старой БД колонки ещё не было)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
        # v0.18.31: новые regime-фичи — досоздать недостающие колонки в уже
        # существующей regime_features (CREATE IF NOT EXISTS старую не трогает)
        rcols = {r["name"] for r in
                 self._conn.execute("PRAGMA table_info(regime_features)")}
        for col in _FEATURE_COLS:
            if col not in rcols:
                self._conn.execute(
                    f"ALTER TABLE regime_features ADD COLUMN "
                    f"{col} {_feature_col_type(col)}")
        # v0.18.40: setup_features новая, но миграция также терпит промежуточную
        # dev-схему и идемпотентно досоздаёт typed feature-колонки.
        scols = {r["name"] for r in
                 self._conn.execute("PRAGMA table_info(setup_features)")}
        for col, col_type in _SETUP_FEATURE_TYPES.items():
            if col not in scols:
                self._conn.execute(
                    f"ALTER TABLE setup_features ADD COLUMN {col} {col_type}")

    def close(self) -> None:
        self._conn.close()

    # ─── writes ──────────────────────────────────────────────────────────

    def insert_open(
        self, *, symbol: str, side: str, qty: float, entry: float, sl: float,
        tp: float, score: int, reasons: str, mode: str,
        strategy: str = "sweep_fade",
        entry_order_id: str | None = None, ts_open: float | None = None,
    ) -> int:
        ts = ts_open if ts_open is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,"
            "reasons,mode,strategy,status,entry_order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            (ts, symbol, side, qty, entry, sl, tp, score, reasons, mode,
             strategy, entry_order_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def insert_regime(self, trade_id: int, features: dict,
                      ts: float | None = None) -> None:
        """Записать regime-фичи сделки в отдельную таблицу (meta-labeling,
        Lopez de Prado AFML Ch3). ТОЛЬКО логирование — на торговлю не влияет.
        Идемпотентно (INSERT OR REPLACE по trade_id PK). Молча игнорируется при
        ошибке — логирование никогда не рвёт торговый поток (no-data-fitting.mdc)."""
        if not features:
            return
        t = ts if ts is not None else time.time()
        cols = ("trade_id", "ts") + _FEATURE_COLS
        vals = [trade_id, t] + [features.get(c) for c in _FEATURE_COLS]
        placeholders = ",".join("?" for _ in cols)
        try:
            self._conn.execute(
                f"INSERT OR REPLACE INTO regime_features "
                f"({','.join(cols)}) VALUES ({placeholders})", tuple(vals))
            self._conn.commit()
        except sqlite3.Error:
            # лог-таблица — не критично; main loop тоже обёрнут try/except
            self._conn.rollback()

    def insert_shadow(self, *, symbol: str, side: str, strategy: str,
                      blocked_by: str, features: dict | None,
                      ts: float | None = None, entry_ref: float | None = None,
                      sl_level: float | None = None,
                      tp_level: float | None = None,
                      score: int | None = None) -> int | None:
        """Shadow-лог ОТВЕРГНУТОГО гейтом сигнала (v0.18.31): те же regime-
        фичи + причина блокировки + уровни несостоявшейся сделки (entry/SL/TP
        — чтобы офлайн по клинам восстановить would-be исход и честно измерить
        каждый гейт: спасает от лузов или режет профит). ТОЛЬКО логирование,
        на торговлю не влияет (no-data-fitting.mdc). Молча игнорирует ошибку."""
        t = ts if ts is not None else time.time()
        feats = features or {}
        head = ("ts", "symbol", "side", "strategy", "blocked_by",
                "entry_ref", "sl_level", "tp_level", "score")
        cols = head + _FEATURE_COLS
        vals = [t, symbol, side, strategy, blocked_by,
                entry_ref, sl_level, tp_level, score] \
            + [feats.get(c) for c in _FEATURE_COLS]
        placeholders = ",".join("?" for _ in cols)
        try:
            self._conn.execute(
                f"INSERT INTO shadow_signals ({','.join(cols)}) "
                f"VALUES ({placeholders})", tuple(vals))
            shadow_id = int(self._conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0])
            self._conn.commit()
            return shadow_id
        except sqlite3.Error:
            self._conn.rollback()
            return None

    def insert_setup_features(
        self, *, strategy: str, features: dict | None,
        trade_id: int | None = None, shadow_signal_id: int | None = None,
        ts: float | None = None,
    ) -> int | None:
        """Сохранить setup geometry ровно для одного owner.

        CHECK в SQLite гарантирует XOR ``trade_id``/``shadow_signal_id``.
        ``INSERT OR REPLACE`` делает запись идемпотентной по UNIQUE owner.
        Любая ошибка fail-open: telemetry не влияет на торговый контур.
        """
        if not features or ((trade_id is None) == (shadow_signal_id is None)):
            return None
        t = ts if ts is not None else time.time()
        head = ("ts", "strategy", "trade_id", "shadow_signal_id")
        cols = head + _SETUP_FEATURE_COLS
        vals = [t, strategy, trade_id, shadow_signal_id] + [
            features.get(c) for c in _SETUP_FEATURE_COLS]
        placeholders = ",".join("?" for _ in cols)
        try:
            self._conn.execute(
                f"INSERT OR REPLACE INTO setup_features ({','.join(cols)}) "
                f"VALUES ({placeholders})", tuple(vals))
            row = self._conn.execute(
                "SELECT id FROM setup_features WHERE trade_id IS ? "
                "AND shadow_signal_id IS ?",
                (trade_id, shadow_signal_id),
            ).fetchone()
            self._conn.commit()
            return int(row["id"]) if row is not None else None
        except sqlite3.Error:
            self._conn.rollback()
            return None

    def mark_closed(
        self, trade_id: int, *, exit_price: float, pnl_usd: float,
        fees_usd: float, close_reason: str, ts_close: float | None = None,
        provisional: bool = False,
    ) -> None:
        ts = ts_close if ts_close is not None else time.time()
        self._conn.execute(
            "UPDATE trades SET status='closed', ts_close=?, exit=?, pnl_usd=?, "
            "fees_usd=?, close_reason=?, pnl_provisional=? WHERE id=?",
            (ts, exit_price, pnl_usd, fees_usd, close_reason,
             1 if provisional else 0, trade_id),
        )
        self._conn.commit()

    def update_entry(self, trade_id: int, entry: float) -> None:
        """Обновить entry реальной ценой исполнения (VWAP входных филлов из
        приватного WS execution).

        Для maker-входа это no-op (филл по своей лимит-цене), для MARKET-входа
        (density_break, v0.18.16) реальный avgEntryPrice отличается от
        референса слиппеджем — без обновления REST-реконсиляция restart-сирот
        не матчит сделку по отпечатку avgEntryPrice (допуск 0.001%) и
        provisional-PnL зависает навсегда (audit 2026-06-10, A-3)."""
        if entry <= 0:
            return
        self._conn.execute(
            "UPDATE trades SET entry=? WHERE id=?", (entry, trade_id))
        self._conn.commit()

    def update_levels(self, trade_id: int, *, sl: float, tp: float) -> None:
        """Обновить SL/TP сделки (P-3, audit 2026-06-10, A-2): после
        MARKET-входа со слиппеджем executor сдвигает брекеты на дельту
        реального VWAP-входа и амендит их на бирже — БД должна отражать
        фактические уровни (от них считаются hold-логи и трассировка)."""
        if sl <= 0 or tp <= 0:
            return
        self._conn.execute(
            "UPDATE trades SET sl=?, tp=? WHERE id=?", (sl, tp, trade_id))
        self._conn.commit()

    def finalize_pnl(self, trade_id: int, *, pnl_usd: float,
                     exit_price: float | None = None,
                     close_reason: str | None = None) -> None:
        """Заменить предварительный (оценочный) PnL реальным closedPnl с биржи
        и снять флаг pnl_provisional (после сверки в reconcile).

        ``close_reason`` (опц.): пересчитанный ярлык bracket-выхода. При
        провизорном закрытии exit≈entry → ``bracket_exit_reason`` всегда выдаёт
        ``tp_hit`` (favorable=0). Реальный знак closedPnl это исправляет, иначе
        залипает «tp_hit при минусе» (см. BUILDLOG v0.18.12).
        """
        sets = ["pnl_usd=?", "pnl_provisional=0"]
        args: list = [pnl_usd]
        if exit_price is not None:
            sets.insert(1, "exit=?")
            args.append(exit_price)
        if close_reason is not None:
            sets.append("close_reason=?")
            args.append(close_reason)
        args.append(trade_id)
        self._conn.execute(
            f"UPDATE trades SET {', '.join(sets)} WHERE id=?", tuple(args))
        self._conn.commit()

    def verify_pnl(self, trade_id: int, *, pnl_usd: float,
                   exit_price: float | None = None,
                   close_reason: str | None = None) -> None:
        """Авторитетный true-up против биржевого closedPnl: ставит net, снимает
        provisional И помечает verified (больше не пересверяем — rate-limit).
        closedPnl уже net (офдок close-pnl: gross − openFee − closeFee)."""
        sets = ["pnl_usd=?", "pnl_provisional=0", "pnl_verified=1"]
        args: list = [pnl_usd]
        if exit_price is not None:
            sets.insert(1, "exit=?")
            args.append(exit_price)
        if close_reason is not None:
            sets.append("close_reason=?")
            args.append(close_reason)
        args.append(trade_id)
        self._conn.execute(
            f"UPDATE trades SET {', '.join(sets)} WHERE id=?", tuple(args))
        self._conn.commit()

    # закрытия без биржевого closedPnl (нечего сверять)
    _NON_TRADE_REASONS = ("restart_flat", "entry_Cancelled", "entry_Rejected",
                          "entry_Deactivated", "entry_timeout")

    def provisional_closed_since(self, ts: float) -> list[TradeRow]:
        """Закрытые сделки с оценочным PnL (нужна сверка с биржей), ts_close>=ts."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND pnl_provisional=1 "
            "AND ts_close>=? ORDER BY id", (ts,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def unverified_closed_live_since(self, ts: float) -> list[TradeRow]:
        """Закрытые LIVE-сделки, ещё не сверённые с биржевым closedPnl
        (pnl_verified=0). Канон: REST closedPnl — источник правды для ВСЕХ
        закрытий, не только provisional (иначе WS-дрейф комиссий не чинится).
        Технические закрытия (entry_* / restart_flat) исключаем — нет closedPnl."""
        placeholders = ",".join("?" for _ in self._NON_TRADE_REASONS)
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND mode='live' "
            "AND pnl_verified=0 AND ts_close>=? "
            f"AND (close_reason IS NULL OR close_reason NOT IN ({placeholders})) "
            "ORDER BY id", (ts, *self._NON_TRADE_REASONS)
        ).fetchall()
        return [self._row(r) for r in rows]

    # ─── reads ───────────────────────────────────────────────────────────

    def shadow_rows(self, since_ts: float = 0.0) -> list[dict]:
        """Shadow-лог отвергнутых сигналов (для анализа/тестов), старые→новые."""
        rows = self._conn.execute(
            "SELECT * FROM shadow_signals WHERE ts>=? ORDER BY id", (since_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    def setup_feature_rows(self, since_ts: float = 0.0) -> list[dict]:
        """Setup-specific telemetry, старые→новые (для анализа/тестов)."""
        rows = self._conn.execute(
            "SELECT * FROM setup_features WHERE ts>=? ORDER BY id",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_density_track(self, row: dict) -> None:
        """Жизненный цикл трека стены density_bounce (v0.18.32, телеметрия):
        для офлайн-анализа выживаемости стен и post-wall цены — обоснование
        persist-порога по данным (strategy-guard.mdc). Молча игнорирует ошибку."""
        cols = ("ts_start", "ts_end", "symbol", "book_side", "anchor_price",
                "life_sec", "death_reason", "reached_persist", "persisted_ts",
                "price_start", "price_persist", "price_end",
                "did_price_approach", "max_size", "round_tier")
        vals = [row.get(c) for c in cols]
        placeholders = ",".join("?" for _ in cols)
        try:
            self._conn.execute(
                f"INSERT INTO density_tracks ({','.join(cols)}) "
                f"VALUES ({placeholders})", tuple(vals))
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()

    def density_track_rows(self, since_ts: float = 0.0) -> list[dict]:
        """Треки стен density_bounce (для анализа/тестов), старые→новые."""
        rows = self._conn.execute(
            "SELECT * FROM density_tracks WHERE ts_start>=? ORDER BY id",
            (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    def insert_maker_nonfill_shadow(
        self, *, trade_id: int, ts_signal: float, ts_nonfill: float,
        symbol: str, side: str, strategy: str, nonfill_reason: str,
        entry: float, sl: float, tp: float, target_r: float,
    ) -> int | None:
        """Начать live-контрфактуал maker non-fill.

        Таблица — только telemetry: хранит путь цены после Cancelled/timeout,
        не влияет на входы, фильтры или сопровождение. Идемпотентность по
        trade_id защищает от повторной записи одного несостоявшегося входа.
        """
        risk = abs(entry - sl)
        if risk <= 0 or target_r <= 0:
            return None
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO maker_nonfill_shadows "
                "(trade_id,ts_signal,ts_nonfill,symbol,side,strategy,"
                "nonfill_reason,entry,sl,tp,risk,target_r,last_update) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, ts_signal, ts_nonfill, symbol, side, strategy,
                 nonfill_reason, entry, sl, tp, risk, target_r, ts_nonfill),
            )
            self._conn.commit()
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = self._conn.execute(
                "SELECT id FROM maker_nonfill_shadows WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
            return int(row["id"]) if row is not None else None
        except sqlite3.Error:
            self._conn.rollback()
            return None

    def pending_maker_nonfill_shadows(self) -> list[dict]:
        """Незавершённые maker-контрфактуалы для resume после рестарта."""
        rows = self._conn.execute(
            "SELECT * FROM maker_nonfill_shadows WHERE status='pending' "
            "ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_maker_nonfill_shadow(self, row: dict) -> None:
        """Сохранить текущие milestones maker-контрфактуала."""
        cols = (
            "ts_end", "status", "outcome_1_5r", "ts_outcome_1_5r",
            "outcome_tp", "ts_outcome_tp", "mfe_r", "mae_r",
            "mfe_r_60", "mae_r_60", "mfe_r_180", "mae_r_180",
            "sample_count", "last_price", "last_update",
        )
        vals = [row.get(c) for c in cols] + [row["id"]]
        try:
            self._conn.execute(
                f"UPDATE maker_nonfill_shadows SET "
                f"{','.join(f'{c}=?' for c in cols)} WHERE id=?",
                tuple(vals),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()

    def maker_nonfill_shadow_rows(self, since_ts: float = 0.0) -> list[dict]:
        """Maker non-fill telemetry, старые→новые (для анализа/тестов)."""
        rows = self._conn.execute(
            "SELECT * FROM maker_nonfill_shadows WHERE ts_nonfill>=? "
            "ORDER BY id", (since_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    def regime_for(self, trade_id: int) -> dict | None:
        """Regime-фичи сделки (для анализа/тестов). None если нет записи."""
        row = self._conn.execute(
            "SELECT * FROM regime_features WHERE trade_id=?", (trade_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def open_trades(self) -> list[TradeRow]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id"
        ).fetchall()
        return [self._row(r) for r in rows]

    def realized_pnl_since(self, ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades "
            "WHERE status='closed' AND ts_close>=?",
            (ts,),
        ).fetchone()
        return float(row["s"] or 0.0)

    def total_realized_pnl(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades WHERE status='closed'"
        ).fetchone()
        return float(row["s"] or 0.0)

    def trades_since(self, ts: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE ts_open>=?", (ts,)
        ).fetchone()
        return int(row["c"] or 0)

    def last_sl_close_ts(self, symbol: str, side: str,
                         strategy: str | None = None) -> float | None:
        """ts_close последнего выхода по SL (close_reason='sl_hit') для символа+
        стороны (+ опционально стратегии). Для sl_cooldown_sec: не перефейдить
        провалившийся уровень сразу (см. settings.sl_cooldown_sec).

        v0.18.21: фильтр по strategy — cooldown ПЕР-СТРАТЕГИЙНЫЙ (запрос
        пользователя 2026-06-11). Раньше SL любой страты блокировал ВСЕ
        остальные по символу+стороне (sweep_fade — на 60 мин): density_break/
        density_bounce теряли сигналы из-за чужого стопа, что портило их
        выборку. Тезис «не перефейдить провалившийся уровень» относится к
        логике КОНКРЕТНОЙ страты: SL фейда ничего не говорит о пробое.
        strategy=None — старое поведение (по всем стратам).
        None — такого закрытия не было."""
        q = ("SELECT MAX(ts_close) AS t FROM trades WHERE status='closed' "
             "AND close_reason='sl_hit' AND symbol=? AND side=?")
        args: list = [symbol, side]
        if strategy is not None:
            q += " AND strategy=?"
            args.append(strategy)
        row = self._conn.execute(q, args).fetchone()
        t = row["t"] if row else None
        return float(t) if t is not None else None

    def open_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status='open'"
        ).fetchone()
        return int(row["c"] or 0)

    def stats_by_strategy(self, since: float = 0.0) -> list[StrategyStat]:
        """Постратегийная сводка по ЗАКРЫТЫМ сделкам с ts_close>=since.

        wins/losses считаем по знаку pnl_usd; pnl_usd в БД — net closedPnl
        (с комиссиями, см. модульный docstring). Реконсил-закрытия
        (restart_flat / entry_*) исключаем — это не торговые исходы.
        """
        rows = self._conn.execute(
            "SELECT strategy, "
            "COUNT(*) AS trades, "
            "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses, "
            "COALESCE(SUM(pnl_usd),0) AS pnl "
            "FROM trades WHERE status='closed' AND ts_close>=? "
            "AND close_reason NOT IN ('restart_flat','entry_Cancelled',"
            "'entry_Rejected','entry_Deactivated','entry_timeout') "
            "GROUP BY strategy ORDER BY pnl DESC",
            (since,),
        ).fetchall()
        return [
            StrategyStat(
                strategy=r["strategy"], trades=int(r["trades"] or 0),
                wins=int(r["wins"] or 0), losses=int(r["losses"] or 0),
                pnl_usd=float(r["pnl"] or 0.0),
            )
            for r in rows
        ]

    @staticmethod
    def _row(r: sqlite3.Row) -> TradeRow:
        return TradeRow(
            id=r["id"], ts_open=r["ts_open"], symbol=r["symbol"], side=r["side"],
            qty=r["qty"], entry=r["entry"], sl=r["sl"], tp=r["tp"], score=r["score"],
            reasons=r["reasons"], mode=r["mode"], strategy=r["strategy"],
            status=r["status"], entry_order_id=r["entry_order_id"],
            ts_close=r["ts_close"], exit=r["exit"], pnl_usd=r["pnl_usd"],
            fees_usd=r["fees_usd"], close_reason=r["close_reason"],
            pnl_provisional=r["pnl_provisional"] if "pnl_provisional"
            in r.keys() else 0,
            pnl_verified=r["pnl_verified"] if "pnl_verified" in r.keys() else 0,
        )
