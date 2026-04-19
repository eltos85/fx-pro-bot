#!/usr/bin/env python3
"""AB-test snapshot для bybit_bot: инкрементальный sync closedPnl → markdown отчёт.

Принципы:
- Источник правды — Bybit closed-pnl API (`closedPnl` = NET, с комиссией).
  Правило bybit-pnl.mdc: не вычитать комиссию повторно.
- БД `ab_snapshots.sqlite` хранится вне docker volume, в bind-mount'е
  /root/fx-pro-bot-data (путь задан переменной AB_SNAPSHOTS_DB_PATH).
- Маппинг стратегии — через JOIN с bybit_stats.sqlite.positions по order_id.
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


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
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
            hold_min = 0.0
            if trade.created_time_ms and trade.updated_time_ms > trade.created_time_ms:
                hold_min = (trade.updated_time_ms - trade.created_time_ms) / 60000.0
            result = conn.execute(
                "INSERT OR IGNORE INTO closed_trades "
                "(order_id, symbol, side, qty, avg_entry_price, avg_exit_price, "
                " closed_pnl, exec_type, created_time_ms, updated_time_ms, "
                " leverage, order_link_id, strategy, hold_minutes, raw_json, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trade.order_id, trade.symbol, trade.side, trade.qty,
                    trade.avg_entry_price, trade.avg_exit_price, trade.closed_pnl,
                    trade.exec_type, trade.created_time_ms, trade.updated_time_ms,
                    trade.leverage, trade.order_link_id, None, hold_min,
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


def enrich_strategy(conn: sqlite3.Connection, stats_db_path: Path | None) -> int:
    """Проставить closed_trades.strategy через JOIN с bybit_stats.sqlite.positions.

    Делает UPDATE только там, где strategy ещё NULL или 'unknown', чтобы не перетирать.
    Возвращает число обновлённых записей.
    """
    if stats_db_path is None or not stats_db_path.exists():
        log.info("enrich_strategy: %s не найден, маппинг пропущен", stats_db_path)
        return 0

    conn.execute("ATTACH DATABASE ? AS bot", (str(stats_db_path),))
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM closed_trades WHERE strategy IS NULL OR strategy='unknown'"
        ).fetchone()["n"]
        conn.execute(
            "UPDATE closed_trades "
            "SET strategy = COALESCE((SELECT p.strategy FROM bot.positions p "
            "                          WHERE p.order_id = closed_trades.order_id "
            "                          LIMIT 1), strategy) "
            "WHERE strategy IS NULL OR strategy='unknown'"
        )
        conn.execute(
            "UPDATE closed_trades SET strategy = 'unknown' WHERE strategy IS NULL"
        )
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM closed_trades WHERE strategy='unknown'"
        ).fetchone()["n"]
        log.info("enrich_strategy: before unknown/null=%d, after unknown=%d", before, after)
        return max(0, before - after)
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
            COALESCE(AVG(hold_minutes), 0)            AS avg_hold,
            COALESCE(MAX(hold_minutes), 0)            AS max_hold,
            MIN(updated_time_ms)                      AS first_ms,
            MAX(updated_time_ms)                      AS last_ms
        FROM closed_trades
        {where}
        """,
        params,
    ).fetchone()
    return dict(row) if row else {}


def _render_overall(conn: sqlite3.Connection, flt: ReportFilter) -> str:
    where, params = _where_clause(flt)
    m = _metrics_row(conn, where, params)
    trades = m.get("trades") or 0
    if trades == 0:
        return "## Overall\n\n_Нет сделок в указанном окне._\n"
    wr = (m["wins"] / trades * 100) if trades else 0
    pf = (m["gross_profit"] / -m["gross_loss"]) if m["gross_loss"] < 0 else float("inf")
    first = _fmt_ms(m["first_ms"]) if m.get("first_ms") else "—"
    last = _fmt_ms(m["last_ms"]) if m.get("last_ms") else "—"
    pf_str = "∞" if pf == float("inf") else f"{pf:.3f}"
    lines = [
        "## Overall",
        "",
        f"Окно: **{first} → {last}**",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Сделок | {trades} |",
        f"| PnL total (NET) | `{_money(m['pnl'])}` |",
        f"| Win Rate | `{wr:.1f}%` ({m['wins']}/{trades}) |",
        f"| Avg PnL / trade | `{_money(m['avg_pnl'], prec=3)}` |",
        f"| Avg Win | `{_money(m['avg_win'], prec=3)}` |",
        f"| Avg Loss | `{_money(m['avg_loss'], prec=3)}` |",
        f"| Gross Profit | `{_money(m['gross_profit'])}` |",
        f"| Gross Loss | `{_money(m['gross_loss'])}` |",
        f"| Profit Factor | `{pf_str}` |",
        f"| Avg Hold (min) | `{m['avg_hold']:.1f}` |",
        f"| Max Hold (min) | `{m['max_hold']:.1f}` |",
        "",
    ]
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
        out.append(
            f"| {w['id']} | {w['name']} | {w['start_utc']} — {w['end_utc'] or 'now'} "
            f"| {trades} | `{_money(m['pnl'])}` | {wr:.1f}% | {pf_str} "
            f"| `{_money(m['avg_pnl'], prec=3)}` | {m['avg_hold']:.1f} |"
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
) -> str:
    where, params = _where_clause(flt)
    rows = conn.execute(
        f"""
        SELECT {group_expr} AS grp,
               COUNT(*) AS trades,
               COALESCE(SUM(closed_pnl), 0) AS pnl,
               COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN closed_pnl > 0 THEN closed_pnl ELSE 0 END), 0) AS gp,
               COALESCE(SUM(CASE WHEN closed_pnl < 0 THEN closed_pnl ELSE 0 END), 0) AS gl,
               COALESCE(AVG(closed_pnl), 0) AS avg_pnl,
               COALESCE(AVG(hold_minutes), 0) AS avg_hold
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
        out.append(
            f"| {grp_val} | {t} | `{_money(r['pnl'])}` | {wr:.1f}% | {pf_str} "
            f"| `{_money(r['avg_pnl'], prec=3)}` | {r['avg_hold']:.1f} |"
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
