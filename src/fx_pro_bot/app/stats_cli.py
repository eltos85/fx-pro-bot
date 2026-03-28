"""CLI: статистика сигналов, верификации и ручная оценка."""

from __future__ import annotations

import argparse
import sys

from fx_pro_bot.config.settings import Settings, display_name
from fx_pro_bot.stats.store import StatsStore, Verdict


def _cmd_report(store: StatsStore, settings: Settings) -> int:
    s = store.summary()
    print("=== Сигналы ===")
    print(f"  Всего: {s['total']}")
    print(f"  Без оценки (pending): {s['pending']}")
    print(f"  Оценено вручную: {s['judged']}")
    print(f"  Верно: {s['right']}")
    print(f"  Неверно: {s['wrong']}")
    print(f"  Доля верных (ручная): {s['accuracy']:.1%}")

    print()
    print("=== Автопроверка ===")
    for h in settings.verify_horizons:
        vs = store.verification_summary(h)
        if vs["total"] == 0:
            continue
        print(f"  Горизонт {h}м:")
        print(f"    Проверено: {vs['total']}")
        print(f"    Win-rate: {vs['win_rate']:.0%}")
        print(f"    Средний профит: {vs['avg_profit']:+.1f} пунктов")
        print(f"    Суммарный профит: {vs['total_profit']:+.1f} пунктов")
        print(f"    Средний выигрыш: {vs['avg_win']:+.1f} пунктов")
        print(f"    Средний проигрыш: {vs['avg_loss']:+.1f} пунктов")

    print()
    print("=== По инструментам ===")
    by_instr = store.verification_summary_by_instrument()
    if not by_instr:
        print("  Нет данных")
    for row in by_instr:
        name = display_name(str(row["instrument"]))
        print(
            f"  {name}: {row['total']} проверок, "
            f"win-rate {row['win_rate']:.0%}, "
            f"{row['total_profit']:+.1f} пунктов"
        )

    return 0


def _cmd_list(store: StatsStore, limit: int) -> int:
    for row in store.list_recent(limit=limit):
        name = display_name(row.instrument)
        print(f"[{row.id[:8]}] {row.created_at:%Y-%m-%d %H:%M} {name} {row.direction} @ {row.price_at_signal or '?'}")

        verifications = store.verifications_for(row.id)
        if verifications:
            for v in verifications:
                print(f"  └─ {v.horizon_minutes}м: {v.profit_pips:+.1f} пунктов ({v.verdict})")
        else:
            print(f"  └─ ручная оценка: {row.verdict}")
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

    parser = argparse.ArgumentParser(prog="fx-pro-stats", description="Статистика сканера-советника")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("report", help="Полный отчёт: сигналы, верификация, инструменты")

    p_list = sub.add_parser("list", help="Последние сигналы с результатами проверки")
    p_list.add_argument("-n", type=int, default=20, help="Сколько строк")

    p_mark = sub.add_parser("mark", help="Проставить ручную оценку по id (из list)")
    p_mark.add_argument("id", help="UUID записи (можно первые 8 символов из list)")
    p_mark.add_argument("verdict", choices=("right", "wrong"))
    p_mark.add_argument("--notes", default=None, help="Заметка")

    args = parser.parse_args()

    if args.cmd == "report":
        raise SystemExit(_cmd_report(store, settings))
    if args.cmd == "list":
        raise SystemExit(_cmd_list(store, args.n))
    if args.cmd == "mark":
        raise SystemExit(_cmd_mark(store, args.id, args.verdict, args.notes))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
