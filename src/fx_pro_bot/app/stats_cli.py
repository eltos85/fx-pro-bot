"""CLI: оценка советов (угадал/ошибся) и отчёт по базе."""

from __future__ import annotations

import argparse
import sys

from fx_pro_bot.config.settings import Settings
from fx_pro_bot.stats.store import StatsStore, Verdict


def _cmd_report(store: StatsStore) -> int:
    s = store.summary()
    print("Всего советов:", s["total"])
    print("Без оценки (pending):", s["pending"])
    print("Оценено:", s["judged"])
    print("Верно:", s["right"])
    print("Неверно:", s["wrong"])
    print("Доля верных среди оценённых:", s["accuracy"])
    return 0


def _cmd_list(store: StatsStore, limit: int) -> int:
    for row in store.list_recent(limit=limit):
        print(row.id, row.created_at.isoformat(), row.direction, row.verdict)
        print(row.advice_text[:200].replace("\n", " "), "…" if len(row.advice_text) > 200 else "")
        print()
    return 0


def _cmd_mark(store: StatsStore, suggestion_id: str, verdict: Verdict, notes: str | None) -> int:
    if verdict == "pending":
        print("Используйте right или wrong", file=sys.stderr)
        return 2
    ok = store.set_verdict(suggestion_id, verdict, notes=notes)
    if not ok:
        print("Запись не найдена:", suggestion_id, file=sys.stderr)
        return 1
    print("Оценка сохранена:", suggestion_id, verdict)
    return 0


def main() -> None:
    settings = Settings()
    store = StatsStore(settings.stats_db_path)

    parser = argparse.ArgumentParser(prog="fx-pro-stats", description="Статистика советов советника")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="Сводка по базе")

    p_list = sub.add_parser("list", help="Последние записи")
    p_list.add_argument("-n", type=int, default=20, help="Сколько строк")

    p_mark = sub.add_parser("mark", help="Проставить оценку по id (из list)")
    p_mark.add_argument("id", help="UUID записи")
    p_mark.add_argument("verdict", choices=("right", "wrong"))
    p_mark.add_argument("--notes", default=None, help="Заметка")

    args = parser.parse_args()

    if args.cmd == "report":
        raise SystemExit(_cmd_report(store))
    if args.cmd == "list":
        raise SystemExit(_cmd_list(store, args.n))
    if args.cmd == "mark":
        raise SystemExit(_cmd_mark(store, args.id, args.verdict, args.notes))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
