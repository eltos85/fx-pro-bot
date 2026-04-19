#!/usr/bin/env python3
"""AB-test snapshot для bybit_bot: инкрементальный sync closedPnl → markdown отчёт.

Принципы:
- Источник правды — Bybit closed-pnl API (`closedPnl` = NET, с комиссией).
  Правило bybit-pnl.mdc: не вычитать комиссию повторно.
- БД `ab_snapshots.sqlite` хранится вне docker volume, в bind-mount'е
  /root/fx-pro-bot-data (путь задан переменной AB_SNAPSHOTS_DB_PATH).
- Маппинг стратегии — fuzzy match с bybit_stats.sqlite.positions.
  Прямой JOIN по order_id невозможен: в closed-pnl `orderId` — это id
  закрывающего reduceOnly ордера, а `positions.order_id` у бота — id
  открывающего ордера (разные сущности). Fuzzy-ключ: symbol + инверсия
  side + entry_price ±0.1% + qty ±5% + opened_at ∈ [updated_ms − 24h, updated_ms).
- hold_minutes считаем как (updated_time_ms − opened_at_ms)/60000, где
  opened_at_ms берётся из positions.opened_at ПОСЛЕ fuzzy-матча (поле
  createdTime из closed-pnl — это время создания закрывающего ордера,
  а не время открытия позиции, поэтому использовать его для hold нельзя).
- Инкрементальный sync: тянем только новое (по last_fetched_end_ms).
- Bybit v5 get_closed_pnl: окно между startTime и endTime ≤ 7 дней,
  пагинация по cursor, limit=100.

Запуск (VPS, внутри контейнера):
    docker exec fx-pro-bot-bybit-bot-1 python3 -m scripts.ab_test_snapshot \\
        [--since 2026-04-16] [--until 2026-04-19] [--wave 3] \\
        [--no-sync] [--output /ab-data/report.md] \\
        [--add-wave "name=baseline;start=2026-04-11T13:00;desc=..."]
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("ab_test_snapshot")

# Старт bybit-бота (первая сделка ~14:22 UTC 2026-04-11).
DEFAULT_EPOCH_MS = int(datetime(2026, 4, 11, 13, 0, tzinfo=UTC).timestamp() * 1000)
WINDOW_MS = 7 * 24 * 3600 * 1000  # Bybit ограничение окна closed-pnl
SYNC_RECENT_OVERLAP_MS = 60 * 60 * 1000  # при sync пересматриваем последний час — страховка от лага API


# ============================================================================
# БД
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS closed_trades (
    order_id          TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               REAL,
    avg_entry_price   REAL,
    avg_exit_price    REAL,
    closed_pnl        REAL,
    exec_type         TEXT,
    created_time_ms   INTEGER,
    updated_time_ms   INTEGER NOT NULL,
    opened_at_ms      INTEGER,
    leverage          REAL,
    order_link_id     TEXT,
    strategy          TEXT,
    hold_minutes      REAL,
    raw_json          TEXT,
    fetched_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ct_updated ON closed_trades(updated_time_ms);
CREATE INDEX IF NOT EXISTS idx_ct_symbol ON closed_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_ct_strategy ON closed_trades(strategy);

CREATE TABLE IF NOT EXISTS waves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT,
    commit_hash   TEXT,
    description   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_waves_start ON waves(start_utc);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _migrate_closed_trades(conn: sqlite3.Connection) -> None:
    """Миграция существующей БД: добавить колонки, которые появились позже."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(closed_trades)").fetchall()}
    if "opened_at_ms" not in cols:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN opened_at_ms INTEGER")
        log.info("migration: closed_trades.opened_at_ms added")
    conn.commit()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_closed_trades(conn)
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


# ============================================================================
# Sync из Bybit
# ============================================================================


@dataclass(frozen=True, slots=True)
class _TradeRow:
    order_id: str
    symbol: str
    side: str
    qty: float
    avg_entry_price: float
    avg_exit_price: float
    closed_pnl: float
    exec_type: str
    created_time_ms: int
    updated_time_ms: int
    leverage: float
    order_link_id: str
    raw_json: str


def _parse_trade(item: dict) -> _TradeRow | None:
    try:
        order_id = str(item["orderId"])
        updated = int(item.get("updatedTime") or item.get("createdTime") or 0)
        created = int(item.get("createdTime") or updated)
        return _TradeRow(
            order_id=order_id,
            symbol=str(item.get("symbol", "")),
            side=str(item.get("side", "")),
            qty=float(item.get("qty", 0) or 0),
            avg_entry_price=float(item.get("avgEntryPrice", 0) or 0),
            avg_exit_price=float(item.get("avgExitPrice", 0) or 0),
            closed_pnl=float(item.get("closedPnl", 0) or 0),
            exec_type=str(item.get("execType", "")),
            created_time_ms=created,
            updated_time_ms=updated,
            leverage=float(item.get("leverage", 0) or 0),
            order_link_id=str(item.get("orderLinkId", "") or ""),
            raw_json=_safe_json(item),
        )
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("Не удалось распарсить closedPnl запись: %s | raw=%s", exc, item)
        return None


def _safe_json(item: dict) -> str:
    import json
    try:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def _fetch_window(session, category: str, start_ms: int, end_ms: int) -> list[dict]:
    """Все записи closed-pnl в окне [start_ms, end_ms] c пагинацией по cursor."""
    out: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        params = {
            "category": category,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            resp = session.get_closed_pnl(**params)
        except Exception as exc:
            log.error("Bybit get_closed_pnl error (start=%s end=%s): %s", start_ms, end_ms, exc)
            break
        result = resp.get("result", {}) or {}
        items = result.get("list", []) or []
        out.extend(items)
        next_cursor = str(result.get("nextPageCursor", "") or "")
        pages += 1
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if pages > 100:
            log.warning("cursor loop break (>100 pages) start=%s end=%s", start_ms, end_ms)
            break
    return out


def sync_closed_pnl(conn: sqlite3.Connection, *, session, category: str, stats_db_path: Path | None) -> int:
    """Подтянуть новые сделки из Bybit, обогатить strategy JOIN'ом с stats_db.

    Возвращает количество добавленных (новых) сделок.
    """
    last_ms_raw = meta_get(conn, "last_fetched_end_ms")
    last_ms = int(last_ms_raw) if last_ms_raw else DEFAULT_EPOCH_MS
    start_ms = max(DEFAULT_EPOCH_MS, last_ms - SYNC_RECENT_OVERLAP_MS)
    now_ms = int(time.time() * 1000)

    added = 0
    seen = 0
    max_updated = last_ms

    cur_start = start_ms
    while cur_start < now_ms:
        cur_end = min(cur_start + WINDOW_MS, now_ms)
        log.info("sync: окно %s → %s", _fmt_ms(cur_start), _fmt_ms(cur_end))
        items = _fetch_window(session, category, cur_start, cur_end)
        for raw in items:
            trade = _parse_trade(raw)
            if trade is None:
                continue
            seen += 1
            if trade.updated_time_ms > max_updated:
                max_updated = trade.updated_time_ms
            # hold_minutes, opened_at_ms проставятся в enrich_strategy после
            # fuzzy-матча с positions (из closed-pnl их взять нельзя, см. docstring).
            result = conn.execute(
                "INSERT OR IGNORE INTO closed_trades "
                "(order_id, symbol, side, qty, avg_entry_price, avg_exit_price, "
                " closed_pnl, exec_type, created_time_ms, updated_time_ms, "
                " opened_at_ms, leverage, order_link_id, strategy, hold_minutes, "
                " raw_json, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade.order_id, trade.symbol, trade.side, trade.qty,
                    trade.avg_entry_price, trade.avg_exit_price, trade.closed_pnl,
                    trade.exec_type, trade.created_time_ms, trade.updated_time_ms,
                    None, trade.leverage, trade.order_link_id, None, None,
                    trade.raw_json, datetime.now(tz=UTC).isoformat(timespec="seconds"),
                ),
            )
            if result.rowcount > 0:
                added += 1
        conn.commit()
        cur_start = cur_end

    if max_updated > last_ms:
        meta_set(conn, "last_fetched_end_ms", str(max_updated))
    meta_set(conn, "last_sync_at", datetime.now(tz=UTC).isoformat(timespec="seconds"))

    enrich_strategy(conn, stats_db_path)

    log.info("sync: seen=%d, added=%d, last_updated=%s", seen, added, _fmt_ms(max_updated))
    return added


ENTRY_PRICE_REL_TOL = 0.005  # ±0.5% — допуск на расхождение entry price между Bybit и БД бота.
# Bybit closedPnl возвращает avgEntryPrice с округлением до tickSize, а
# positions.entry_price хранит float32 конверсию в REAL — расхождение до
# 0.2% для дешёвых токенов. 0.5% — компромисс: больше точности чем 0.2%,
# но всё ещё защищает от ложного матча с другой позицией того же символа.
QTY_REL_TOL = 0.05           # ±5% — округление qty до qty_step
# Позиции могут удерживаться неделями (напр. stat-arb до сходимости Z-score),
# поэтому окно — 14 дней. Жёсткие фильтры по entry_price ±0.1% и qty ±5%
# страхуют от неверных матчей с другой позицией того же символа.
MATCH_WINDOW_MS = 14 * 24 * 3600 * 1000


def enrich_strategy(conn: sqlite3.Connection, stats_db_path: Path | None) -> int:
    """Fuzzy-match closed_trades ↔ positions для проставления strategy и opened_at_ms.

    Причина fuzzy-матча: в `/v5/position/closed-pnl` поле `orderId` — это id
    закрывающего reduceOnly ордера (не совпадает с `positions.order_id`, где
    лежит id открывающего ордера). Прямой JOIN не работает.

    Ключ: symbol + инверсия side (closed.side='Sell' → open.side='Buy') +
    entry_price ±0.1% + qty ±5% + opened_at ∈ [updated_ms − 24h, updated_ms).
    При нескольких кандидатах — ближайшая по |opened_ms − updated_ms|.

    После матча пересчитывается hold_minutes = (updated_ms − opened_ms)/60000.

    Обрабатывает только записи с strategy IS NULL или 'unknown', чтобы не
    перетирать уже сматченное (идемпотентно при повторных запусках).

    Возвращает: число новых записей, которые получили strategy из positions.
    """
    if stats_db_path is None or not stats_db_path.exists():
        log.info("enrich_strategy: %s не найден, маппинг пропущен", stats_db_path)
        conn.execute(
            "UPDATE closed_trades SET strategy = 'unknown' WHERE strategy IS NULL"
        )
        conn.commit()
        return 0

    conn.execute("ATTACH DATABASE ? AS bot", (str(stats_db_path),))
    try:
        # Кандидаты на обогащение: либо strategy не проставлена, либо
        # opened_at_ms пуст (после миграции старой БД). Во втором случае
        # хотим получить opened_at_ms, чтобы правильно посчитать hold.
        pending = conn.execute(
            "SELECT order_id, symbol, side, qty, avg_entry_price, updated_time_ms, strategy "
            "FROM closed_trades "
            "WHERE strategy IS NULL OR strategy='unknown' OR opened_at_ms IS NULL"
        ).fetchall()

        matched = 0
        for ct in pending:
            open_side = "Buy" if ct["side"] == "Sell" else "Sell"
            entry = float(ct["avg_entry_price"] or 0)
            qty = float(ct["qty"] or 0)
            if entry <= 0 or qty <= 0:
                continue
            upd_ms = int(ct["updated_time_ms"])
            lower_ms = upd_ms - MATCH_WINDOW_MS

            # julianday в SQLite корректно парсит ISO-строки с суффиксом.
            # opened_ms получаем в int: ((julianday − 2440587.5) * 86400) sec → *1000 ms.
            match = conn.execute(
                """
                SELECT p.strategy AS strategy,
                       CAST((julianday(p.opened_at) - 2440587.5) * 86400000 AS INTEGER) AS opened_ms
                FROM bot.positions p
                WHERE p.symbol = ?
                  AND p.side = ?
                  AND ABS(p.entry_price - ?) <= ? * ?
                  AND ABS(CAST(p.qty AS REAL) - ?) <= ? * ?
                  AND CAST((julianday(p.opened_at) - 2440587.5) * 86400000 AS INTEGER) < ?
                  AND CAST((julianday(p.opened_at) - 2440587.5) * 86400000 AS INTEGER) >= ?
                ORDER BY ABS(
                    CAST((julianday(p.opened_at) - 2440587.5) * 86400000 AS INTEGER) - ?
                ) ASC
                LIMIT 1
                """,
                (
                    ct["symbol"], open_side,
                    entry, ENTRY_PRICE_REL_TOL, entry,
                    qty, QTY_REL_TOL, qty,
                    upd_ms, lower_ms, upd_ms,
                ),
            ).fetchone()

            if match is not None and match["strategy"]:
                # Если strategy уже стояла (например recovered) — оставляем её,
                # только дозаполняем opened_at_ms.
                keep_existing = ct["strategy"] not in (None, "", "unknown")
                new_strategy = ct["strategy"] if keep_existing else match["strategy"]
                conn.execute(
                    "UPDATE closed_trades "
                    "SET strategy = ?, opened_at_ms = ? "
                    "WHERE order_id = ?",
                    (new_strategy, match["opened_ms"], ct["order_id"]),
                )
                if not keep_existing:
                    matched += 1

        # Для записей, которым не нашли пару — явно 'unknown'.
        conn.execute(
            "UPDATE closed_trades SET strategy = 'unknown' WHERE strategy IS NULL"
        )

        # Пересчёт hold_minutes только там, где есть opened_at_ms.
        conn.execute(
            "UPDATE closed_trades "
            "SET hold_minutes = CAST((updated_time_ms - opened_at_ms) AS REAL) / 60000.0 "
            "WHERE opened_at_ms IS NOT NULL AND opened_at_ms > 0"
        )
        # Зануляем hold для unmatched записей (в старых снимках могли
        # остаться значения из кривой формулы updated − created).
        conn.execute(
            "UPDATE closed_trades SET hold_minutes = NULL WHERE opened_at_ms IS NULL"
        )
        conn.commit()

        unknown_after = conn.execute(
            "SELECT COUNT(*) AS n FROM closed_trades WHERE strategy='unknown'"
        ).fetchone()["n"]
        log.info(
            "enrich_strategy: pending=%d, matched=%d, unknown_after=%d",
            len(pending), matched, unknown_after,
        )
        return matched
    finally:
        conn.execute("DETACH DATABASE bot")


# ============================================================================
# Волны
# ============================================================================


def parse_add_wave_spec(spec: str) -> dict[str, str]:
    """Парсит строку вида 'name=baseline;start=2026-04-11T13:00;desc=...'."""
    out: dict[str, str] = {}
    for part in spec.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def add_wave(conn: sqlite3.Connection, spec: dict[str, str]) -> int:
    required = ("name", "start")
    missing = [k for k in required if not spec.get(k)]
    if missing:
        raise ValueError(f"add-wave: не хватает полей {missing}")
    name = spec["name"]
    start_utc = spec["start"]
    end_utc = spec.get("end") or None
    commit_hash = spec.get("commit") or None
    description = spec.get("desc") or None
    row = conn.execute("SELECT id FROM waves WHERE name=?", (name,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE waves SET start_utc=?, end_utc=?, commit_hash=?, description=? WHERE id=?",
            (start_utc, end_utc, commit_hash, description, row["id"]),
        )
        conn.commit()
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO waves(name, start_utc, end_utc, commit_hash, description) "
        "VALUES (?,?,?,?,?)",
        (name, start_utc, end_utc, commit_hash, description),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_waves(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM waves ORDER BY start_utc"
        ).fetchall()
    )


# ============================================================================
# Репорт
# ============================================================================


@dataclass(frozen=True, slots=True)
class ReportFilter:
    since_ms: int | None
    until_ms: int | None
    wave_id: int | None


def _resolve_filter(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
    wave_id: int | None,
) -> ReportFilter:
    since_ms: int | None = _parse_date_ms(since)
    until_ms: int | None = _parse_date_ms(until, end_of_day=True)
    if wave_id is not None:
        row = conn.execute("SELECT start_utc, end_utc FROM waves WHERE id=?", (wave_id,)).fetchone()
        if row is None:
            raise ValueError(f"wave id={wave_id} не найдена")
        since_ms = _parse_date_ms(row["start_utc"]) or since_ms
        end_val = row["end_utc"]
        if end_val:
            until_ms = _parse_date_ms(end_val, end_of_day=True) or until_ms
    return ReportFilter(since_ms=since_ms, until_ms=until_ms, wave_id=wave_id)


def _where_clause(flt: ReportFilter) -> tuple[str, list]:
    conds: list[str] = []
    params: list = []
    if flt.since_ms is not None:
        conds.append("updated_time_ms >= ?")
        params.append(flt.since_ms)
    if flt.until_ms is not None:
        conds.append("updated_time_ms <= ?")
        params.append(flt.until_ms)
    sql = " WHERE " + " AND ".join(conds) if conds else ""
    return sql, params


def _metrics_row(conn: sqlite3.Connection, where: str, params: list) -> dict:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*)                                  AS trades,
            COALESCE(SUM(closed_pnl), 0)              AS pnl,
            COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN 1 ELSE 0 END), 0)  AS wins,
            COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN closed_pnl ELSE 0 END), 0) AS gross_profit,
            COALESCE(SUM(CASE WHEN closed_pnl < 0 THEN closed_pnl ELSE 0 END), 0) AS gross_loss,
            COALESCE(AVG(closed_pnl), 0)              AS avg_pnl,
            COALESCE(AVG(CASE WHEN closed_pnl > 0 THEN closed_pnl END), 0) AS avg_win,
            COALESCE(AVG(CASE WHEN closed_pnl < 0 THEN closed_pnl END), 0) AS avg_loss,
            AVG(hold_minutes)                         AS avg_hold,
            MAX(hold_minutes)                         AS max_hold,
            SUM(CASE WHEN hold_minutes IS NOT NULL THEN 1 ELSE 0 END) AS hold_known,
            MIN(updated_time_ms)                      AS first_ms,
            MAX(updated_time_ms)                      AS last_ms
        FROM closed_trades
        {where}
        """,
        params,
    ).fetchone()
    return dict(row) if row else {}


def _format_metrics_rows(m: dict, trades: int) -> list[str]:
    wr = (m["wins"] / trades * 100) if trades else 0
    gross_loss = m.get("gross_loss") or 0
    gross_profit = m.get("gross_profit") or 0
    pf = (gross_profit / -gross_loss) if gross_loss < 0 else float("inf")
    pf_str = "∞" if pf == float("inf") else f"{pf:.3f}"
    avg_hold = m.get("avg_hold")
    max_hold = m.get("max_hold")
    hold_known = m.get("hold_known") or 0
    hold_suffix = f" (по {hold_known}/{trades})" if hold_known < trades else ""
    avg_hold_str = f"{avg_hold:.1f}" if avg_hold is not None else "—"
    max_hold_str = f"{max_hold:.1f}" if max_hold is not None else "—"
    return [
        f"| Сделок | {trades} |",
        f"| PnL total (NET) | `{_money(m['pnl'])}` |",
        f"| Win Rate | `{wr:.1f}%` ({m['wins']}/{trades}) |",
        f"| Avg PnL / trade | `{_money(m['avg_pnl'], prec=3)}` |",
        f"| Avg Win | `{_money(m['avg_win'], prec=3)}` |",
        f"| Avg Loss | `{_money(m['avg_loss'], prec=3)}` |",
        f"| Gross Profit | `{_money(m['gross_profit'])}` |",
        f"| Gross Loss | `{_money(m['gross_loss'])}` |",
        f"| Profit Factor | `{pf_str}` |",
        f"| Avg Hold (min){hold_suffix} | `{avg_hold_str}` |",
        f"| Max Hold (min) | `{max_hold_str}` |",
    ]


def _render_overall(conn: sqlite3.Connection, flt: ReportFilter) -> str:
    where, params = _where_clause(flt)
    m = _metrics_row(conn, where, params)
    trades = m.get("trades") or 0
    if trades == 0:
        return "## Overall\n\n_Нет сделок в указанном окне._\n"
    first = _fmt_ms(m["first_ms"]) if m.get("first_ms") else "—"
    last = _fmt_ms(m["last_ms"]) if m.get("last_ms") else "—"
    lines = [
        "## Overall",
        "",
        f"Окно: **{first} → {last}**",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        *_format_metrics_rows(m, trades),
        "",
    ]
    # Срез без 'recovered' — чистая бот-логика (позиции, открытые самим ботом).
    where_excl = where + (" AND " if where else " WHERE ") + "COALESCE(strategy,'') <> 'recovered'"
    m_excl = _metrics_row(conn, where_excl, params)
    trades_excl = m_excl.get("trades") or 0
    recovered_cnt = trades - trades_excl
    if recovered_cnt > 0 and trades_excl > 0:
        lines.extend([
            f"### Overall (excl. `recovered`) — {recovered_cnt} подхваченных позиций исключено",
            "",
            "| Метрика | Значение |",
            "|---|---|",
            *_format_metrics_rows(m_excl, trades_excl),
            "",
        ])
    return "\n".join(lines)


def _render_by_wave(conn: sqlite3.Connection) -> str:
    waves = list_waves(conn)
    if not waves:
        return "## По волнам\n\n_Волны не заданы. Добавь через --add-wave._\n"
    out = [
        "## По волнам",
        "",
        "| # | Name | Период | Сделок | PnL | WR | PF | Avg/trade | Avg Hold (m) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for w in waves:
        start_ms = _parse_date_ms(w["start_utc"]) or 0
        end_ms = _parse_date_ms(w["end_utc"], end_of_day=True) if w["end_utc"] else None
        params: list = [start_ms]
        where = "WHERE updated_time_ms >= ?"
        if end_ms is not None:
            where += " AND updated_time_ms <= ?"
            params.append(end_ms)
        m = _metrics_row(conn, where, params)
        trades = m.get("trades") or 0
        if trades == 0:
            out.append(
                f"| {w['id']} | {w['name']} | {w['start_utc']} — {w['end_utc'] or 'now'} "
                f"| 0 | — | — | — | — | — |"
            )
            continue
        wr = m["wins"] / trades * 100
        pf = (m["gross_profit"] / -m["gross_loss"]) if m["gross_loss"] < 0 else float("inf")
        pf_str = "∞" if pf == float("inf") else f"{pf:.3f}"
        avg_hold_str = f"{m['avg_hold']:.1f}" if m.get("avg_hold") is not None else "—"
        out.append(
            f"| {w['id']} | {w['name']} | {w['start_utc']} — {w['end_utc'] or 'now'} "
            f"| {trades} | `{_money(m['pnl'])}` | {wr:.1f}% | {pf_str} "
            f"| `{_money(m['avg_pnl'], prec=3)}` | {avg_hold_str} |"
        )
    out.append("")
    return "\n".join(out)


def _render_group(
    conn: sqlite3.Connection,
    flt: ReportFilter,
    *,
    title: str,
    group_expr: str,
    order_by: str = "pnl ASC",
    header_label: str = "Ключ",
    extra_where: str = "",
) -> str:
    where, params = _where_clause(flt)
    if extra_where:
        where = where + (" AND " if where else " WHERE ") + extra_where
    rows = conn.execute(
        f"""
        SELECT {group_expr} AS grp,
               COUNT(*) AS trades,
               COALESCE(SUM(closed_pnl), 0) AS pnl,
               COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN closed_pnl ELSE 0 END), 0) AS gp,
               COALESCE(SUM(CASE WHEN closed_pnl < 0 THEN closed_pnl ELSE 0 END), 0) AS gl,
               COALESCE(AVG(closed_pnl), 0) AS avg_pnl,
               AVG(hold_minutes) AS avg_hold
        FROM closed_trades
        {where}
        GROUP BY grp
        ORDER BY {order_by}
        """,
        params,
    ).fetchall()
    if not rows:
        return f"## {title}\n\n_Нет данных._\n"
    out = [
        f"## {title}",
        "",
        f"| {header_label} | Сделок | PnL | WR | PF | Avg/trade | Avg Hold (m) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        t = r["trades"] or 0
        wr = (r["wins"] / t * 100) if t else 0
        gl = r["gl"] or 0
        pf = (r["gp"] / -gl) if gl < 0 else float("inf")
        pf_str = "∞" if pf == float("inf") else f"{pf:.3f}"
        grp_val = r["grp"] if r["grp"] not in (None, "") else "(none)"
        avg_hold_str = f"{r['avg_hold']:.1f}" if r["avg_hold"] is not None else "—"
        out.append(
            f"| {grp_val} | {t} | `{_money(r['pnl'])}` | {wr:.1f}% | {pf_str} "
            f"| `{_money(r['avg_pnl'], prec=3)}` | {avg_hold_str} |"
        )
    out.append("")
    return "\n".join(out)


def _render_by_hold(conn: sqlite3.Connection, flt: ReportFilter) -> str:
    """Buckets: 0-5m, 5-15m, 15-60m, 60m+."""
    bucket_expr = (
        "CASE "
        "WHEN hold_minutes < 5 THEN '0-5m' "
        "WHEN hold_minutes < 15 THEN '5-15m' "
        "WHEN hold_minutes < 60 THEN '15-60m' "
        "ELSE '60m+' END"
    )
    # Определяем собственный ORDER BY через FIELD-эмуляцию.
    order_expr = (
        "CASE grp "
        "WHEN '0-5m' THEN 1 "
        "WHEN '5-15m' THEN 2 "
        "WHEN '15-60m' THEN 3 "
        "WHEN '60m+' THEN 4 ELSE 5 END"
    )
    return _render_group(
        conn, flt, title="По длительности удержания",
        group_expr=bucket_expr, order_by=order_expr, header_label="Bucket",
        extra_where="hold_minutes IS NOT NULL",
    )


def render_report(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
    wave_id: int | None,
) -> str:
    flt = _resolve_filter(conn, since=since, until=until, wave_id=wave_id)
    now_utc = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    header = [
        f"# AB Test Snapshot — {now_utc}",
        "",
        "**Источник:** Bybit closed-pnl API (`closedPnl` = NET, с учётом комиссии).",
    ]
    if since or until or wave_id is not None:
        scope_bits: list[str] = []
        if since:
            scope_bits.append(f"since=`{since}`")
        if until:
            scope_bits.append(f"until=`{until}`")
        if wave_id is not None:
            scope_bits.append(f"wave={wave_id}")
        header.append("**Фильтр:** " + ", ".join(scope_bits))
    last_sync = meta_get(conn, "last_sync_at")
    if last_sync:
        header.append(f"**Последний sync:** `{last_sync}`")
    header.append("")
    parts = [
        "\n".join(header),
        _render_overall(conn, flt),
        _render_by_wave(conn),
        _render_group(conn, flt, title="По дням (UTC)",
                      group_expr="date(datetime(updated_time_ms/1000, 'unixepoch'))",
                      order_by="grp ASC", header_label="День"),
        _render_group(conn, flt, title="По символам", group_expr="symbol",
                      header_label="Символ"),
        _render_group(conn, flt, title="По стратегиям", group_expr="COALESCE(strategy,'unknown')",
                      header_label="Стратегия"),
        _render_group(conn, flt, title="По часам (UTC)",
                      group_expr="cast(strftime('%H', datetime(updated_time_ms/1000, 'unixepoch')) AS INTEGER)",
                      order_by="grp ASC", header_label="Час"),
        _render_by_hold(conn, flt),
    ]
    return "\n".join(parts).rstrip() + "\n"


# ============================================================================
# Утилиты
# ============================================================================


def _parse_date_ms(value: str | None, *, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    fmts = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    parsed: datetime | None = None
    for fmt in fmts:
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(f"unsupported date format: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end_of_day and len(value) <= 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return int(parsed.timestamp() * 1000)


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _money(value: float | None, *, prec: int = 2) -> str:
    v = float(value or 0)
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):.{prec}f}"


# ============================================================================
# CLI
# ============================================================================


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ab_test_snapshot",
        description="Sync Bybit closedPnl → SQLite → markdown отчёт",
    )
    p.add_argument("--since", help="Отфильтровать отчёт от даты (YYYY-MM-DD[THH:MM])")
    p.add_argument("--until", help="Отфильтровать отчёт до даты (YYYY-MM-DD[THH:MM])")
    p.add_argument("--wave", type=int, help="Ограничить отчёт волной с этим ID")
    p.add_argument("--no-sync", action="store_true", help="Не ходить в Bybit API, работать по БД")
    p.add_argument("--no-report", action="store_true", help="Только sync/манипуляции, без markdown")
    p.add_argument("--output", help="Путь для записи markdown (помимо stdout)")
    p.add_argument("--add-wave", dest="add_wave", help="Добавить/обновить волну: 'name=...;start=...;[end=...;commit=...;desc=...]'")
    p.add_argument("--list-waves", action="store_true", help="Показать список волн и выйти")
    p.add_argument("--db", help="Путь к ab_snapshots.sqlite (перекрывает $AB_SNAPSHOTS_DB_PATH)")
    p.add_argument("--stats-db", help="Путь к bybit_stats.sqlite для маппинга стратегий")
    p.add_argument("--log-level", default="INFO", help="INFO|DEBUG|WARNING")
    return p


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path | None]:
    db_str = args.db or os.environ.get("AB_SNAPSHOTS_DB_PATH") or "/ab-data/ab_snapshots.sqlite"
    db = Path(db_str)
    if args.stats_db:
        stats_db: Path | None = Path(args.stats_db)
    else:
        try:
            from bybit_bot.config.settings import Settings
            stats_db = Settings(_env_file=None).stats_db_path
        except Exception as exc:
            log.warning("Settings недоступны, stats_db пропущен: %s", exc)
            stats_db = None
    return db, stats_db


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_path, stats_db = _resolve_paths(args)
    conn = open_db(db_path)

    if args.add_wave:
        spec = parse_add_wave_spec(args.add_wave)
        wave_id = add_wave(conn, spec)
        log.info("wave upsert ok: id=%d name=%s", wave_id, spec.get("name"))

    if args.list_waves:
        waves = list_waves(conn)
        if not waves:
            print("(нет волн)")
        else:
            for w in waves:
                print(f"[{w['id']:>2}] {w['name']:<25} {w['start_utc']} → {w['end_utc'] or 'now'}"
                      f"  commit={w['commit_hash'] or '—'}  {w['description'] or ''}")
        return 0

    if not args.no_sync:
        try:
            from bybit_bot.config.settings import Settings
            from pybit.unified_trading import HTTP
        except Exception as exc:
            log.error("Импорт pybit/Settings упал, sync невозможен: %s", exc)
            return 2
        settings = Settings(_env_file=None)
        if not settings.api_key or not settings.api_secret:
            log.error("BYBIT_BOT_API_KEY/SECRET не заданы, sync невозможен")
            return 3
        session = HTTP(api_key=settings.api_key, api_secret=settings.api_secret, demo=settings.demo)
        sync_closed_pnl(conn, session=session, category=settings.category, stats_db_path=stats_db)
    else:
        enrich_strategy(conn, stats_db)

    if args.no_report:
        return 0

    md = render_report(conn, since=args.since, until=args.until, wave_id=args.wave)
    print(md)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        log.info("report saved → %s", out_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
