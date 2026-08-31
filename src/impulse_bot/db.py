"""SQLite impulse-bot. Чужие позиции на общем счёте не трогаем.

Схема хранит две вещи, которых раньше не было, — обе нужны только для
разбора результатов, на торговые решения они не влияют:

1. **Снимок сигнала** (`SignalSnapshot`) на момент входа: сила удара,
   ход цены, лента, кластер, оборот инструмента. Без него нельзя
   проверить, отличается ли исход сделки при сильном ударе от
   порогового — разбор 2026-08-31 упёрся именно в отсутствие этих полей.

2. **Фактические цены исполнения и net PnL** из Bybit. Поля `entry`/`exit`
   заполняются ценой тикера на момент решения и на момент обнаружения
   закрытия, то есть с задержкой до цикла поллинга. На выборке за
   2026-08-21..31 это завышало долю прибыльных сделок с 36% до 41%.
   Поэтому рядом лежат `entry_real` / `exit_real` / `pnl_net` — то, что
   реально произошло на бирже.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SignalSnapshot:
    """Что бот видел в момент входа. Только для последующего анализа."""

    burst_usd: float = 0.0
    move_pct: float = 0.0
    tape_buy: float = 0.0
    tape_sell: float = 0.0
    cluster_frac: float = 0.0
    turnover24h: float = 0.0


_SIGNAL_COLUMNS = {
    "burst_usd": "REAL",
    "move_pct": "REAL",
    "tape_buy": "REAL",
    "tape_sell": "REAL",
    "cluster_frac": "REAL",
    "turnover24h": "REAL",
}

_SIGNAL_FIELDS = tuple(_SIGNAL_COLUMNS)


class ImpulseDB:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                 symbol TEXT PRIMARY KEY,
                 side TEXT NOT NULL,
                 qty REAL NOT NULL,
                 entry REAL NOT NULL,
                 sl REAL NOT NULL,
                 tp REAL NOT NULL,
                 ts_open INTEGER NOT NULL,
                 link_id TEXT NOT NULL
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts_open INTEGER, ts_close INTEGER,
                 symbol TEXT, side TEXT, qty REAL,
                 entry REAL, exit REAL, pnl_usd REAL, reason TEXT
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS session_day (
                 day TEXT PRIMARY KEY,
                 trades INTEGER NOT NULL
               )""")
        # Миграция существующих БД: ADD COLUMN не трогает старые строки,
        # у них новые поля останутся NULL.
        self._ensure_columns("positions", {**_SIGNAL_COLUMNS, "entry_real": "REAL"})
        self._ensure_columns("trades", {
            **_SIGNAL_COLUMNS,
            "entry_real": "REAL",
            "exit_real": "REAL",
            "pnl_net": "REAL",
        })
        self._db.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        have = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def owned(self, symbol: str) -> dict | None:
        cols = ("symbol", "side", "qty", "entry", "sl", "tp", "ts_open",
                "link_id", "entry_real", *_SIGNAL_FIELDS)
        row = self._db.execute(
            f"SELECT {', '.join(cols)} FROM positions WHERE symbol=?",
            (symbol,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def open_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM positions").fetchone()[0])

    def all_owned(self) -> list[dict]:
        cols = ("symbol", "side", "qty", "entry", "sl", "tp", "ts_open",
                "link_id", "entry_real", *_SIGNAL_FIELDS)
        rows = self._db.execute(
            f"SELECT {', '.join(cols)} FROM positions").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def open_pos(self, symbol: str, side: str, qty: float, entry: float,
                 sl: float, tp: float, link_id: str, *,
                 signal: SignalSnapshot | None = None,
                 entry_real: float | None = None) -> None:
        snap = asdict(signal) if signal else dict.fromkeys(_SIGNAL_FIELDS, None)
        cols = ("symbol", "side", "qty", "entry", "sl", "tp", "ts_open",
                "link_id", "entry_real", *_SIGNAL_FIELDS)
        values = (symbol, side, qty, entry, sl, tp, int(time.time()), link_id,
                  entry_real, *(snap[f] for f in _SIGNAL_FIELDS))
        self._db.execute(
            f"INSERT OR REPLACE INTO positions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", values)
        self._db.commit()

    def set_entry_real(self, symbol: str, entry_real: float) -> None:
        """Реальная цена филла приходит уже после ответа на ордер."""
        self._db.execute("UPDATE positions SET entry_real=? WHERE symbol=?",
                         (entry_real, symbol))
        self._db.commit()

    def close_pos(self, symbol: str, exit_px: float, reason: str, *,
                  exit_real: float | None = None,
                  pnl_net: float | None = None) -> None:
        """Переносит позицию в trades.

        `exit_px` / `pnl_usd` — приблизительные (цена тикера, расчёт без
        комиссий), оставлены для совместимости. `exit_real` / `pnl_net` —
        факт с биржи, если его удалось получить.
        """
        pos = self.owned(symbol)
        if not pos:
            return
        sign = 1 if pos["side"] == "Buy" else -1
        pnl = sign * (exit_px - pos["entry"]) * pos["qty"]
        cols = ("ts_open", "ts_close", "symbol", "side", "qty", "entry", "exit",
                "pnl_usd", "reason", "entry_real", "exit_real", "pnl_net",
                *_SIGNAL_FIELDS)
        values = (pos["ts_open"], int(time.time()), symbol, pos["side"],
                  pos["qty"], pos["entry"], exit_px, pnl, reason,
                  pos.get("entry_real"), exit_real, pnl_net,
                  *(pos.get(f) for f in _SIGNAL_FIELDS))
        self._db.execute(
            f"INSERT INTO trades ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", values)
        self._db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._db.commit()

    def session_trades(self, day: str) -> int:
        row = self._db.execute(
            "SELECT trades FROM session_day WHERE day=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def bump_session(self, day: str) -> None:
        n = self.session_trades(day) + 1
        self._db.execute(
            "INSERT OR REPLACE INTO session_day (day, trades) VALUES (?,?)",
            (day, n))
        self._db.commit()
