"""Насколько замер §17.6 совпадает с тем, что бот делает на самом деле.

Замер порога (`hybrid_fix_threshold.py`, канон §17.6) считал, что закрытие
происходит ровно на уровне «средняя цена входа + порог»: так исполнилась бы
лимитная заявка. Живой бот работает иначе (src/hybrid_bot/app/main.py):

  * смотрит цену раз в ``HYBRID_POLL_SEC`` секунд (сейчас 180) — быстрый прокол
    уровня между опросами он просто не увидит, и закрытия не будет;
  * увидев цену выше уровня, отправляет РЫНОЧНЫЙ ордер — исполнение приходит
    не на уровне, а там, где стоит стакан;
  * за один цикл делает не больше одной фиксации на символ.

При пороге +1% это уже не мелочь: сам порог даёт $2.00 на ставке $200, а круг
комиссий забирает $0.22. Скрипт считает две руки на одних и тех же минутных
данных, чтобы разница была ровно про механику исполнения, а не про разное
разрешение свечей:

  «уровень»  — модель §17.6: закрытие ровно на уровне, сколько бы раз за минуту
               он ни встретился;
  «как у бота» — опрос по сетке в ``--poll`` минут, закрытие по цене последней
               увиденной минуты, не больше одной фиксации за опрос.

Проскальзывание рыночного ордера задаётся отдельным перебором (0/1/2/5 б.п. на
ногу), потому что живых фиксаций пока нет и измерить его нечем: перебор
показывает, при какой величине оно начинает менять картину. Ставка taker
0.055% с каждой ноги в обеих руках
(<https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate>).

Запуск (данные публичные, ключи не нужны):
    python3 scripts/hybrid_poll_fidelity.py --days 180 --notional 200
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hybrid_fix_threshold import TAKER, core_states  # noqa: E402
from scalp_swing_research import fetch  # noqa: E402  протокол загрузки баров

MINUTE_MS = 60_000
THRESHOLDS_PCT = (1.0, 2.0, 6.0)
SLIPS_BP = (0.0, 1.0, 2.0, 5.0)

# Минутных свечей нужны сотни тысяч — это сотни страниц по 1000 штук. Лимит
# биржи: 600 запросов на 5 секунд на IP, ответ 10006 = лимит уже пробит, и
# документация прямо не советует ходить у самой границы.
# https://bybit-exchange.github.io/docs/v5/rate-limit
PAGE_PAUSE_SEC = 0.25
PAGE_RETRIES = 6


def fetch_minutes(sess, symbol: str, start_ms: int) -> list[tuple]:
    """Минутные свечи от ``start_ms`` до сейчас — без молчаливых обрывов.

    Общий загрузчик `scalp_swing_research.fetch` на любом исключении просто
    выходит из цикла, и при упоре в лимит биржи возвращает огрызок истории,
    выглядящий как полноценный ряд (так прогон «на 4 года» 2026-08-20 оказался
    прогоном на 42 дня). Здесь страница либо приходит, либо скрипт падает.
    """
    out: dict[int, tuple] = {}
    end = int(time.time() * 1000)
    pages = 0
    while True:
        rows = None
        for attempt in range(PAGE_RETRIES):
            try:
                rows = sess.get_kline(category="linear", symbol=symbol,
                                      interval="1", start=start_ms, end=end,
                                      limit=1000)["result"]["list"]
                break
            except Exception as exc:                      # noqa: BLE001
                pause = 2 ** attempt
                print(f"  страница не пришла ({exc}), ждём {pause} с",
                      flush=True)
                time.sleep(pause)
        if rows is None:
            raise SystemExit("биржа не отдала страницу свечей после "
                             f"{PAGE_RETRIES} попыток — ряд был бы неполным")
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            out[ts] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = min(oldest, ts)
        pages += 1
        if pages % 50 == 0:
            print(f"  загружено {len(out):,} минут…", flush=True)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
        time.sleep(PAGE_PAUSE_SEC)
    return [(ts, *out[ts]) for ts in sorted(out)]


def run(minutes: list[tuple], states: dict[int, int], *, threshold_pct: float,
        notional: float, mode: str, poll_min: int = 3,
        slip_bp: float = 0.0) -> dict:
    """Один прогон. ``mode``: ``level`` (модель §17.6) или ``poll`` (как бот).

    Проскальзывание применяется только в руке ``poll``: лимитная заявка по
    построению исполняется на своём уровне, платит за рынок только вторая рука.
    """
    slip = slip_bp / 10_000 if mode == "poll" else 0.0
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
        fees += qty * price * TAKER
        row = {"ts": ts, "price": price, "avg": avg, "qty": qty,
               "gross": (price - avg) * qty,
               "hours": (ts - opened_ts) / 3_600_000, "reason": reason}
        (events if reason == "порог" else rule_exits).append(row)
        qty, avg = 0.0, 0.0

    for ts, op, hi, _lo, cl in minutes:
        st = states.get(ts)
        if st == 1 and want == 0:
            want = 1
            enter(op, ts)
        elif st == 0 and want == 1:
            if qty > 0:
                close(op, ts, "правило")
            want = 0

        if want != 1 or qty <= 0:
            continue

        if mode == "level":
            # Заявка стоит всегда: после обратного входа уровень выше, но и он
            # может оказаться под максимумом этой же минуты. Цикл конечен.
            while True:
                target = avg * (1 + threshold_pct / 100)
                if hi < target:
                    break
                close(target, ts, "порог")
                enter(target, ts)
        else:
            # Бот просыпается по сетке и видит только цену на момент опроса.
            if (ts // MINUTE_MS) % poll_min:
                continue
            if cl >= avg * (1 + threshold_pct / 100):
                close(cl * (1 - slip), ts, "порог")
                enter(cl * (1 + slip), ts)

    if qty > 0:
        close(minutes[-1][4], minutes[-1][0], "конец истории")

    gross = sum(e["gross"] for e in events + rule_exits)
    span = (minutes[-1][0] - minutes[0][0]) / 86_400_000 if minutes else 0.0
    return {"events": events, "rule_exits": rule_exits, "fees": fees,
            "gross": gross, "net": gross - fees, "span_days": span}


def line(title: str, r: dict) -> None:
    ev = r["events"]
    months = r["span_days"] / 30.4 if r["span_days"] else 1.0
    if not ev:
        print(f"    {title:<22} ни одного закрытия по порогу")
        return
    money = [e["gross"] for e in ev]
    print(f"    {title:<22} {len(ev):>4} закрытий ({len(ev) / months:>5.1f} в "
          f"месяц), в каждом ${statistics.median(money):>6.2f}  |  "
          f"по порогу {sum(money):+9,.0f} $, "
          f"по правилу {sum(e['gross'] for e in r['rule_exits']):+9,.0f} $, "
          f"комиссии {-r['fees']:+9,.0f} $  =  на руки {r['net']:+9,.0f} $")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--notional", type=float, default=200.0)
    ap.add_argument("--poll", type=int, default=3,
                    help="период опроса в минутах (HYBRID_POLL_SEC/60)")
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    # Тренду нужен разгон на SMA50 по 4h — берём историю заранее.
    start4h = int((time.time() - (args.days + 20) * 86400) * 1000)
    bars4h = fetch(sess, args.symbol, "240", start4h)
    states = core_states(bars4h)

    start1m = int((time.time() - args.days * 86400) * 1000)
    print(f"загружаем минутные свечи за {args.days} дней, это долго…",
          flush=True)
    minutes = fetch_minutes(sess, args.symbol, start1m)
    if len(minutes) < 1000:
        raise SystemExit("минутных баров мало")

    have = (minutes[-1][0] - minutes[0][0]) / 86_400_000
    print(f"\n{args.symbol}: {len(minutes):,} минутных свечей, "
          f"{len(bars4h)} 4-часовых, ставка ${args.notional:,.0f}, "
          f"опрос раз в {args.poll} мин")
    if have < args.days - 2:
        print(f"ВНИМАНИЕ: просили {args.days} дней, биржа отдала {have:.0f} — "
              "глубина минутной истории ограничена. Все цифры ниже — за "
              f"{have:.0f} дней, читать их как более длинное окно нельзя.")
    else:
        print(f"окно {have:.0f} дней")
    print("Обе руки на одних данных: разница только в том, как исполняется "
          "закрытие.\n")

    for th in THRESHOLDS_PCT:
        print(f"  порог +{th:.1f}%")
        base = run(minutes, states, threshold_pct=th, notional=args.notional,
                   mode="level")
        line("заявка на уровне", base)
        for slip in SLIPS_BP:
            r = run(minutes, states, threshold_pct=th, notional=args.notional,
                    mode="poll", poll_min=args.poll, slip_bp=slip)
            tail = "" if slip else "  ← только сетка опроса"
            line(f"бот, слиппедж {slip:.0f} б.п.", r)
            if tail:
                print(f"      {tail.strip()}: закрытий "
                      f"{len(r['events'])} против {len(base['events'])}, "
                      f"на руки {r['net'] - base['net']:+,.0f} $ к модели")
        print()

    print("=" * 78)
    print("Как читать: рука «заявка на уровне» — это цифры канона §17.6. Рука "
          "«как у бота» показывает, что от них останется при реальном способе "
          "исполнения. Если расхождение велико, читать живые фиксации по §17.6 "
          "нельзя — нужна перепроверка ожиданий, а не подкрутка порога.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
