"""Шаги 4-5 плана STRATEGY_HYBRID.md: выбор ядра и выбор сайзинга.

Шаги 1-2 закрыты отрицательно (§14, §15 канона): фиксация не меняет экспозицию
и стоит комиссий, а триггер выбирает моменты не лучше случайного часа. Осталось
слагаемое, которое вообще и приносит матожидание, — само трендовое правило ядра
плюс размер позиции. Этот скрипт их измеряет. Read-only: только публичные kline
и funding, никаких ключей и никакой торговой логики.

─── Правила зафиксированы ДО прогона, каждое со ссылкой ──────────────────────

Подбор окон запрещён (`no-data-fitting.mdc`, `strategy-guard.mdc`), поэтому
берутся только правила с независимым источником:

  1. `4h SMA20/50` — текущее ядро `swing-bot` (Murphy 1999, ch.9). База,
     которую остальные обязаны обогнать.
  2. `D SMA200` — таймингом по 200-дневной/10-месячной средней: Faber 2007,
     «A Quantitative Approach to Tactical Asset Allocation»; ранее
     Brock/Lakonishok/LeBaron 1992 (JF) на 90 годах Dow.
  3. `D SMA50` — более быстрый режимный фильтр, уже прогонялся в
     `scripts/scalp_swing_research.py`.
  4. `D Turtle 55/20` — Donchian-канал System 2, Faith 2007 «Way of the
     Turtle» (Dennis/Eckhardt).
  5. `4h Turtle 20/10` — System 1 на 4h, тот же канон.
  6. `TSMOM 12м` — знак доходности за 12 месяцев, ребаланс раз в месяц:
     Moskowitz/Ooi/Pedersen 2012 (JFE) «Time Series Momentum».
  7. `TSMOM 4н` — знак доходности за 4 недели, ребаланс раз в неделю: в крипте
     импульс живёт на горизонте 1-4 недель (Liu/Tsyvinski 2021, RFS «Risks
     and Returns of Cryptocurrency»).

Протокол исполнения как во всех прошлых скриптах: сигнал на close бара, сделка
на open следующего. Издержка taker 0.055% на ногу
(<https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate>), funding —
фактические ставки из `/v5/market/funding/history`, платит открытая позиция.

─── Как защищаемся от подглядывания ──────────────────────────────────────────

7 правил × 3 символа = 21 испытание, поэтому «нашлось что-то с p<0.05» ничего
не значит (Harvey/Liu 2015, Bailey/Lopez de Prado 2014 о deflated Sharpe).
Правило считается принятым только если выполнено ВСЁ:

  * обгоняет холд по Sharpe на ВСЕХ трёх символах (не на лучшем);
  * превосходство над холдом положительно и в первой, и во второй половине
    периода (IS/OOS по знаку);
  * p парного теста «правило минус холд» ниже порога Бонферрони 0.05/21.

Бенчмарк — холд, а не ноль: ядро существует, чтобы держать инвентарь, поэтому
правило без преимущества над холдом смысла не имеет.

─── Сайзинг (шаг 5) ──────────────────────────────────────────────────────────

Сравниваются на текущем правиле ядра, чтобы не смешивать выбор правила с
выбором размера:

  * `доля equity` — как сейчас (`SWING_POSITION_FRAC`=0.15);
  * `фикс. нотионал` — постоянный размер в долларах;
  * `цель по воле` — нотионал ∝ target/реализованная волатильность
    (Moskowitz/Ooi/Pedersen 2012: позиции масштабируются к постоянной
    ex-ante волатильности; там 40% годовых).

Запуск (kline и funding — публичные, ключи не нужны):
    python3 scripts/hybrid_core_select.py --days 1460
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scalp_swing_research import fetch  # noqa: E402  протокол загрузки баров

TAKER = 0.00055
DAY_MS = 86_400_000
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Текущее ядро: src/horizon_bot, SWING_POSITION_FRAC в docker-compose.yml.
CORE_FRAC = 0.15

# Moskowitz/Ooi/Pedersen 2012: масштабирование к постоянной ex-ante волатильности.
# 40% годовых — уровень из статьи; окно 60 дней — их же ex-ante оценка волы.
VOL_TARGET_ANNUAL = 0.40
VOL_LOOKBACK_D = 60
# Потолок на плечо: в спокойный период формула просит размер тем больше, чем
# ниже вола. Ограничение риска, а не подобранный параметр.
VOL_MAX_MULT = 3.0


# ─── сигналы ────────────────────────────────────────────────────────────────
# Каждая функция возвращает {ts бара исполнения: желаемое состояние 0/1}.
# Сигнал считается по барам до i включительно, исполняется на открытии i+1.


def _sched(bars: list[tuple], want_at: list[int | None]) -> dict[int, int]:
    out: dict[int, int] = {}
    for i, want in enumerate(want_at):
        if want is None or i + 1 >= len(bars):
            continue
        out[bars[i + 1][0]] = want
    return out


def sma_cross(bars: list[tuple], fast: int, slow: int) -> dict[int, int]:
    cl = [b[4] for b in bars]
    want: list[int | None] = []
    for i in range(len(bars)):
        if i + 1 < slow:
            want.append(None)
            continue
        f = statistics.fmean(cl[i + 1 - fast:i + 1])
        s = statistics.fmean(cl[i + 1 - slow:i + 1])
        want.append(1 if f > s else 0)
    return _sched(bars, want)


def sma_price(bars: list[tuple], window: int) -> dict[int, int]:
    cl = [b[4] for b in bars]
    want: list[int | None] = []
    for i in range(len(bars)):
        if i + 1 < window:
            want.append(None)
            continue
        sma = statistics.fmean(cl[i + 1 - window:i + 1])
        want.append(1 if cl[i] > sma else 0)
    return _sched(bars, want)


def donchian(bars: list[tuple], enter: int, exit_: int) -> dict[int, int]:
    hi = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    cl = [b[4] for b in bars]
    want: list[int | None] = []
    state = 0
    for i in range(len(bars)):
        if i < max(enter, exit_):
            want.append(None)
            continue
        if state == 0 and cl[i] > max(hi[i - enter:i]):
            state = 1
        elif state == 1 and cl[i] < min(lo[i - exit_:i]):
            state = 0
        want.append(state)
    return _sched(bars, want)


def tsmom(bars: list[tuple], lookback: int, rebalance: int) -> dict[int, int]:
    """Знак доходности за `lookback` баров, пересмотр раз в `rebalance` баров.

    Между пересмотрами состояние держится — это и есть смысл ребаланса у
    Moskowitz/Ooi/Pedersen: сигнал не пересчитывается каждый бар.
    """
    cl = [b[4] for b in bars]
    want: list[int | None] = []
    state: int | None = None
    for i in range(len(bars)):
        if i < lookback:
            want.append(None)
            continue
        if state is None or i % rebalance == 0:
            state = 1 if cl[i] > cl[i - lookback] else 0
        want.append(state)
    return _sched(bars, want)


# ─── исполнение ─────────────────────────────────────────────────────────────


def fetch_funding(sess, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Фактические ставки funding. Long платит при rate > 0.

    https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
    """
    out: dict[int, float] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_funding_rate_history(
                category="linear", symbol=symbol, startTime=start_ms,
                endTime=end, limit=200)["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            out[ts] = float(r["fundingRate"])
            oldest = min(oldest, ts)
        if oldest <= start_ms or len(rows) < 200:
            break
        end = oldest - 1
    return [(ts, out[ts]) for ts in sorted(out)]


def _funding_per_bar(bars: list[tuple],
                     funding: list[tuple[int, float]]) -> list[float]:
    """Сумма ставок, попавших в интервал каждого бара."""
    per = [0.0] * len(bars)
    if not bars:
        return per
    edges = [b[0] for b in bars]
    j = 0
    for ts, rate in funding:
        while j + 1 < len(edges) and edges[j + 1] <= ts:
            j += 1
        if ts >= edges[0]:
            per[j] += rate
    return per


def _realized_vol(cl: list[float], i: int, bars_per_day: float) -> float | None:
    """Годовая волатильность по логарифмическим доходностям за окно."""
    n = int(VOL_LOOKBACK_D * bars_per_day)
    if i < n + 1:
        return None
    rets = [math.log(cl[k] / cl[k - 1]) for k in range(i - n + 1, i + 1)
            if cl[k - 1] > 0]
    if len(rets) < 2:
        return None
    sd = statistics.stdev(rets)
    return sd * math.sqrt(365 * bars_per_day)


def simulate(bars: list[tuple], sched: dict[int, int],
             funding: list[tuple[int, float]], *, equity0: float = 10_000.0,
             sizing: str = "frac", frac: float = 1.0,
             bars_per_day: float = 6.0) -> dict:
    """Ведёт счёт по барам: доходность позиции минус комиссии и funding.

    Состояние — количество (`qty`), а не нотионал: внутри позиции лот не
    меняется, как у живого ядра, и тогда разрыв между закрытием бара и
    открытием следующего попадает в счёт, а не теряется.

    `sizing`:
      `frac`     — нотионал = frac × текущий счёт (как сейчас у ядра);
      `notional` — постоянный нотионал frac × стартовый счёт;
      `vol`      — нотионал = frac × счёт × цель_волы / реализованная вола
                   (Moskowitz/Ooi/Pedersen 2012), с потолком на плечо.
    """
    cl = [b[4] for b in bars]
    fund = _funding_per_bar(bars, funding)
    equity = equity0
    qty = 0.0
    fees_total = 0.0
    funding_total = 0.0
    legs = 0
    long_bars = 0
    ruined = False
    curve: list[tuple[int, float]] = []

    def target_notional(i: int) -> float:
        if sizing == "notional":
            return frac * equity0
        if sizing == "vol":
            vol = _realized_vol(cl, i, bars_per_day)
            if vol is None or vol <= 0:
                return 0.0
            scaled = frac * VOL_TARGET_ANNUAL / vol
            return equity * min(frac * VOL_MAX_MULT, scaled)
        return frac * equity

    for i, bar in enumerate(bars):
        ts, op = bar[0], bar[1]
        entry_ref: float | None = None
        new_want = sched.get(ts)

        if ruined:
            curve.append((ts, 0.0))
            continue

        if new_want == 1 and qty == 0.0:
            notional = target_notional(i)
            if notional > 0 and op > 0:
                qty = notional / op
                fee = notional * TAKER
                equity -= fee
                fees_total += fee
                legs += 1
                entry_ref = op
        elif new_want == 0 and qty > 0.0:
            fee = qty * op * TAKER
            equity -= fee
            fees_total += fee
            legs += 1
            equity += qty * (op - cl[i - 1])  # разрыв до выхода тоже наш
            qty = 0.0

        if qty > 0.0:
            ref = entry_ref if entry_ref is not None else cl[i - 1]
            equity += qty * (cl[i] - ref)
            pay = fund[i] * qty * cl[i]
            equity -= pay
            funding_total += pay
            long_bars += 1

        # Счёт не бывает отрицательным: на нуле биржа ликвидирует позицию, и
        # дальше торговать нечем. Без этого длинный холд по падающему символу
        # уходит в минус (цена −80% плюс годы funding) и все метрики после
        # этого — деление на отрицательный счёт, то есть мусор.
        if equity <= 0.0:
            ruined = True
            equity = 0.0
            qty = 0.0
        curve.append((ts, equity))

    if qty > 0.0:
        fee = qty * cl[-1] * TAKER
        equity -= fee
        fees_total += fee
        legs += 1
        if curve:
            curve[-1] = (curve[-1][0], equity)

    return {
        "equity0": equity0, "equity": equity, "curve": curve,
        "fees": fees_total, "funding": funding_total, "legs": legs,
        "time_in": long_bars / len(bars) if bars else 0.0,
        "ruined": ruined,
    }


def hold_schedule(bars: list[tuple], warmup: int) -> dict[int, int]:
    """Холд с того же бара, с которого правило имеет право на сигнал."""
    return {b[0]: 1 for b in bars[warmup:]}


# ─── метрики ────────────────────────────────────────────────────────────────


def daily_returns(curve: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Доходности по календарным дням: счёт на конец дня к концу прошлого."""
    by_day: dict[int, float] = {}
    for ts, eq in curve:
        by_day[ts // DAY_MS] = eq
    days = sorted(by_day)
    out = []
    for a, b in zip(days, days[1:]):
        prev = by_day[a]
        if prev > 0:
            out.append((b, by_day[b] / prev - 1))
    return out


def _ruin_note(rule: dict, hold: dict) -> str:
    """Пометка, если одна из ветвей разорилась: сравнение тогда бессмысленно."""
    if hold.get("ruined") and rule.get("ruined"):
        return "  ОБЕ ВЕТВИ РАЗОРЕНЫ"
    if hold.get("ruined"):
        return "  ХОЛД РАЗОРЁН (обгон тривиален)"
    if rule.get("ruined"):
        return "  ПРАВИЛО РАЗОРЕНО"
    return ""


def _sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    sd = statistics.stdev(rets)
    if sd == 0:
        return 0.0
    return statistics.fmean(rets) / sd * math.sqrt(365)


def _max_dd(curve: list[tuple[int, float]]) -> float:
    peak = -math.inf
    dd = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = min(dd, eq / peak - 1)
    return dd * 100


def _norm_p(t: float) -> float:
    """Двусторонний p по нормальному приближению."""
    return math.erfc(abs(t) / math.sqrt(2))


def compare(rule: dict, hold: dict) -> dict:
    """Правило против холда: разница дневных доходностей, парный тест."""
    r_daily = dict(daily_returns(rule["curve"]))
    h_daily = dict(daily_returns(hold["curve"]))
    common = sorted(set(r_daily) & set(h_daily))
    diff = [r_daily[d] - h_daily[d] for d in common]
    n = len(diff)
    mean = statistics.fmean(diff) if diff else 0.0
    se = (statistics.stdev(diff) / math.sqrt(n)) if n > 1 else 0.0
    t = mean / se if se > 0 else 0.0
    mid = n // 2
    return {
        "ret": (rule["equity"] / rule["equity0"] - 1) * 100,
        "hold_ret": (hold["equity"] / hold["equity0"] - 1) * 100,
        "sharpe": _sharpe([r_daily[d] for d in common]),
        "hold_sharpe": _sharpe([h_daily[d] for d in common]),
        "dd": _max_dd(rule["curve"]), "hold_dd": _max_dd(hold["curve"]),
        "time_in": rule["time_in"] * 100, "legs": rule["legs"],
        "fees": rule["fees"], "funding": rule["funding"],
        "edge_bp": mean * 10_000, "t": t, "p": _norm_p(t), "n_days": n,
        "is_edge": statistics.fmean(diff[:mid]) * 10_000 if mid else 0.0,
        "oos_edge": statistics.fmean(diff[mid:]) * 10_000 if mid else 0.0,
    }


# ─── прогон ─────────────────────────────────────────────────────────────────


N_RULES = 7  # список ниже; вынесено для порога Бонферрони


def rules_for(b4: list[tuple], bd: list[tuple]) -> list[tuple]:
    """(имя, бары, расписание, разогрев, баров в сутках). Фиксировано заранее."""
    return [
        ("4h SMA20/50 (сейчас)", b4, sma_cross(b4, 20, 50), 50, 6.0),
        ("D SMA200 (Faber)", bd, sma_price(bd, 200), 200, 1.0),
        ("D SMA50", bd, sma_price(bd, 50), 50, 1.0),
        ("D Turtle 55/20", bd, donchian(bd, 55, 20), 55, 1.0),
        ("4h Turtle 20/10", b4, donchian(b4, 20, 10), 20, 6.0),
        ("TSMOM 12м (MOP)", bd, tsmom(bd, 365, 30), 365, 1.0),
        ("TSMOM 4н (L&T)", bd, tsmom(bd, 28, 7), 28, 1.0),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    start = int((time.time() - args.days * 86400) * 1000)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    n_trials = N_RULES * len(symbols)
    bonf = 0.05 / n_trials

    print(f"Шаги 4-5: выбор ядра и сайзинга | {args.days}д | taker "
          f"{TAKER*100:.3f}%/нога + фактический funding")
    print(f"Бенчмарк — холд. Порог Бонферрони для {n_trials} испытаний "
          f"({N_RULES} правил × {len(symbols)} символа): p<{bonf:.4f}. "
          "Нотионал 1x, чтобы сравнивать сами правила.\n")
    per_rule: dict[str, list[dict]] = {}

    for sym in symbols:
        b4 = fetch(sess, sym, "240", start)
        bd = fetch(sess, sym, "D", start)
        fnd = fetch_funding(sess, sym, start)
        if len(bd) < 400:
            print(f"=== {sym}: истории мало ({len(bd)} дней), пропуск ===\n")
            continue
        print(f"=== {sym} — {len(bd)} дней, {len(b4)} 4h-баров ===")
        for name, bars, sched, warmup, bpd in rules_for(b4, bd):
            if not sched:
                print(f"  {name:<22} расписание пустое, пропуск")
                continue
            rule = simulate(bars, sched, fnd, bars_per_day=bpd)
            hold = simulate(bars, hold_schedule(bars, warmup), fnd,
                            bars_per_day=bpd)
            c = compare(rule, hold)
            note = _ruin_note(rule, hold)
            per_rule.setdefault(name, []).append(
                {**c, "sym": sym, "valid": not note})
            print(f"  {name:<22} итог {c['ret']:+7.1f}% (холд "
                  f"{c['hold_ret']:+7.1f}%)  Sharpe {c['sharpe']:+.2f} "
                  f"(холд {c['hold_sharpe']:+.2f})  DD {c['dd']:.1f}% "
                  f"(холд {c['hold_dd']:.1f}%)  в рынке {c['time_in']:.0f}%  "
                  f"ног {c['legs']:<4} перевес {c['edge_bp']:+.1f}бп/день "
                  f"p={c['p']:.3f}  IS/OOS {c['is_edge']:+.1f}/"
                  f"{c['oos_edge']:+.1f}бп" + note)
        print()

    print("=" * 100)
    print("ВЕРДИКТ по правилам (нужно ВСЁ: Sharpe > холда на всех символах, "
          f"перевес > 0 в обеих половинах, p < {bonf:.4f})")
    print("Символы, где какая-то ветвь разорилась, из вердикта исключены: "
          "Sharpe и просадка после нуля не определены.")
    accepted = []
    for name, all_rows in per_rule.items():
        if len(all_rows) < len(symbols):
            print(f"  {name:<22} нет данных по всем символам")
            continue
        rows = [r for r in all_rows if r["valid"]]
        if not rows:
            print(f"  {name:<22} нет ни одного корректного сравнения")
            continue
        skipped = len(all_rows) - len(rows)
        all_sharpe = all(r["sharpe"] > r["hold_sharpe"] for r in rows)
        both_halves = all(r["is_edge"] > 0 and r["oos_edge"] > 0 for r in rows)
        sig = all(r["p"] < bonf for r in rows)
        why = []
        if not all_sharpe:
            bad = [r["sym"] for r in rows if r["sharpe"] <= r["hold_sharpe"]]
            why.append("Sharpe ниже холда: " + ",".join(bad))
        if not both_halves:
            why.append("перевес меняет знак между половинами")
        if not sig:
            why.append("нет значимости после Бонферрони")
        ok = all_sharpe and both_halves and sig
        accepted.append(ok)
        tail = f" [учтено {len(rows)} симв., исключено {skipped}]" if skipped \
            else ""
        print(f"  {name:<22} {'ПРИНЯТО' if ok else 'закрыто'}"
              + ("" if ok else " — " + "; ".join(why)) + tail)

    print()
    if not any(accepted):
        print("Ни одно каноническое трендовое правило не обогнало холд на всех "
              "символах. Шаг 4 не даёт ядра, пригодного для контура.")
    else:
        print("Есть кандидат в ядро — дальше OOS на свежих данных, до тех пор "
              "торговать по нему нельзя (гейты §8).")

    print("\n" + "=" * 100)
    print("ШАГ 5 — сайзинг на текущем правиле ядра (4h SMA20/50), "
          f"доля {CORE_FRAC:.0%} против фикс. нотионала и цели по воле "
          f"{VOL_TARGET_ANNUAL:.0%}")
    for sym in symbols:
        b4 = fetch(sess, sym, "240", start)
        fnd = fetch_funding(sess, sym, start)
        if len(b4) < 400:
            continue
        sched = sma_cross(b4, 20, 50)
        print(f"\n  {sym}:")
        for label, kw in (
            ("доля equity", {"sizing": "frac", "frac": CORE_FRAC}),
            ("фикс. нотионал", {"sizing": "notional", "frac": CORE_FRAC}),
            ("цель по воле", {"sizing": "vol", "frac": CORE_FRAC}),
        ):
            r = simulate(b4, sched, fnd, bars_per_day=6.0, **kw)
            rets = [x for _, x in daily_returns(r["curve"])]
            print(f"    {label:<16} итог {(r['equity']/r['equity0']-1)*100:+6.2f}%  "
                  f"Sharpe {_sharpe(rets):+.2f}  DD {_max_dd(r['curve']):.2f}%  "
                  f"комиссии ${r['fees']:,.0f}  funding ${r['funding']:,.0f}")
    print("\nСайзинг меняет масштаб и риск, но не знак: если правило не бьёт "
          "холд, ни один размер этого не исправит.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
