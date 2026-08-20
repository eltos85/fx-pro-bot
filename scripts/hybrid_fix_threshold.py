"""Какой порог закрытия выбрать: считаем каждый вариант на истории эфира.

Правила стратегии зафиксированы в STRATEGY_HYBRID.md §17.4 и здесь не
обсуждаются. Открыт был один вопрос (§17.5): на каком расстоянии от средней
цены входа закрывать позицию целиком. Скрипт считает каждый вариант порога и
показывает, как он делит деньги: много мелких закрытий или мало крупных.

Что воспроизводится:
  * покупка, пока трендовое правило говорит «покупать» — SMA20/50 на 4h,
    как у свинга (src/horizon_bot);
  * заявка на закрытие ВСЕЙ позиции на уровне «средняя цена входа + порог»;
    срабатывание считается по максимуму свечи, как у обычной заявки;
  * сразу обратный вход тем же объёмом по той же цене — по факту так и было
    (0-2 минуты, ±0.3%, §17.3);
  * закрытие по трендовому правилу, если порог до этого не достали.

Издержки: taker 0.055% на каждую ногу, обе ноги круга
(<https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate>) — считаем по
дорогому тарифу, чтобы не приукрасить. Объём по умолчанию $7 000 — примерно
то, чем торговал свинг в разобранных событиях (3.56 ETH по 1918).

Запуск (данные публичные, ключи не нужны):
    python3 scripts/hybrid_fix_threshold.py --days 1460
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scalp_swing_research import fetch  # noqa: E402  протокол загрузки баров

TAKER = 0.00055
CORE_FAST, CORE_SLOW = 20, 50
THRESHOLDS_PCT = (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0)


def core_states(bars: list[tuple]) -> dict[int, int]:
    """{время открытия свечи: покупать или нет с этого открытия}.

    Решение считается по закрытию свечи, исполняется на открытии следующей —
    тот же порядок, что во всех прошлых расчётах.
    """
    closes = [b[4] for b in bars]
    out: dict[int, int] = {}
    for i in range(len(bars) - 1):
        if i + 1 < CORE_SLOW:
            continue
        fast = statistics.fmean(closes[i + 1 - CORE_FAST:i + 1])
        slow = statistics.fmean(closes[i + 1 - CORE_SLOW:i + 1])
        out[bars[i + 1][0]] = 1 if fast > slow else 0
    return out


def run(bars: list[tuple], states: dict[int, int], *, threshold_pct: float,
        notional: float) -> dict:
    """Один прогон с заданным порогом. Возвращает список закрытий и итоги."""
    want = 0
    qty = 0.0
    avg = 0.0
    opened_ts = 0
    fees = 0.0
    events: list[dict] = []
    rule_exits: list[dict] = []

    def enter(price: float, ts: int) -> None:
        nonlocal qty, avg, fees, opened_ts
        qty = notional / price
        avg = price
        opened_ts = ts
        fees += notional * TAKER

    def close(price: float, ts: int, reason: str) -> None:
        nonlocal qty, avg, fees
        gross = (price - avg) * qty
        fees += qty * price * TAKER
        row = {"ts": ts, "price": price, "avg": avg, "qty": qty,
               "gross": gross, "hours": (ts - opened_ts) / 3_600_000,
               "reason": reason}
        (events if reason == "порог" else rule_exits).append(row)
        qty, avg = 0.0, 0.0

    for ts, op, hi, _lo, cl in bars:
        new_want = states.get(ts)
        if new_want == 1 and want == 0:
            want = 1
            enter(op, ts)
        elif new_want == 0 and want == 1:
            if qty > 0:
                close(op, ts, "правило")
            want = 0

        if want != 1 or qty <= 0:
            continue

        # Внутри одной свечи порог может сработать несколько раз: после
        # обратного входа средняя цена поднимается, и следующий уровень тоже
        # может оказаться под максимумом этой же свечи. Цикл конечен, потому
        # что уровень с каждым разом выше.
        while True:
            target = avg * (1 + threshold_pct / 100)
            if hi < target:
                break
            close(target, ts, "порог")
            enter(target, ts)   # обратный вход сразу, как по факту
        _ = cl

    if qty > 0:
        close(bars[-1][4], bars[-1][0], "конец истории")

    gross_all = sum(e["gross"] for e in events) \
        + sum(e["gross"] for e in rule_exits)
    span_days = (bars[-1][0] - bars[0][0]) / 86_400_000 if bars else 0.0
    return {
        "events": events, "rule_exits": rule_exits, "fees": fees,
        "gross": gross_all, "net": gross_all - fees,
        "span_days": span_days,
    }


def show(threshold: float, r: dict) -> None:
    """Одна строка на вариант порога.

    Показываются обе половины результата. Закрытия по порогу всегда плюсовые
    по построению, но позиция ещё закрывается по трендовому правилу, и вот там
    сидят убытки. Без второй половины строка выглядела бы как большой плюс.
    """
    ev = r["events"]
    months = r["span_days"] / 30.4 if r["span_days"] else 1.0
    if not ev:
        print(f"  +{threshold:<4.1f}%  ни одного закрытия по порогу")
        return
    money = sorted(e["gross"] for e in ev)
    waits = sorted(e["hours"] for e in ev)
    rule_sum = sum(e["gross"] for e in r["rule_exits"])
    print(f"  +{threshold:<4.1f}%  "
          f"{len(ev):>4} закрытий по порогу ({len(ev)/months:>4.1f} в месяц), "
          f"в каждом ${statistics.median(money):>5,.0f}, "
          f"ждали {statistics.median(waits):>4.1f} ч  |  "
          f"по порогу {sum(money):+9,.0f} $, "
          f"по правилу {rule_sum:+9,.0f} $ ({len(r['rule_exits'])} шт), "
          f"комиссии {-r['fees']:+8,.0f} $  =  "
          f"на руки {r['net']:+9,.0f} $")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--notional", type=float, default=7000.0)
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    start = int((time.time() - args.days * 86400) * 1000)
    bars = fetch(sess, args.symbol, "240", start)
    if len(bars) < CORE_SLOW + 10:
        raise SystemExit("баров мало")
    states = core_states(bars)

    print(f"{args.symbol}: {len(bars)} 4-часовых свечей за {args.days} дней, "
          f"объём позиции ${args.notional:,.0f}, комиссия "
          f"{TAKER*100:.3f}% с каждой ноги")
    print("Правило покупки — SMA20/50 на 4h, как у свинга. Закрываем весь "
          "объём на уровне «средняя цена входа + порог» и сразу входим снова.\n")

    results = {}
    for th in THRESHOLDS_PCT:
        results[th] = run(bars, states, threshold_pct=th,
                          notional=args.notional)
        show(th, results[th])

    print("\nЕсли порог не ставить совсем и закрывать только по трендовому "
          "правилу —")
    plain = run(bars, states, threshold_pct=10_000.0, notional=args.notional)
    print(f"  {len(plain['rule_exits'])} закрытий, "
          f"комиссии {-plain['fees']:+,.0f} $, "
          f"на руки {plain['net']:+,.0f} $")

    print("\n" + "=" * 78)
    print("Как читать: закрытия по порогу всегда в плюс — это и есть те самые "
          "крупные цифры в телеграме. Убытки приходят отдельно, когда позицию "
          "закрывает трендовое правило.")
    print("Чем больше порог, тем крупнее каждое закрытие и тем меньше комиссий:")
    for th, r in results.items():
        ev = r["events"]
        if ev:
            per = statistics.median(e["gross"] for e in ev)
            print(f"  порог +{th:.1f}% → ${per:,.0f} в закрытии, "
                  f"{len(ev)/(r['span_days']/30.4):.1f} закрытий в месяц, "
                  f"на руки {r['net']:+,.0f} $")
    return 0


if __name__ == "__main__":
    sys.exit(main())
