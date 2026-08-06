#!/usr/bin/env python3
"""Fail-closed readiness checkpoint для activation этапа 5.

Скрипт только читает SQLite и никогда не меняет конфиг/торговую логику.
READY означает лишь, что выборка достаточна и не сконцентрирована; после READY
всё равно обязательны effect size, p<0.05, BH-FDR/CI, PF и expectancy из
специализированных отчётов.

Готовность (v0.18.50) = ``исходов ≥ MIN_OUTCOMES`` И ``независимых символо-дней
≥ MIN_CLUSTERS`` И все режимные ячейки заполнены. Календарный размах остался
только справочной колонкой: он был прокси для разнообразия режимов и мерил не
то (см. комментарий у MIN_CLUSTERS).

Готовность монотонна (v0.18.56): порог волатильности заморожен, поэтому новые
данные могут только приблизить гипотезу к READY, но не отозвать разрешение
задним числом. Порог печатается в шапке отчёта вместе с актуальной медианой
популяции — расхождение видно сразу и служит поводом пересмотреть заморозку.

Usage:
  python scripts/scalp_forward_checkpoint.py \
    --db /data/scalp_bot.sqlite --cutoff 2026-07-22T14:08:00Z
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_episodes import DEDUPED_SETUP_TYPES, collapse_episodes  # noqa: E402


DEFAULT_CUTOFF = "2026-07-22T14:08:00Z"
MIN_OUTCOMES = 100
# Независимые кластеры вместо календарного размаха (v0.18.50). Календарь был
# лишь ПРОКСИ для того, что требует sample-size.mdc: «≥2 недели данных (в разных
# рыночных режимах: тренд, флет, новости)». Прокси мерил не то: maker_nonfill
# прошёл 14 дней, имея всего 3 символа и 16 символо-дней, а sl_widen набрал
# «154 исхода», из которых 41 (27%) — один ZECUSDT за одно 29.07. Теперь
# проверяем напрямую обе вещи, которые прокси пытался покрыть: разнообразие
# режимов и число независимых кластеров.
#
# Порог 40: при кластеризованных данных cluster-robust инференс ненадёжен на
# малом числе кластеров; общепринятое эмпирическое правило — ~40+.
# Cameron & Miller (2015) «A Practitioner's Guide to Cluster-Robust Inference»,
# Journal of Human Resources 50(2):317–372, §VI «few clusters»;
# Angrist & Pischke (2009) «Mostly Harmless Econometrics» ch.8 («42 clusters»).
MIN_CLUSTERS = 40
# Кластер = символ×день: внутри одного символа за один день наблюдения
# сильно скоррелированы (тот же уровень, тот же режим, те же участники).
CLUSTER_KEY = "symbol-day"
# Ось тренда: ADX≥25 — канонический порог Wilder (1978) «New Concepts in
# Technical Trading Systems»; ниже — безтрендовый рынок.
ADX_TREND = 25.0
# Ось волатильности: порог ЗАМОРОЖЕН (v0.18.56). Раньше медиана пересчитывалась
# на каждом запуске, и готовность перестала быть монотонной: 2026-08-06
# медиана уехала 0.367→0.432 (в популяцию вошли 22 counterfactual-дня), четыре
# символо-дня сменили ярлык волатильно→тихо, и canon_rejection откатился с 4/4
# режимов на 3/4, имея СТРОГО больше данных. Гейт, санкционирующий анализ, не
# может отзывать разрешение задним числом.
#
# Значение выведено из данных, а не выбрано: медиана NATR по 73 символо-дням
# объединённой популяции с cutoff 2026-07-22T14:08:00Z, посчитана 2026-08-06.
# Порог привязан к поколению эксперимента: меняется cutoff → пересчитать
# (`--recompute-split` печатает актуальную медиану, но НЕ правит константу —
# правка руками и в коммите, иначе воспроизводимость снова теряется).
NATR_SPLIT = 0.432055
NATR_SPLIT_BASIS = "медиана 73 символо-дней с cutoff 2026-07-22, зафикс. 2026-08-06"
REQUIRED_CELLS = ("тренд/волатильно", "тренд/тихо",
                  "флет/волатильно", "флет/тихо")


@dataclass(frozen=True)
class Readiness:
    hypothesis: str
    outcomes: int
    first_ts: float | None
    last_ts: float | None
    clusters: int = 0
    cells: frozenset[str] = frozenset()

    @property
    def span_days(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return max(0.0, (self.last_ts - self.first_ts) / 86_400.0)

    @property
    def missing_cells(self) -> tuple[str, ...]:
        return tuple(c for c in REQUIRED_CELLS if c not in self.cells)

    @property
    def ready(self) -> bool:
        return (self.outcomes >= MIN_OUTCOMES
                and self.clusters >= MIN_CLUSTERS
                and not self.missing_cells)


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def regime_cells(con: sqlite3.Connection,
                 cutoff: float) -> dict[tuple[str, str], str]:
    """Режимная ячейка для каждого символо-дня: тренд/флет × волатильно/тихо.

    Источников два, и они объединяются (v0.18.55). ``shadow_signals`` покрывает
    только символы, чей сигнал дошёл до боевых гейтов, — теневая вселенная туда
    не пишет по устройству, а density-тени попадают редко. Пока карта строилась
    на одном этом источнике, у трёх семейств гипотез было размечено 4–29%
    кластеров, и требование «все четыре режима» не могло выполниться в принципе.
    Второй источник — режим, записанный самим counterfactual-кандидатом при
    рождении; он покрывает свою гипотезу по определению.

    Порог волатильности — замороженный ``NATR_SPLIT`` (v0.18.56). Он выведен из
    данных (медиана объединённой популяции), но зафиксирован в коде, потому что
    пересчёт на каждом запуске делал готовность немонотонной. Деление одно и то
    же для всех гипотез намеренно: «волатильно» должно означать «относительно
    всего, что бот наблюдает», а не относительно узкой популяции самой гипотезы.
    Per-hypothesis медиана давала бы 50/50 по построению и превратила бы
    требование разнообразия режимов в тавтологию.
    """
    sources = (
        """SELECT symbol, date(ts,'unixepoch'), AVG(adx), AVG(htf_natr_pct)
           FROM shadow_signals
           WHERE ts>=? AND adx IS NOT NULL AND htf_natr_pct IS NOT NULL
           GROUP BY 1,2""",
        """SELECT symbol, date(ts_candidate,'unixepoch'),
                  AVG(regime_adx), AVG(regime_natr_pct)
           FROM counterfactual_setups
           WHERE ts_candidate>=? AND regime_adx IS NOT NULL
                 AND regime_natr_pct IS NOT NULL
           GROUP BY 1,2""",
    )
    rows: list[tuple] = []
    for sql in sources:
        try:
            rows.extend(con.execute(sql, (cutoff,)).fetchall())
        except sqlite3.OperationalError:
            # Нет таблицы/колонок — не падаем, но и гейт не открываем: пустая
            # карта означает «режим неизвестен», а неизвестный ячейку не даёт.
            continue
    # Символо-день, попавший в оба источника, обязан весить столько же, сколько
    # любой другой: иначе он дважды сдвигал бы медиану волатильности.
    merged: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for symbol, day, adx, vol in rows:
        if adx is None or vol is None:
            continue
        merged.setdefault((str(symbol), str(day)), []).append(
            (float(adx), float(vol)))
    if not merged:
        return {}
    means = {k: (sum(a for a, _ in v) / len(v), sum(n for _, n in v) / len(v))
             for k, v in merged.items()}
    return {k: ("тренд" if adx >= ADX_TREND else "флет")
               + ("/волатильно" if vol >= NATR_SPLIT else "/тихо")
            for k, (adx, vol) in means.items()}


def current_natr_median(con: sqlite3.Connection, cutoff: float) -> float | None:
    """Актуальная медиана популяции — справочно, для решения о переzаморозке.

    Никогда не используется при разметке: ярлык обязан зависеть только от
    зафиксированного ``NATR_SPLIT``.
    """
    cells = regime_cells(con, cutoff)
    if not cells:
        return None
    sources = (
        """SELECT symbol, date(ts,'unixepoch'), AVG(htf_natr_pct)
           FROM shadow_signals WHERE ts>=? AND htf_natr_pct IS NOT NULL
           GROUP BY 1,2""",
        """SELECT symbol, date(ts_candidate,'unixepoch'), AVG(regime_natr_pct)
           FROM counterfactual_setups
           WHERE ts_candidate>=? AND regime_natr_pct IS NOT NULL GROUP BY 1,2""",
    )
    merged: dict[tuple[str, str], list[float]] = {}
    for sql in sources:
        try:
            for symbol, day, vol in con.execute(sql, (cutoff,)):
                if vol is not None:
                    merged.setdefault((str(symbol), str(day)), []).append(
                        float(vol))
        except sqlite3.OperationalError:
            continue
    if not merged:
        return None
    natr = sorted(sum(v) / len(v) for v in merged.values())
    return natr[len(natr) // 2]


def _readiness(hypothesis: str, observations: list[tuple[str, float]],
               cells: dict[tuple[str, str], str]) -> Readiness:
    """Свести наблюдения ``(символ, ts)`` в готовность.

    Кластеры и режимные ячейки считаются по символо-дням. Символо-день без
    режимных данных попадает в кластеры, но НЕ засчитывает ячейку: неизвестный
    режим не должен открывать гейт (fail-closed).
    """
    times = [ts for _, ts in observations]
    keys = {(symbol, _day(ts)) for symbol, ts in observations}
    return Readiness(
        hypothesis=hypothesis, outcomes=len(observations),
        first_ts=min(times) if times else None,
        last_ts=max(times) if times else None,
        clusters=len(keys),
        cells=frozenset(cells[k] for k in keys if k in cells))


def collect_readiness(con: sqlite3.Connection,
                      cutoff: float) -> list[Readiness]:
    cells = regime_cells(con, cutoff)
    result: list[Readiness] = []

    def observations(query: str, params: tuple) -> list[tuple[str, float]]:
        return [(str(sym), float(ts))
                for sym, ts in con.execute(query, params).fetchall()
                if sym is not None and ts is not None]

    # Meta-gate sweep: closed actual fills + terminal maker non-fill.
    actual = observations(
        """SELECT t.symbol,t.ts_open
           FROM trades t JOIN meta_label_features m ON m.trade_id=t.id
           WHERE t.ts_open>=? AND m.label_type='fade_exhaustion'
             AND m.would_keep IS NOT NULL AND t.status='closed'
             AND t.pnl_usd IS NOT NULL
             AND (t.close_reason IS NULL OR t.close_reason NOT LIKE 'entry_%')""",
        (cutoff,),
    )
    maker = observations(
        """SELECT c.symbol,c.ts_candidate
           FROM counterfactual_setups c
           JOIN meta_label_features m ON m.trade_id=c.source_trade_id
           WHERE c.ts_candidate>=? AND c.setup_type='maker_nonfill'
             AND m.label_type='fade_exhaustion' AND m.would_keep IS NOT NULL
             AND c.outcome_target IN ('target','sl')""",
        (cutoff,),
    )
    result.append(_readiness("sweep_fade_meta_gate", actual + maker, cells))

    # Breakout meta-label на реально завершённых V1.
    result.append(_readiness("density_break_meta_gate", observations(
        """SELECT t.symbol,t.ts_open
           FROM trades t JOIN meta_label_features m ON m.trade_id=t.id
           WHERE t.ts_open>=? AND t.strategy='density_break'
             AND m.label_type='breakout_fuel' AND m.would_keep IS NOT NULL
             AND t.status='closed' AND t.pnl_usd IS NOT NULL
             AND (t.close_reason IS NULL OR t.close_reason NOT LIKE 'entry_%')""",
        (cutoff,)), cells))

    for setup_type, hypothesis in (
        ("density_break_v2_shadow", "density_break_v2_retest"),
        ("canon_rejection_shadow", "canon_rejection_redesign"),
    ):
        if setup_type in DEDUPED_SETUP_TYPES:
            # Считаем эпизоды, а не строки: до v0.18.47 один свип писался
            # десятками кандидатов, и порог MIN_OUTCOMES брался бы дублями.
            rows = con.execute(
                """SELECT symbol,side,level_type,level_price,ts_candidate
                   FROM counterfactual_setups
                   WHERE ts_candidate>=? AND setup_type=?
                     AND outcome_target IN ('target','sl')""",
                (cutoff, setup_type),
            ).fetchall()
            episodes = collapse_episodes(
                [dict(zip(("symbol", "side", "level_type", "level_price",
                           "ts_candidate"), r)) for r in rows])
            result.append(_readiness(hypothesis, [
                (str(e["symbol"]), float(e["ts_candidate"])) for e in episodes
            ], cells))
            continue
        result.append(_readiness(hypothesis, observations(
            """SELECT symbol,ts_candidate FROM counterfactual_setups
               WHERE ts_candidate>=? AND setup_type=?
                 AND outcome_target IN ('target','sl')""",
            (cutoff, setup_type)), cells))

    def by_variant(setup_type: str,
                   terminal: str) -> dict[str, list[tuple[str, float]]]:
        """Наблюдения по ветке. ``terminal`` — какой столбец считаем исходом:
        ``outcome_target`` (дошло до +target_r) или ``outcome_tp`` (брекет
        целиком). Значения столбца зашиты рядом, чтобы не собирать SQL строкой.
        """
        column, values = {
            "target": ("outcome_target", ("target", "sl")),
            "bracket": ("outcome_tp", ("tp", "sl")),
        }[terminal]
        grouped: dict[str, list[tuple[str, float]]] = {}
        for variant, sym, ts in con.execute(
            f"""SELECT variant,symbol,ts_candidate FROM counterfactual_setups
                WHERE ts_candidate>=? AND setup_type=?
                  AND {column} IN (?,?)""",
                (cutoff, setup_type, *values)):
            if sym is None or ts is None:
                continue
            grouped.setdefault(str(variant), []).append((str(sym), float(ts)))
        return grouped

    grouped = by_variant("density_bounce_persist_shadow", "target")
    for seconds in (60, 90, 120, 180):
        variant = f"persist_{seconds}s"
        result.append(_readiness(f"density_bounce_{variant}",
                                 grouped.get(variant, []), cells))

    # v0.18.45: ширина стопа. Считаем по outcome_tp (TP vs SL), а не по
    # outcome_target: гипотеза именно про исход брекета целиком, ведь комиссия
    # в R зависит от ширины стопа, а не от промежуточного +1.5R.
    # Контрольная ×1.0 тоже проходит checkpoint — сравнивать не с чем, пока
    # у контроля нет собственной выборки.
    grouped = by_variant("sl_widen", "bracket")
    # Ветки перечисляем явно (как persist-grid): гипотеза должна быть видна в
    # отчёте с n=0, иначе про неё легко забыть до появления первых исходов.
    for variant in ("x1", "x1.5", "x2", "x3"):
        result.append(_readiness(f"sl_widen_{variant}",
                                 grouped.get(variant, []), cells))

    # v0.18.48: стоил ли чего-то порог оборота. Группируем по стратегии —
    # порог мог быть вреден для одной и полезен для другой, агрегат это скрыл бы.
    for variant, obs in sorted(
            by_variant("shadow_universe", "bracket").items()):
        result.append(_readiness(f"shadow_universe_{variant}", obs, cells))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/scalp_bot.sqlite")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    args = parser.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cutoff = _ts(args.cutoff)
    rows = collect_readiness(con, cutoff)
    live_median = current_natr_median(con, cutoff)
    con.close()

    print(f"cutoff={args.cutoff} min_n={MIN_OUTCOMES} "
          f"min_clusters={MIN_CLUSTERS} ({CLUSTER_KEY}) "
          f"нужны режимы: {', '.join(REQUIRED_CELLS)}")
    # Порог печатаем всегда: разметка режимов должна быть воспроизводима по
    # одному только выводу отчёта, без чтения исходников.
    drift = ("" if live_median is None
             else f"; текущая медиана популяции {live_median:.3f}")
    print(f"порог волатильности NATR={NATR_SPLIT:.3f} заморожен "
          f"({NATR_SPLIT_BASIS}){drift}")
    any_ready = False
    for row in rows:
        status = "READY_FOR_STATS" if row.ready else "COLLECTING"
        any_ready = any_ready or row.ready
        # Явно показываем, ЧТО именно держит гипотезу: раньше был только
        # span, и по нему нельзя было понять, мало данных или они
        # сконцентрированы в одном символе.
        blockers = []
        if row.outcomes < MIN_OUTCOMES:
            blockers.append(f"исходов {row.outcomes}/{MIN_OUTCOMES}")
        if row.clusters < MIN_CLUSTERS:
            blockers.append(f"кластеров {row.clusters}/{MIN_CLUSTERS}")
        if row.missing_cells:
            blockers.append("нет режимов: " + ",".join(row.missing_cells))
        print(f"{row.hypothesis}: {status} n={row.outcomes} "
              f"кластеров={row.clusters} режимов={len(row.cells)}/"
              f"{len(REQUIRED_CELLS)} span={row.span_days:.2f}d"
              + (f" | держит: {'; '.join(blockers)}" if blockers else ""))
    print("activation=FORBIDDEN "
          "(READY_FOR_STATS запускает статистическую проверку, не автогейт)")
    return 0 if any_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
