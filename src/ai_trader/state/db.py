"""SQLite-хранилище AI-Trader.

Хранит:
- positions: открытые/закрытые позиции (только наши, по orderLinkId='ai_*')
- decisions: полный audit-trail каждого решения LLM
  (timestamp, prompt, response, parsed action, выполнено или нет, error)
- daily_pnl: дневная статистика для killswitch

Отдельная БД от bybit_bot и fx_pro_bot — никаких пересечений.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator


@dataclass
class AiPosition:
    id: int
    symbol: str
    side: str  # "Buy" / "Sell"
    qty: float
    entry_price: float
    sl_price: float | None
    tp_price: float | None
    leverage: int
    order_link_id: str
    opened_at: str
    closed_at: str | None
    exit_price: float | None
    realized_pnl_usd: float | None
    close_reason: str | None
    llm_reason: str  # rationale из LLM
    # v0.13 (2026-05-18): meta-cognition поля Nof1-style. Заставляют LLM
    # явно посчитать (а не «прикинуть») уверенность, риск и заранее
    # сформулировать условие, при котором тезис сделки неверен.
    # См. BUILDLOG_AI_TRADER.md v0.13 + AI_TRADER_PROPOSAL_ALPHA_ARENA.md.
    confidence: float | None = None  # 0.0-1.0, обязательно при open
    invalidation_condition: str | None = None  # observable exit signal
    risk_usd_declared: float | None = None  # |entry-SL|*qty по расчёту LLM
    # v0.18 (2026-05-25): "gross" → realized_pnl_usd посчитан как
    # ``(exit - entry) × qty`` (без trading fee и funding). "net" →
    # значение синкнуто с Bybit ``closedPnl`` через ``get_closed_pnl``
    # API (учтены fee, **но НЕ funding** — Bybit closedPnl это не
    # включает, см. v0.21 ниже). При open всегда NULL, при close
    # сначала "gross" (немедленно), потом ``_reconcile_pnl_to_net()``
    # на следующем full-cycle перезаписывает в "net" если API доступен.
    # KillSwitch и любая стата по PnL должна предпочитать записи "net".
    pnl_source: str | None = None
    # v0.21 (2026-05-28): funding settlements за время жизни позиции.
    # Bybit на perpetual futures каждые 8ч (00:00/08:00/16:00 UTC) делает
    # settlement: long платит при rate>0, short при rate<0; в обратном
    # случае получает. Если позиция не пересекает settlement timestamp —
    # funding_usd=0. closedPnl от Bybit (поле realized_pnl_usd при
    # pnl_source='net') funding НЕ включает, поэтому храним отдельно.
    # Полный net = realized_pnl_usd + funding_usd. Заполняется
    # ``_reconcile_funding()`` через `get_transaction_log` (type=SETTLEMENT).
    # Отрицательное значение = бот заплатил, положительное = бот получил.
    funding_usd: float | None = None
    # v0.30 (2026-05-28): macro_thesis cited at open (THESIS DISCIPLINE port
    # из FX-trader). Это "WHY this position exists" — dominant macro driver(s)
    # из per-asset hierarchy. Например для BTC long: "ETF net inflow $1.2B
    # last 5d + DXY -0.8% testing 98.5 support + Fed dovish minutes".
    # Дополняет invalidation_condition (v0.13): invalidation = OBSERVABLE
    # signal (price level / indicator value), macro_thesis = НАРРАТИВ.
    # Перечитывается каждый цикл в LIVE-строке (исправляет паттерн
    # «closes by 1H MACD flip ignoring entry thesis», см. FX-trader
    # 22/26 closes audit, BUILDLOG_AI_FX_TRADER.md 2026-05-26).
    # Обязательное на open (executor валидирует ≥50 chars).
    macro_thesis: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL,
    tp_price REAL,
    leverage INTEGER NOT NULL,
    order_link_id TEXT NOT NULL UNIQUE,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    realized_pnl_usd REAL,
    close_reason TEXT,
    llm_reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    ts TEXT NOT NULL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    response_raw TEXT,
    parsed_action TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS daily_pnl (
    day TEXT PRIMARY KEY,
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    n_trades INTEGER NOT NULL DEFAULT 0,
    n_wins INTEGER NOT NULL DEFAULT 0,
    api_cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class AiTraderStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Идемпотентные миграции для существующих БД на VPS.

        SQLite не поддерживает ``ADD COLUMN IF NOT EXISTS``, поэтому
        проверяем через ``pragma_table_info``. ``ALTER TABLE ADD COLUMN``
        с NULL default безопасен для существующих строк.

        v0.13 (2026-05-18): meta-cognition поля для open positions
        (см. AI_TRADER_PROPOSAL_ALPHA_ARENA.md, Nof1-style schema).

        v0.30 (2026-05-28): institutional rewrite (порт FX-trader patterns).
        positions.macro_thesis — pre-registered dominant macro driver
        cited at open, перечитывается каждый цикл в LIVE-строке (Phase 1
        FX-bot pattern, BUILDLOG_AI_FX_TRADER.md 2026-05-26). Дополняет
        существующее `invalidation_condition` (v0.13): invalidation =
        observable signal (price level), macro_thesis = WHY position
        exists (макро-нарратив).
        decisions.thesis_status / thesis_invalidator — обязательные при
        close (см. THESIS DISCIPLINE в SYSTEM_PROMPT). Поля сохраняются
        в `decisions` (audit-trail), а не в `positions`, потому что
        position может закрываться по разным triggers в разное время
        (broker SL, LLM close, review close) — БД хранит decision-time
        анализ.
        decisions.aggregate_uncertainty / sentiment_items_json — 5-dim
        news sentiment по итерации, нужно для аудита `>0.7 → reject` gate.
        decisions.macro_rates_snapshot — точная картинка DXY/UST10Y/BTC.D
        на момент решения (опционально, для аудита расхождений).
        """
        positions_cols = {row[1] for row in c.execute("PRAGMA table_info(positions)")}
        decisions_cols = {row[1] for row in c.execute("PRAGMA table_info(decisions)")}
        per_table: list[tuple[str, set[str], list[tuple[str, str]]]] = [
            (
                "positions",
                positions_cols,
                [
                    ("confidence", "ALTER TABLE positions ADD COLUMN confidence REAL"),
                    ("invalidation_condition", "ALTER TABLE positions ADD COLUMN invalidation_condition TEXT"),
                    ("risk_usd_declared", "ALTER TABLE positions ADD COLUMN risk_usd_declared REAL"),
                    # v0.18: после ALTER старые closed-позиции остаются с pnl_source=NULL
                    # — это значит "до миграции" и трактуется как gross (см. dataclass).
                    ("pnl_source", "ALTER TABLE positions ADD COLUMN pnl_source TEXT"),
                    # v0.21: funding settlements за время жизни позиции (см. dataclass).
                    ("funding_usd", "ALTER TABLE positions ADD COLUMN funding_usd REAL"),
                    # v0.30: macro thesis cited at open (THESIS DISCIPLINE port из FX-trader).
                    ("macro_thesis", "ALTER TABLE positions ADD COLUMN macro_thesis TEXT"),
                ],
            ),
            (
                "decisions",
                decisions_cols,
                [
                    # v0.30: thesis classification at close (audit-trail per decision).
                    ("thesis_status", "ALTER TABLE decisions ADD COLUMN thesis_status TEXT"),
                    ("thesis_invalidator", "ALTER TABLE decisions ADD COLUMN thesis_invalidator TEXT"),
                    # v0.30: 5-dim news sentiment per-cycle.
                    ("aggregate_uncertainty", "ALTER TABLE decisions ADD COLUMN aggregate_uncertainty REAL"),
                    ("sentiment_items_json", "ALTER TABLE decisions ADD COLUMN sentiment_items_json TEXT"),
                    # v0.30: macro snapshot at decision time (optional audit).
                    ("macro_rates_snapshot", "ALTER TABLE decisions ADD COLUMN macro_rates_snapshot TEXT"),
                ],
            ),
        ]
        for _table, existing, migrations in per_table:
            for col_name, ddl in migrations:
                if col_name not in existing:
                    c.execute(ddl)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ─── Decisions audit-trail ───────────────────────────────────────────

    def log_decision(
        self,
        *,
        cycle: int,
        prompt_system: str,
        prompt_user: str,
        response_raw: str | None,
        parsed_action: dict[str, Any] | None,
        executed: bool,
        error: str | None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: float | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO decisions
                (cycle, ts, prompt_system, prompt_user, response_raw,
                 parsed_action, executed, error, tokens_input, tokens_output, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle,
                    datetime.now(tz=UTC).isoformat(),
                    prompt_system,
                    prompt_user,
                    response_raw,
                    json.dumps(parsed_action) if parsed_action else None,
                    1 if executed else 0,
                    error,
                    tokens_input,
                    tokens_output,
                    cost_usd,
                ),
            )
            return int(cur.lastrowid or 0)

    # ─── Positions ───────────────────────────────────────────────────────

    def open_position(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        sl_price: float | None,
        tp_price: float | None,
        leverage: int,
        order_link_id: str,
        llm_reason: str,
        confidence: float | None = None,
        invalidation_condition: str | None = None,
        risk_usd_declared: float | None = None,
        macro_thesis: str | None = None,
    ) -> int:
        """Открыть позицию с записью meta-cognition полей.

        v0.13: ``confidence`` / ``invalidation_condition`` / ``risk_usd_declared``
        обязательны на стороне promp'а (parser требует), но дефолтные
        ``None`` сохраняются для backward compatibility (старые тесты,
        потенциальные future паттерны без этих полей).

        v0.30: ``macro_thesis`` — обязательное при open в production-промпте
        (THESIS DISCIPLINE), default ``None`` для backward compat в тестах.
        """
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO positions
                (symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                 order_link_id, opened_at, llm_reason,
                 confidence, invalidation_condition, risk_usd_declared,
                 macro_thesis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    side,
                    qty,
                    entry_price,
                    sl_price,
                    tp_price,
                    leverage,
                    order_link_id,
                    datetime.now(tz=UTC).isoformat(),
                    llm_reason,
                    confidence,
                    invalidation_condition,
                    risk_usd_declared,
                    macro_thesis,
                ),
            )
            return int(cur.lastrowid or 0)

    def close_position(
        self,
        position_id: int,
        *,
        exit_price: float,
        realized_pnl_usd: float,
        close_reason: str,
        pnl_source: str = "gross",
    ) -> None:
        """Закрыть позицию в БД и обновить daily_pnl.

        ``pnl_source``: "gross" (default — расчётный
        ``(exit-entry)*qty``) либо "net" (точный ``closedPnl`` от
        Bybit с уже вычтенными fee+funding, см. v0.18).

        ``daily_pnl`` инкремент НЕ зависит от ``pnl_source`` — туда
        кладём то значение что есть. Если позже ``update_pnl_to_net``
        перезапишет ``realized_pnl_usd`` другим числом — он же
        корректирует ``daily_pnl`` на разницу.
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE positions
                SET closed_at = ?, exit_price = ?, realized_pnl_usd = ?,
                    close_reason = ?, pnl_source = ?
                WHERE id = ?
                """,
                (
                    datetime.now(tz=UTC).isoformat(),
                    exit_price,
                    realized_pnl_usd,
                    close_reason,
                    pnl_source,
                    position_id,
                ),
            )
            today = date.today().isoformat()
            won = 1 if realized_pnl_usd > 0 else 0
            c.execute(
                """
                INSERT INTO daily_pnl (day, realized_pnl_usd, n_trades, n_wins)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(day) DO UPDATE SET
                    realized_pnl_usd = realized_pnl_usd + excluded.realized_pnl_usd,
                    n_trades = n_trades + 1,
                    n_wins = n_wins + excluded.n_wins
                """,
                (today, realized_pnl_usd, won),
            )

    def update_pnl_to_net(
        self,
        position_id: int,
        *,
        new_realized_pnl_usd: float,
        new_exit_price: float | None = None,
    ) -> None:
        """v0.18: пересчитать ``realized_pnl_usd`` и (опц.) ``exit_price``
        на точные net-значения из Bybit ``closedPnl``.

        Идемпотентно: если запись уже ``pnl_source='net'`` или новое
        значение совпадает с текущим — изменений нет, ``daily_pnl`` не
        двигается. При реальной правке корректирует ``daily_pnl`` на
        разницу (старое gross заменяется новым net), `n_wins` тоже
        пересчитывается если сторона прибыли поменялась после fee.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd, pnl_source, closed_at FROM positions "
                "WHERE id = ?",
                (position_id,),
            ).fetchone()
            if row is None:
                return
            if row["pnl_source"] == "net":
                return
            old_pnl = float(row["realized_pnl_usd"] or 0.0)
            closed_at = row["closed_at"]
            diff = new_realized_pnl_usd - old_pnl
            won_old = 1 if old_pnl > 0 else 0
            won_new = 1 if new_realized_pnl_usd > 0 else 0
            wins_diff = won_new - won_old

            if new_exit_price is not None:
                c.execute(
                    """
                    UPDATE positions
                    SET realized_pnl_usd = ?, exit_price = ?, pnl_source = 'net'
                    WHERE id = ?
                    """,
                    (new_realized_pnl_usd, new_exit_price, position_id),
                )
            else:
                c.execute(
                    """
                    UPDATE positions
                    SET realized_pnl_usd = ?, pnl_source = 'net'
                    WHERE id = ?
                    """,
                    (new_realized_pnl_usd, position_id),
                )

            if abs(diff) > 1e-9 or wins_diff != 0:
                # daily_pnl привязан к дню закрытия, не «сегодня»
                day = (closed_at or "")[:10] or date.today().isoformat()
                c.execute(
                    """
                    UPDATE daily_pnl
                    SET realized_pnl_usd = realized_pnl_usd + ?,
                        n_wins = MAX(0, n_wins + ?)
                    WHERE day = ?
                    """,
                    (diff, wins_diff, day),
                )

    def update_funding(
        self,
        position_id: int,
        *,
        funding_usd: float,
    ) -> None:
        """v0.21: записать funding_usd для уже закрытой позиции.

        Идемпотентно: пере-вызов с тем же значением — no-op. При первой
        записи funding включается в ``daily_pnl.realized_pnl_usd``
        (он считается net+funding для full picture). При повторной
        правке (например после задержки биржевой записи) ``daily_pnl``
        двигается на разницу.

        Если позиция ещё не закрыта — функция игнорируется (funding имеет
        смысл только для закрытых позиций; для открытых funding-cost
        отображается в context.py для LLM на лету).
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT funding_usd, closed_at FROM positions WHERE id = ?",
                (position_id,),
            ).fetchone()
            if row is None:
                return
            if row["closed_at"] is None:
                return
            old_funding = float(row["funding_usd"] or 0.0)
            diff = funding_usd - old_funding
            if abs(diff) < 1e-9:
                return
            c.execute(
                "UPDATE positions SET funding_usd = ? WHERE id = ?",
                (funding_usd, position_id),
            )
            day = (row["closed_at"] or "")[:10] or date.today().isoformat()
            c.execute(
                """
                UPDATE daily_pnl
                SET realized_pnl_usd = realized_pnl_usd + ?
                WHERE day = ?
                """,
                (diff, day),
            )

    def get_positions_missing_funding(
        self, *, hours: int = 96
    ) -> list[AiPosition]:
        """v0.21: закрытые позиции с funding_usd IS NULL за последние ``hours``.

        Используется ``_reconcile_funding()``. По умолчанию 96ч —
        funding settlements могут отставать на 1–2 минуты от close, плюс
        запас на случай если бот был остановлен и не успел синкнуть
        вовремя. Bybit transaction-log хранит данные до 2 лет.
        """
        cutoff = datetime.now(tz=UTC).timestamp() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM positions
                WHERE closed_at IS NOT NULL AND funding_usd IS NULL
                ORDER BY closed_at DESC
                """,
            ).fetchall()
        out: list[AiPosition] = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["closed_at"]).timestamp()
                if ts >= cutoff:
                    out.append(AiPosition(**dict(r)))
            except (ValueError, TypeError):
                continue
        return out

    def get_recent_closed_gross_positions(
        self, *, hours: int = 24
    ) -> list[AiPosition]:
        """v0.18: позиции закрытые < ``hours`` назад с pnl_source != 'net'.

        Используется ``_reconcile_pnl_to_net()`` для догонной синхронизации
        ``realized_pnl_usd`` с биржевым net-значением, если в момент close
        ``get_closed_pnl`` API был недоступен.
        """
        cutoff = datetime.now(tz=UTC).timestamp() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM positions
                WHERE closed_at IS NOT NULL
                  AND (pnl_source IS NULL OR pnl_source != 'net')
                ORDER BY closed_at DESC
                """,
            ).fetchall()
        out: list[AiPosition] = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["closed_at"]).timestamp()
                if ts >= cutoff:
                    out.append(AiPosition(**dict(r)))
            except (ValueError, TypeError):
                continue
        return out

    def get_open_positions(self) -> list[AiPosition]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at"
            ).fetchall()
        return [AiPosition(**dict(r)) for r in rows]

    def get_position_by_link_id(self, order_link_id: str) -> AiPosition | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE order_link_id = ?",
                (order_link_id,),
            ).fetchone()
        return AiPosition(**dict(row)) if row else None

    # ─── PnL для killswitch ──────────────────────────────────────────────

    def get_today_pnl(self) -> float:
        today = date.today().isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM daily_pnl WHERE day = ?", (today,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def get_total_pnl(self) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) FROM daily_pnl"
            ).fetchone()
        return float(row[0]) if row else 0.0

    def get_equity_high_water_mark(self, initial_capital_usd: float) -> float:
        """v0.32 (2026-05-28): peak equity для drawdown tracking.

        Считает running cumulative sum по дням (ORDER BY day) поверх
        `initial_capital_usd`, возвращает максимум достигнутого equity.
        Если PnL пуст — peak = initial. Используется в context.py для
        EQUITY AWARENESS блока (drawdown_from_peak_pct помогает LLM
        видеть «бывал ли в плюсе и насколько откатилось»).

        Аппроксимация: peak считается по концу дня, не intra-day. Для
        нашего periodicity (15min full + 5min review) достаточно: бот
        видит daily resolution drawdown, не minute-by-minute.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT realized_pnl_usd FROM daily_pnl ORDER BY day"
            ).fetchall()
        peak = initial_capital_usd
        running = initial_capital_usd
        for r in rows:
            running += float(r[0] or 0.0)
            if running > peak:
                peak = running
        return peak

    def add_api_cost(self, cost_usd: float) -> None:
        today = date.today().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO daily_pnl (day, api_cost_usd)
                VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    api_cost_usd = api_cost_usd + excluded.api_cost_usd
                """,
                (today, cost_usd),
            )

    # ─── KV state (chat_id, paused, etc) ──────────────────────────────────

    def kv_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM kv_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO kv_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, datetime.now(tz=UTC).isoformat()),
            )

    def is_paused(self) -> bool:
        return self.kv_get("paused") == "1"

    def set_paused(self, value: bool) -> None:
        self.kv_set("paused", "1" if value else "0")

    def get_telegram_chat_id(self) -> int | None:
        v = self.kv_get("telegram_chat_id")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    def set_telegram_chat_id(self, chat_id: int) -> None:
        self.kv_set("telegram_chat_id", str(chat_id))

    def get_recent_decisions(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT cycle, ts, parsed_action, executed, error
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions_count(self) -> tuple[int, int]:
        """Возвращает (всего закрытых, прибыльных)."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins
                FROM positions WHERE closed_at IS NOT NULL
                """
            ).fetchone()
        return (int(row[0]) if row else 0, int(row[1]) if row else 0)

    # ─── v0.30 SELF-REFLECTION / COLD-START / REGIME-CHANGE ──────────────
    # Port из fx_ai_trader/state/db.py (BUILDLOG_AI_FX_TRADER.md 2026-05-26
    # «self-reflection» + 2026-05-28 «cold-start» + 2026-05-28
    # «regime-change cutoff»). Адаптация для Bybit:
    # - таблица `positions` без `is_paper` (ai-trader всегда demo, нет
    #   paper-mode) и без `volume_lots` (qty вместо).
    # - sides "Buy"/"Sell" с capital letter (вместо FX "BUY"/"SELL").
    # - cumulative pnl preference: net (pnl_source='net' — после v0.21
    #   reconcile) чтобы self-reflection видел реальные числа после fee.

    def get_pnl_by_symbol(
        self,
        symbols: list[str],
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-symbol агрегаты по закрытым позициям (live demo).

        Возвращает по одной записи на каждый символ из ``symbols`` (порядок
        сохраняется). Если по символу нет закрытых трейдов в окне — запись
        с ``n=0`` (явный сигнал «не торговали», не пропуск).

        Поля: ``symbol``, ``n``, ``wins``, ``win_rate_pct``,
        ``avg_pnl_usd``, ``sum_pnl_usd``.

        ``since`` — ISO 8601 cutoff на ``opened_at`` (включительно: ≥).
        ``None`` или пустая строка → без фильтра, вся история. Применяется
        для regime-change cutoff: pre-v0.30 trades — outcome другой
        стратегии (без THESIS DISCIPLINE / per-asset hierarchy), включение
        в SELF-REFLECTION = curve-fitting. См.
        ``AiTraderSettings.stats_window_start`` и
        BUILDLOG_AI_TRADER.md v0.30.

        В отличие от FX-trader (`is_paper=0` фильтр) — в ai-trader нет
        paper-mode, поэтому все закрытые позиции считаются live demo.
        """
        with self._conn() as c:
            placeholders = ",".join("?" * len(symbols)) if symbols else "''"
            params: list[Any] = list(symbols)
            since_clause = ""
            if since:
                since_clause = " AND opened_at >= ?"
                params.append(since)
            rows = c.execute(
                f"""
                SELECT symbol,
                       COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(AVG(realized_pnl_usd), 0.0) AS avg_pnl,
                       COALESCE(SUM(realized_pnl_usd), 0.0) AS sum_pnl
                FROM positions
                WHERE closed_at IS NOT NULL
                  AND symbol IN ({placeholders}){since_clause}
                GROUP BY symbol
                """,
                params,
            ).fetchall()
        by_symbol = {r["symbol"]: r for r in rows}
        out: list[dict[str, Any]] = []
        for sym in symbols:
            r = by_symbol.get(sym)
            if r is None:
                out.append(
                    {
                        "symbol": sym,
                        "n": 0,
                        "wins": 0,
                        "win_rate_pct": 0.0,
                        "avg_pnl_usd": 0.0,
                        "sum_pnl_usd": 0.0,
                    }
                )
                continue
            n = int(r["n"])
            wins = int(r["wins"])
            wr = (wins / n * 100.0) if n > 0 else 0.0
            out.append(
                {
                    "symbol": sym,
                    "n": n,
                    "wins": wins,
                    "win_rate_pct": wr,
                    "avg_pnl_usd": float(r["avg_pnl"] or 0.0),
                    "sum_pnl_usd": float(r["sum_pnl"] or 0.0),
                }
            )
        return out

    def get_pnl_by_symbol_side(
        self,
        symbols: list[str],
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-(symbol × side) агрегаты по закрытым позициям.

        Возвращает по 2 записи на каждый символ из ``symbols`` (Buy+Sell,
        порядок ``symbols`` сохраняется; внутри symbol — Buy первой). Если
        по (symbol × side) нет закрытых трейдов в окне — запись с ``n=0``
        (явный cold-start сигнал, NOT пропуск).

        Поля: ``symbol``, ``side``, ``n``, ``wins``, ``win_rate_pct``,
        ``avg_pnl_usd``, ``sum_pnl_usd``.

        Зачем: основной ``get_pnl_by_symbol`` агрегирует через side, что
        скрывает критическую информацию вида «BTCUSDT Sell 3/3 wins, но
        BTCUSDT Buy 0 trades в истории». Self-reflection в SYSTEM_PROMPT
        без этой разбивки получает confounded WR и систематически
        избегает untested направления (cold-start bias).

        Research basis: Sutton & Barto (2018) §2.7 «Optimistic Initial
        Values» — explicit treatment of (action × state) pairs with n=0
        is required to avoid cold-start trap. См. SYSTEM_PROMPT раздел
        COLD-START DISCOVERY RULE и BUILDLOG_AI_TRADER.md v0.30.

        ``since`` — same semantics как get_pnl_by_symbol.
        """
        with self._conn() as c:
            placeholders = ",".join("?" * len(symbols)) if symbols else "''"
            params: list[Any] = list(symbols)
            since_clause = ""
            if since:
                since_clause = " AND opened_at >= ?"
                params.append(since)
            rows = c.execute(
                f"""
                SELECT symbol,
                       side,
                       COUNT(*) AS n,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(AVG(realized_pnl_usd), 0.0) AS avg_pnl,
                       COALESCE(SUM(realized_pnl_usd), 0.0) AS sum_pnl
                FROM positions
                WHERE closed_at IS NOT NULL
                  AND symbol IN ({placeholders}){since_clause}
                GROUP BY symbol, side
                """,
                params,
            ).fetchall()
        by_key = {(r["symbol"], r["side"]): r for r in rows}
        out: list[dict[str, Any]] = []
        for sym in symbols:
            for side in ("Buy", "Sell"):
                r = by_key.get((sym, side))
                if r is None:
                    out.append(
                        {
                            "symbol": sym,
                            "side": side,
                            "n": 0,
                            "wins": 0,
                            "win_rate_pct": 0.0,
                            "avg_pnl_usd": 0.0,
                            "sum_pnl_usd": 0.0,
                        }
                    )
                    continue
                n = int(r["n"])
                wins = int(r["wins"])
                wr = (wins / n * 100.0) if n > 0 else 0.0
                out.append(
                    {
                        "symbol": sym,
                        "side": side,
                        "n": n,
                        "wins": wins,
                        "win_rate_pct": wr,
                        "avg_pnl_usd": float(r["avg_pnl"] or 0.0),
                        "sum_pnl_usd": float(r["sum_pnl"] or 0.0),
                    }
                )
        return out

    def get_recent_closed_trades(
        self,
        limit: int = 10,
        reason_clamp: int = 180,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Последние ``limit`` закрытых трейдов, отсортированы по
        ``closed_at`` ASC (oldest → newest для USER_PROMPT readability).

        Поля: ``id``, ``symbol``, ``side``, ``qty``, ``entry_price``,
        ``exit_price``, ``realized_pnl_usd``, ``opened_at``, ``closed_at``,
        ``duration_minutes``, ``llm_reason`` (clamp), ``close_reason``
        (clamp), ``macro_thesis`` (clamp, v0.30 audit-trail).

        ``reason_clamp`` — лимит на символ для llm_reason / close_reason /
        macro_thesis (default 180). LLM в SELF-REFLECTION сможет
        cross-check свой past reasoning с outcome.

        ``since`` — same semantics как get_pnl_by_symbol.
        """
        with self._conn() as c:
            params: list[Any] = []
            since_clause = ""
            if since:
                since_clause = " AND opened_at >= ?"
                params.append(since)
            params.append(int(limit))
            rows = c.execute(
                f"""
                SELECT id, symbol, side, qty, entry_price, exit_price,
                       realized_pnl_usd, opened_at, closed_at, llm_reason,
                       close_reason, macro_thesis
                FROM positions
                WHERE closed_at IS NOT NULL{since_clause}
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            duration_min: int | None = None
            try:
                t_open = datetime.fromisoformat(r["opened_at"])
                t_close = datetime.fromisoformat(r["closed_at"])
                duration_min = max(0, int((t_close - t_open).total_seconds() // 60))
            except (TypeError, ValueError):
                duration_min = None
            llm_reason = (r["llm_reason"] or "")[:reason_clamp]
            close_reason = (r["close_reason"] or "")[:reason_clamp]
            macro_thesis = (r["macro_thesis"] or "")[:reason_clamp]
            out.append(
                {
                    "id": int(r["id"]),
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "qty": float(r["qty"]),
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
                    "realized_pnl_usd": float(r["realized_pnl_usd"]) if r["realized_pnl_usd"] is not None else 0.0,
                    "opened_at": r["opened_at"],
                    "closed_at": r["closed_at"],
                    "duration_minutes": duration_min,
                    "llm_reason": llm_reason,
                    "close_reason": close_reason,
                    "macro_thesis": macro_thesis,
                }
            )
        out.reverse()  # oldest → newest для prompt readability
        return out

    def update_decision_thesis(
        self,
        decision_id: int,
        *,
        thesis_status: str | None,
        thesis_invalidator: str | None,
    ) -> None:
        """v0.30: записать thesis_status + thesis_invalidator в decisions.

        Вызывается из executor._apply_close после успешного close (если
        action содержал thesis-поля). Если decision_id невалидный или
        запись не существует — silent no-op (commit-time write,
        audit-trail).
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE decisions
                SET thesis_status = ?, thesis_invalidator = ?
                WHERE id = ?
                """,
                (thesis_status, thesis_invalidator, decision_id),
            )

    def update_decision_sentiment(
        self,
        decision_id: int,
        *,
        aggregate_uncertainty: float | None,
        sentiment_items_json: str | None,
        macro_rates_snapshot: str | None = None,
    ) -> None:
        """v0.30: записать 5-dim news sentiment + macro snapshot в decisions.

        Audit-trail для последующего анализа: чем выше corr между
        ``aggregate_uncertainty`` и ``error="open blocked: ..."`` —
        тем валиднее gate. ``macro_rates_snapshot`` опционально для
        случаев когда LLM решение зависит от DXY/UST10Y.
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE decisions
                SET aggregate_uncertainty = ?,
                    sentiment_items_json = ?,
                    macro_rates_snapshot = ?
                WHERE id = ?
                """,
                (
                    aggregate_uncertainty,
                    sentiment_items_json,
                    macro_rates_snapshot,
                    decision_id,
                ),
            )
