"""Периодическая очистка устаревших данных в SQLite.

Не модифицирует store.py — работает с БД напрямую.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

SHADOW_CLOSED_KEEP_DAYS = 7
SHADOW_MAX_KEEP_DAYS = 30


def cleanup_shadow_log(db_path: Path) -> int:
    """Удалить устаревшие записи shadow_log. Возвращает кол-во удалённых строк.

    - Для закрытых позиций: старше SHADOW_CLOSED_KEEP_DAYS дней
    - Для всех: старше SHADOW_MAX_KEEP_DAYS дней
    """
    conn = sqlite3.connect(db_path)
    try:
        deleted = 0

        cur = conn.execute(
            """
            DELETE FROM shadow_log
            WHERE position_id IN (
                SELECT id FROM positions WHERE status = 'closed'
            )
            AND ts < datetime('now', ?)
            """,
            (f"-{SHADOW_CLOSED_KEEP_DAYS} days",),
        )
        deleted += cur.rowcount

        cur = conn.execute(
            "DELETE FROM shadow_log WHERE ts < datetime('now', ?)",
            (f"-{SHADOW_MAX_KEEP_DAYS} days",),
        )
        deleted += cur.rowcount

        if deleted > 0:
            conn.execute("PRAGMA optimize")
            conn.commit()
            log.info("Cleanup: удалено %d записей shadow_log", deleted)

        return deleted
    finally:
        conn.close()


def db_size_mb(db_path: Path) -> float:
    """Размер файла БД в мегабайтах."""
    if db_path.exists():
        return db_path.stat().st_size / (1024 * 1024)
    return 0.0


def vacuum_if_needed(db_path: Path, threshold_mb: float = 100.0) -> bool:
    """VACUUM если размер БД превышает порог."""
    size = db_size_mb(db_path)
    if size < threshold_mb:
        return False

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    new_size = db_size_mb(db_path)
    log.info("VACUUM: %.1f MB → %.1f MB", size, new_size)
    return True
