"""Read-only доступ к БД ботов ``scalp_bot`` / ``hybrid_bot`` (TASKSPEC §3.1).

Открываем строго read-only через SQLite URI ``mode=ro`` — запись физически
невозможна (read-only инвариант §11). tradecard НИЧЕГО не пишет в БД ботов.
Обе БД имеют идентичную схему ``trades`` → один загрузчик с параметром ``bot``.
"""
from __future__ import annotations

import os
import sqlite3

from tradecard_bybit.analysis.trade import Trade


class BotDBReadOnly:
    """Тонкая read-only обёртка над ``trades`` одной БД бота."""

    def __init__(self, db_path: str, bot: str) -> None:
        if bot not in ("scalp", "hybrid"):
            raise ValueError(f"unknown bot: {bot!r}")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"bot db not found (read-only): {db_path}")
        self._bot = bot
        self._path = db_path
        # mode=ro: соединение НЕ может писать (sqlite вернёт SQLITE_READONLY).
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                     timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    @property
    def bot(self) -> str:
        return self._bot

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BotDBReadOnly":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def closed_trades(self, *, since_ts: float = 0.0,
                      until_ts: float | None = None,
                      mode: str | None = None) -> list[Trade]:
        """Закрытые сделки с ts_close в [since_ts, until_ts), опц. фильтр mode.

        Возвращаем ВСЕ закрытия (включая non-trade) — фильтрацию реконсила делает
        потребитель через ``Trade.is_decided`` (чтобы счётчики non-trade тоже
        были видны в отчёте). Сортировка по ts_close.
        """
        q = ("SELECT * FROM trades WHERE status='closed' AND ts_close>=?")
        args: list = [since_ts]
        if until_ts is not None:
            q += " AND ts_close<?"
            args.append(until_ts)
        if mode is not None:
            q += " AND mode=?"
            args.append(mode)
        q += " ORDER BY ts_close"
        rows = self._conn.execute(q, args).fetchall()
        return [self._to_trade(r) for r in rows]

    def open_trades(self) -> list[Trade]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id").fetchall()
        return [self._to_trade(r) for r in rows]

    def _to_trade(self, r: sqlite3.Row) -> Trade:
        keys = set(r.keys())
        verified = int(r["pnl_verified"]) if "pnl_verified" in keys else 0
        provisional = int(r["pnl_provisional"]) if "pnl_provisional" in keys else 0
        if verified:
            src = "verified"
        elif provisional:
            src = "provisional"
        else:
            src = "db"
        return Trade(
            id=int(r["id"]), bot=self._bot, ts_open=float(r["ts_open"]),
            symbol=r["symbol"], side=r["side"], qty=float(r["qty"]),
            entry=float(r["entry"]), sl=float(r["sl"]), tp=float(r["tp"]),
            score=int(r["score"]), reasons_raw=r["reasons"] or "", mode=r["mode"],
            strategy=r["strategy"], status=r["status"],
            ts_close=float(r["ts_close"]) if r["ts_close"] is not None else None,
            exit=float(r["exit"]) if r["exit"] is not None else None,
            pnl_usd=float(r["pnl_usd"]) if r["pnl_usd"] is not None else None,
            fees_usd=float(r["fees_usd"]) if r["fees_usd"] is not None else None,
            close_reason=r["close_reason"],
            pnl_provisional=provisional, pnl_verified=verified, pnl_source=src,
        )
