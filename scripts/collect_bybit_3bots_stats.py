"""Сбор статистики 3-х Bybit-ботов через API (source of truth) + локальные БД.

Bybit API — источник правды по `.cursor/rules/no-data-fitting.mdc`
и явному правилу пользователя: "ВСЕГДА апи главнее".

Запуск на VPS (через любой контейнер с pybit, например ai-trader):

    docker run --rm \\
        --env-file /root/fx-pro-bot/.env \\
        -v ai_trader_data:/data_at:ro \\
        -v ai_arena_data:/data_aa:ro \\
        -v bybit_data:/data_bb:ro \\
        -v /tmp/collect_bybit_3bots_stats.py:/script.py:ro \\
        ai-trader:local \\
        python /script.py --days 30

3 бота:
- `bybit-bot`   : key BYBIT_BOT_API_KEY/SECRET (shared subaccount с ai-trader)
- `ai-trader`   : key AI_TRADER_BYBIT_API_KEY/SECRET (тот же subaccount)
- `ai-arena`    : key AI_ARENA_BYBIT_API_KEY/SECRET (ОТДЕЛЬНЫЙ subaccount)

Split shared subaccount по orderLinkId:
- ai-trader = orderLinkId starts with "ai_" (включая "ai_close_*")
- bybit-bot = всё остальное (orderLinkId пустой или не "ai_*")
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

try:
    from pybit.unified_trading import HTTP
except ImportError:
    print("ERROR: pybit не установлен. Запусти через ai-trader/ai-arena container.", file=sys.stderr)
    sys.exit(2)


# ─── Константы ───────────────────────────────────────────────────────────

BYBIT_DEMO = True  # demo окружение для всех 3 ботов
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000  # API window для closed-pnl


# ─── Структуры ───────────────────────────────────────────────────────────


@dataclass
class ApiTradeStats:
    n_trades: int = 0
    n_wins: int = 0
    sum_pnl: float = 0.0
    by_symbol: dict[str, dict] = field(default_factory=dict)


@dataclass
class DbTradeStats:
    n_closed: int = 0
    n_wins: int = 0
    sum_pnl: float = 0.0
    n_open: int = 0


@dataclass
class WalletInfo:
    equity: float | None = None
    available: float | None = None
    error: str | None = None


@dataclass
class PositionInfo:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealised_pnl: float
    leverage: str
    order_link_id: str = ""


# ─── Bybit API ───────────────────────────────────────────────────────────


def _make_session(api_key: str, api_secret: str) -> HTTP:
    return HTTP(
        demo=BYBIT_DEMO,
        api_key=api_key,
        api_secret=api_secret,
        recv_window=20000,
    )


def fetch_closed_pnl(
    session: HTTP,
    *,
    start_ms: int,
    end_ms: int,
    category: str = "linear",
) -> list[dict]:
    """Все closed PnL records в [start_ms, end_ms]. Без фильтра по orderLinkId,
    т.к. Bybit `get_closed_pnl` НЕ возвращает orderLinkId (всегда None).
    Split делается отдельно через `fetch_order_history` (там orderLinkId есть).
    """
    out: list[dict] = []
    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            try:
                resp = session.get_closed_pnl(
                    category=category,
                    startTime=cur_start,
                    endTime=cur_end,
                    limit=200,
                    cursor=cursor,
                )
            except Exception as e:
                print(f"  WARN closed_pnl call failed: {e}", file=sys.stderr)
                break
            ret = resp.get("retCode", -1)
            if ret != 0:
                print(f"  WARN closed_pnl retCode={ret} msg={resp.get('retMsg')}", file=sys.stderr)
                break
            result = resp.get("result", {}) or {}
            for row in result.get("list", []) or []:
                out.append(row)
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
            time.sleep(0.05)
        cur_start = cur_end
        time.sleep(0.05)
    return out


def fetch_order_history_link_ids(
    session: HTTP, *, start_ms: int, end_ms: int, category: str = "linear",
) -> dict[str, str]:
    """Возвращает mapping `orderId -> orderLinkId` за период.

    Bybit `get_order_history` возвращает orderLinkId в каждом ордере
    (в отличие от closed_pnl). Окно тоже до 7 дней per call.
    """
    out: dict[str, str] = {}
    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            try:
                resp = session.get_order_history(
                    category=category,
                    startTime=cur_start,
                    endTime=cur_end,
                    limit=50,
                    cursor=cursor,
                )
            except Exception as e:
                print(f"  WARN order_history call failed: {e}", file=sys.stderr)
                break
            if resp.get("retCode") != 0:
                print(f"  WARN order_history retCode={resp.get('retCode')}", file=sys.stderr)
                break
            result = resp.get("result", {}) or {}
            for o in result.get("list", []) or []:
                oid = (o.get("orderId") or "").strip()
                lid = (o.get("orderLinkId") or "").strip()
                if oid:
                    out[oid] = lid
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
            time.sleep(0.05)
        cur_start = cur_end
        time.sleep(0.05)
    return out


def split_closed_pnl_by_link_id(
    rows: list[dict],
    order_link_map: dict[str, str],
    *,
    prefix: str,
) -> tuple[list[dict], list[dict]]:
    """Разделяет closedPnl на (matches_prefix, others) по orderLinkId.

    Использует order_link_map (orderId -> orderLinkId) для матча. Если
    orderId записи не найден в map (старая запись вне нашего окна
    order_history), считаем как 'others'.
    """
    matched: list[dict] = []
    others: list[dict] = []
    for row in rows:
        oid = (row.get("orderId") or "").strip()
        lid = order_link_map.get(oid, "")
        if lid.startswith(prefix):
            matched.append(row)
        else:
            others.append(row)
    return matched, others


def aggregate_api_trades(rows: list[dict]) -> ApiTradeStats:
    s = ApiTradeStats()
    by_sym: dict[str, dict] = {}
    for row in rows:
        try:
            pnl = float(row.get("closedPnl") or 0.0)
        except (ValueError, TypeError):
            pnl = 0.0
        sym = row.get("symbol", "?")
        s.n_trades += 1
        s.sum_pnl += pnl
        if pnl > 0:
            s.n_wins += 1
        d = by_sym.setdefault(sym, {"n": 0, "wins": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += pnl
        if pnl > 0:
            d["wins"] += 1
    s.by_symbol = by_sym
    return s


def fetch_open_positions(
    session: HTTP, *, category: str = "linear", settle_coin: str = "USDT"
) -> list[PositionInfo]:
    out: list[PositionInfo] = []
    try:
        resp = session.get_positions(category=category, settleCoin=settle_coin)
    except Exception as e:
        print(f"  WARN get_positions failed: {e}", file=sys.stderr)
        return out
    if resp.get("retCode") != 0:
        print(f"  WARN get_positions retCode={resp.get('retCode')}", file=sys.stderr)
        return out
    for p in (resp.get("result", {}) or {}).get("list", []) or []:
        size = float(p.get("size") or 0.0)
        if size == 0:
            continue
        out.append(PositionInfo(
            symbol=p["symbol"],
            side=p.get("side", "?"),
            size=size,
            entry_price=float(p.get("avgPrice") or 0.0),
            unrealised_pnl=float(p.get("unrealisedPnl") or 0.0),
            leverage=p.get("leverage", "1"),
            # У "обычных" позиций orderLinkId не возвращается
            # из get_positions — для split нужен closedPnl history.
            order_link_id="",
        ))
    return out


def fetch_wallet(session: HTTP) -> WalletInfo:
    w = WalletInfo()
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED")
    except Exception as e:
        w.error = f"exception: {e}"
        return w
    if resp.get("retCode") != 0:
        w.error = f"retCode={resp.get('retCode')} msg={resp.get('retMsg')}"
        return w
    lst = (resp.get("result", {}) or {}).get("list", []) or []
    if not lst:
        w.error = "empty list"
        return w
    acct = lst[0]
    try:
        w.equity = float(acct.get("totalEquity") or 0.0)
        w.available = float(acct.get("totalAvailableBalance") or 0.0)
    except (ValueError, TypeError) as e:
        w.error = f"parse: {e}"
    return w


# ─── Local SQLite ────────────────────────────────────────────────────────


def db_stats_ai(db_path: str, since_iso: str) -> DbTradeStats | None:
    """ai_trader / ai_arena схема (одинаковая)."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        print(f"  DB open error {db_path}: {e}", file=sys.stderr)
        return None
    try:
        s = DbTradeStats()
        cur = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(realized_pnl_usd), 0),
                      COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0)
               FROM positions
               WHERE closed_at IS NOT NULL AND closed_at >= ?""",
            (since_iso,),
        )
        row = cur.fetchone()
        if row:
            s.n_closed = int(row[0] or 0)
            s.sum_pnl = float(row[1] or 0.0)
            s.n_wins = int(row[2] or 0)
        cur = conn.execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL")
        row = cur.fetchone()
        if row:
            s.n_open = int(row[0] or 0)
        return s
    finally:
        conn.close()


def db_stats_bybit_bot(db_path: str, since_iso: str) -> DbTradeStats | None:
    """bybit-bot схема: pnl_usd, closed_at."""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        print(f"  DB open error {db_path}: {e}", file=sys.stderr)
        return None
    try:
        s = DbTradeStats()
        cur = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(pnl_usd), 0),
                      COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0)
               FROM positions
               WHERE closed_at IS NOT NULL AND closed_at >= ?""",
            (since_iso,),
        )
        row = cur.fetchone()
        if row:
            s.n_closed = int(row[0] or 0)
            s.sum_pnl = float(row[1] or 0.0)
            s.n_wins = int(row[2] or 0)
        cur = conn.execute("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL")
        row = cur.fetchone()
        if row:
            s.n_open = int(row[0] or 0)
        return s
    finally:
        conn.close()


# ─── Reporting ───────────────────────────────────────────────────────────


def fmt_money(x: float) -> str:
    return f"${x:+.2f}" if x else "$0.00"


def fmt_wr(wins: int, n: int) -> str:
    if n <= 0:
        return "—"
    return f"{wins / n * 100:.0f}%"


def render_bot(
    name: str,
    *,
    api_stats: ApiTradeStats,
    api_open: list[PositionInfo] | None,
    api_open_filter: str | None,
    wallet: WalletInfo | None,
    db_stats: DbTradeStats | None,
    period_days: int,
) -> str:
    lines = []
    lines.append("┌" + "─" * 60 + "┐")
    lines.append(f"│ Bot: {name}".ljust(61) + "│")
    if api_open_filter:
        lines.append(f"│ Subaccount filter: orderLinkId {api_open_filter}".ljust(61) + "│")
    lines.append("└" + "─" * 60 + "┘")
    lines.append("")
    lines.append(f"API (last {period_days}d, source of truth):")
    lines.append(f"  Closed trades : {api_stats.n_trades}")
    lines.append(f"  Wins / WR     : {api_stats.n_wins} / {fmt_wr(api_stats.n_wins, api_stats.n_trades)}")
    lines.append(f"  Net PnL (sum) : {fmt_money(api_stats.sum_pnl)}")
    if api_stats.n_trades:
        avg = api_stats.sum_pnl / api_stats.n_trades
        lines.append(f"  Avg per trade : {fmt_money(avg)}")
    if api_stats.by_symbol:
        lines.append("  By symbol     :")
        sorted_syms = sorted(api_stats.by_symbol.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        for sym, d in sorted_syms:
            wr = fmt_wr(d["wins"], d["n"])
            lines.append(f"    {sym:10s}  n={d['n']:3d}  W={d['wins']:3d} ({wr:>4s})  pnl={fmt_money(d['pnl']):>10s}")
    if wallet is not None:
        if wallet.error:
            lines.append(f"  Wallet        : ERROR {wallet.error}")
        else:
            lines.append(f"  Wallet equity : ${wallet.equity:.2f}    available: ${wallet.available:.2f}")
    if api_open is not None:
        lines.append(f"  Open positions: {len(api_open)}")
        for p in api_open:
            lines.append(
                f"    {p.side} {p.symbol} size={p.size}  entry=${p.entry_price:.4g}  "
                f"uPnL={fmt_money(p.unrealised_pnl)}  lev={p.leverage}x"
            )
    lines.append("")
    if db_stats is not None:
        lines.append(f"DB (local SQLite, last {period_days}d):")
        lines.append(f"  Closed trades : {db_stats.n_closed}")
        lines.append(f"  Wins / WR     : {db_stats.n_wins} / {fmt_wr(db_stats.n_wins, db_stats.n_closed)}")
        lines.append(f"  Sum PnL       : {fmt_money(db_stats.sum_pnl)}")
        lines.append(f"  Open in DB    : {db_stats.n_open}")
        # Δ
        d_n = api_stats.n_trades - db_stats.n_closed
        d_pnl = api_stats.sum_pnl - db_stats.sum_pnl
        lines.append("")
        lines.append("Δ (API − DB):")
        lines.append(f"  trades : {d_n:+d}")
        lines.append(f"  PnL    : {fmt_money(d_pnl)}")
        if d_n != 0 or abs(d_pnl) > 0.10:
            lines.append("  ⚠️  расхождение API↔DB — API источник правды (правило стороны юзера)")
        else:
            lines.append("  ✓ совпадает в пределах допуска")
    else:
        lines.append("DB (local SQLite): NOT MOUNTED / NOT FOUND")
    return "\n".join(lines)


# ─── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="окно для closed PnL (дней назад)")
    ap.add_argument("--db-ai-trader", default="/data_at/ai_trader.sqlite")
    ap.add_argument("--db-ai-arena", default="/data_aa/ai_arena.sqlite")
    ap.add_argument("--db-bybit-bot", default="/data_bb/bybit_stats.sqlite")
    args = ap.parse_args()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 24 * 60 * 60 * 1000
    since_iso = (datetime.now(tz=UTC) - timedelta(days=args.days)).isoformat()

    # Креды
    bb_key = os.environ.get("BYBIT_BOT_API_KEY", "")
    bb_sec = os.environ.get("BYBIT_BOT_API_SECRET", "")
    at_key = os.environ.get("AI_TRADER_BYBIT_API_KEY", "")
    at_sec = os.environ.get("AI_TRADER_BYBIT_API_SECRET", "")
    aa_key = os.environ.get("AI_ARENA_BYBIT_API_KEY", "")
    aa_sec = os.environ.get("AI_ARENA_BYBIT_API_SECRET", "")

    if not (bb_key and at_key and aa_key):
        missing = [n for n, v in [
            ("BYBIT_BOT_API_KEY", bb_key),
            ("AI_TRADER_BYBIT_API_KEY", at_key),
            ("AI_ARENA_BYBIT_API_KEY", aa_key),
        ] if not v]
        print(f"ERROR: missing env vars: {missing}", file=sys.stderr)
        return 2

    out: list[str] = []
    out.append("═" * 62)
    out.append(
        f"BYBIT 3-BOTS STATISTICS  ({datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')})"
    )
    out.append(f"Period: last {args.days} days   |   Demo: {BYBIT_DEMO}")
    out.append("═" * 62)
    out.append("")

    # 1) Shared subaccount (bybit-bot + ai-trader)
    print("[1/4] Fetching shared subaccount closed-pnl ...", file=sys.stderr)
    shared_session = _make_session(bb_key, bb_sec)  # ключи bybit-bot и ai-trader идентичны
    shared_rows = fetch_closed_pnl(shared_session, start_ms=start_ms, end_ms=end_ms)
    print(f"  shared subaccount: {len(shared_rows)} closed-pnl rows", file=sys.stderr)

    # closedPnl не возвращает orderLinkId — split через order_history
    print("[2/4] Fetching order_history for link_id map ...", file=sys.stderr)
    order_link_map = fetch_order_history_link_ids(shared_session, start_ms=start_ms, end_ms=end_ms)
    print(f"  shared subaccount: {len(order_link_map)} orders in history", file=sys.stderr)
    rows_at, rows_bb = split_closed_pnl_by_link_id(shared_rows, order_link_map, prefix="ai_")
    unmatched_count = sum(1 for r in shared_rows if (r.get("orderId") or "") not in order_link_map)
    print(f"  split: ai-trader={len(rows_at)}  bybit-bot={len(rows_bb)}  unmatched_oid={unmatched_count}", file=sys.stderr)

    api_at = aggregate_api_trades(rows_at)
    api_bb = aggregate_api_trades(rows_bb)

    shared_open = fetch_open_positions(shared_session)
    shared_wallet = fetch_wallet(shared_session)
    # Open positions split: на shared subaccount нельзя точно отличить
    # bybit-bot vs ai-trader из get_positions (orderLinkId не возвращается).
    # Показываем общий список под shared header + по БД-снимку каждого бота.

    # 2) ai-arena (отдельный subaccount)
    print("[3/4] Fetching ai-arena subaccount ...", file=sys.stderr)
    arena_session = _make_session(aa_key, aa_sec)
    rows_aa = fetch_closed_pnl(arena_session, start_ms=start_ms, end_ms=end_ms)
    api_aa = aggregate_api_trades(rows_aa)
    arena_open = fetch_open_positions(arena_session)
    arena_wallet = fetch_wallet(arena_session)
    print(f"  arena subaccount: {len(rows_aa)} closed-pnl rows", file=sys.stderr)

    # Диагностика временного диапазона ai-arena (на случай если БД свежая
    # и старые трейды — это предыдущий инстанс / другой пользователь).
    arena_timestamps = []
    for r in rows_aa:
        try:
            ts = int(r.get("updatedTime") or 0)
            if ts:
                arena_timestamps.append(ts)
        except (ValueError, TypeError):
            pass
    arena_first_ts = min(arena_timestamps) if arena_timestamps else None
    arena_last_ts = max(arena_timestamps) if arena_timestamps else None

    # 3) Local DB stats
    print("[4/4] Reading local SQLite ...", file=sys.stderr)
    db_at = db_stats_ai(args.db_ai_trader, since_iso)
    db_aa = db_stats_ai(args.db_ai_arena, since_iso)
    db_bb = db_stats_bybit_bot(args.db_bybit_bot, since_iso)

    # ─── Shared subaccount header ─────────────────────────────────────
    out.append("┃ SHARED Bybit subaccount (bybit-bot + ai-trader делят один аккаунт) ┃")
    out.append(f"┃ Wallet equity : "
               f"{'ERROR ' + (shared_wallet.error or '') if shared_wallet.error else f'${shared_wallet.equity:.2f}'}")
    if not shared_wallet.error:
        out.append(f"┃ Available     : ${shared_wallet.available:.2f}")
    out.append(f"┃ Open positions on exchange (any bot): {len(shared_open)}")
    for p in shared_open:
        out.append(
            f"┃   {p.side} {p.symbol} size={p.size}  entry=${p.entry_price:.4g}  "
            f"uPnL={fmt_money(p.unrealised_pnl)}  lev={p.leverage}x"
        )
    out.append("")

    # ─── Per-bot blocks ───────────────────────────────────────────────
    out.append(render_bot(
        "bybit-bot",
        api_stats=api_bb,
        api_open=None,  # split open невозможен на shared subaccount
        api_open_filter="NOT startswith('ai_')",
        wallet=None,
        db_stats=db_bb,
        period_days=args.days,
    ))
    out.append("")
    out.append(render_bot(
        "ai-trader",
        api_stats=api_at,
        api_open=None,
        api_open_filter="startswith('ai_')",
        wallet=None,
        db_stats=db_at,
        period_days=args.days,
    ))
    out.append("")
    arena_block = render_bot(
        "ai-arena",
        api_stats=api_aa,
        api_open=arena_open,
        api_open_filter=None,
        wallet=arena_wallet,
        db_stats=db_aa,
        period_days=args.days,
    )
    if arena_first_ts and arena_last_ts:
        arena_first = datetime.fromtimestamp(arena_first_ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        arena_last = datetime.fromtimestamp(arena_last_ts / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        arena_block += (
            f"\n\nAPI-trades time range:\n"
            f"  first : {arena_first}\n"
            f"  last  : {arena_last}\n"
            f"  (если БД создана позже first → старые трейды от предыдущего "
            f"инстанса, не от текущего ai-arena кода)"
        )
    out.append(arena_block)

    # ─── Combined summary ─────────────────────────────────────────────
    out.append("")
    out.append("═" * 62)
    out.append("SUMMARY (3 bots combined, API source of truth):")
    out.append(f"  bybit-bot : {api_bb.n_trades:4d} trades  WR {fmt_wr(api_bb.n_wins, api_bb.n_trades):>4s}  PnL {fmt_money(api_bb.sum_pnl):>10s}")
    out.append(f"  ai-trader : {api_at.n_trades:4d} trades  WR {fmt_wr(api_at.n_wins, api_at.n_trades):>4s}  PnL {fmt_money(api_at.sum_pnl):>10s}")
    out.append(f"  ai-arena  : {api_aa.n_trades:4d} trades  WR {fmt_wr(api_aa.n_wins, api_aa.n_trades):>4s}  PnL {fmt_money(api_aa.sum_pnl):>10s}")
    total_n = api_bb.n_trades + api_at.n_trades + api_aa.n_trades
    total_w = api_bb.n_wins + api_at.n_wins + api_aa.n_wins
    total_pnl = api_bb.sum_pnl + api_at.sum_pnl + api_aa.sum_pnl
    out.append("  " + "─" * 58)
    out.append(f"  TOTAL     : {total_n:4d} trades  WR {fmt_wr(total_w, total_n):>4s}  PnL {fmt_money(total_pnl):>10s}")
    out.append("═" * 62)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
