"""Исследование: перекрёстный funding-carry на бессрочных Bybit + TS-момент.

Зачем этот скрипт
─────────────────
Замеры собственных данных (BUILDLOG_SCALP.md, 2026-08-18) закрыли целый класс
идей: по 23 популяциям контрфактов и ~11k разрешённых наблюдений отношение
MFE/MAE держится на 0.98–1.21, то есть цена уходит в нашу пользу ровно настолько
же, насколько против — подпись случайного блуждания. Все шесть живых стратегий
в минусе (2209 сделок, −$3838), а комиссия при стопе 0.31% цены стоит 0.33R.
Вывод: направленное предсказание на горизонте 1–3 минут не окупает издержки.

Тот же вывод независимо получен в литературе:
- `retail-crypto-alpha` (Mykola-Quant, 2026): order flow, ликвидации+OI, OHLCV,
  spot-perp CVD, mean-reversion фандинга, ORB, интрадей-момент, календарь на 5
  активах → при round-trip ~0.13% ни один сигнал не даёт края; «предсказуемый
  ход примерно на порядок меньше стоимости его извлечения».
- «Can Funding Rate Predict Price Change?»: корреляция ставки фандинга с
  последующей ценой около нуля, R²≈0, p велико → фандинг как ДИРЕКЦИОННЫЙ
  предиктор falsified. Там же указано, что данные полезны иначе — «cross-
  sectionally over a universe of multiple assets», как stat-arb альфа.
- Chen/Ma/Nie и обзоры funding-arbitrage: delta-neutral сбор фандинга даёт
  8–25% APR, НО требует spot-ноги и чувствителен к издержкам; MDPI
  (10.3390/math14020346): лишь 40% лучших кросс-биржевых возможностей
  прибыльны после costs, forced exit в 95% случаев.

Отсюда гипотеза, которую проверяет скрипт. Мы НЕ предсказываем направление и НЕ
берём одну ногу. Мы собираем сам денежный поток фандинга перекрёстно:
  * фандинг > 0 → лонги платят шортам ⇒ чтобы ПОЛУЧАТЬ, надо быть в шорте;
  * фандинг < 0 → шорты платят лонгам ⇒ чтобы ПОЛУЧАТЬ, надо быть в лонге.
Значит корзина «лонг самых отрицательных ставок + шорт самых положительных»
получает фандинг обеими ногами и при равных нотионалах примерно нейтральна к
рынку. Арифметика, из которой гипотеза вообще имеет смысл: фандинг на Bybit
платится каждые 8 часов, поэтому удержание сутки собирает три выплаты против
одного round-trip. При спреде ставок 0.1% за интервал это 0.3% дохода против
0.11% taker-издержки.

Чего скрипт НЕ делает
─────────────────────
Не подбирает параметры под результат (no-data-fitting.mdc). Сетка задана
заранее и печатается ЦЕЛИКОМ, включая убыточные ячейки. Выборка делится на
in-sample и out-of-sample по времени; вывод принимается только если знак
совпал в обеих половинах. Число испытаний печатается, чтобы порог значимости
дефлировался на него (Bailey/Lopez de Prado, Deflated Sharpe Ratio).
Бэктест обязан иметь право вернуть «края нет» — это valid результат.

Осторожно с look-ahead: ставка фандинга известна ДО расчёта (Bybit публикует
predicted rate), но здесь используется только УЖЕ ОПЛАЧЕННАЯ история, а решение
принимается на закрытии интервала t и применяется к периоду t+1.

Источники API (api-docs.mdc):
- ставки: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- бары:   https://bybit-exchange.github.io/docs/v5/market/kline
- тарифы: https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from collections import defaultdict

# Стандартная сетка Bybit linear perpetual, non-VIP.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
TAKER_FEE = 0.00055
MAKER_FEE = 0.0002

FUNDING_INTERVAL_H = 8


def _session(demo: bool):
    from pybit.unified_trading import HTTP
    return HTTP(demo=demo)


def fetch_liquid_symbols(sess, min_turnover_usd: float, limit: int) -> list[str]:
    """Ликвидные USDT-бессрочные, отсортированные по обороту за 24ч."""
    rows = sess.get_tickers(category="linear")["result"]["list"]
    out = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            turnover = float(r.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if turnover < min_turnover_usd:
            continue
        out.append((turnover, sym))
    out.sort(reverse=True)
    return [s for _, s in out[:limit]]


def fetch_funding(sess, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Полная история ставок с пагинацией назад по времени.

    API отдаёт максимум 200 записей и идёт от свежих к старым, поэтому
    сдвигаем endTime, пока не дойдём до start_ms.
    """
    out: dict[int, float] = {}
    end_ms = int(time.time() * 1000)
    while True:
        try:
            resp = sess.get_funding_rate_history(
                category="linear", symbol=symbol,
                startTime=start_ms, endTime=end_ms, limit=200)
        except Exception:
            break
        rows = resp.get("result", {}).get("list") or []
        if not rows:
            break
        oldest = end_ms
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            out[ts] = float(r["fundingRate"])
            oldest = min(oldest, ts)
        if len(rows) < 200 or oldest <= start_ms:
            break
        end_ms = oldest - 1
    return sorted(out.items())


def fetch_klines(sess, symbol: str, interval: str, start_ms: int
                 ) -> list[tuple[int, float]]:
    """Закрытия бара с пагинацией.

    Валидные интервалы Bybit: 1,3,5,15,30,60,120,240,360,720,D,W,M
    (<https://bybit-exchange.github.io/docs/v5/market/kline>). Значения вроде
    '480' (8ч) API молча возвращает пустым списком — поэтому сетку 8ч мы
    собираем из часовых баров, а не просим у биржи напрямую.
    """
    out: dict[int, float] = {}
    end_ms = int(time.time() * 1000)
    while True:
        try:
            resp = sess.get_kline(category="linear", symbol=symbol,
                                  interval=interval, start=start_ms,
                                  end=end_ms, limit=1000)
        except Exception:
            break
        rows = resp.get("result", {}).get("list") or []
        if not rows:
            break
        oldest = end_ms
        for r in rows:
            ts = int(r[0])
            out[ts] = float(r[4])
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end_ms = oldest - 1
    return sorted(out.items())


STEP_MS = FUNDING_INTERVAL_H * 3600 * 1000


def build_panel(sess, symbols: list[str], days: int, verbose: bool
                ) -> tuple[dict, dict, list[int]]:
    """Панель на решающей сетке 8ч (00:00/08:00/16:00 UTC).

    Период фандинга у Bybit НЕ универсален: у мажоров 8ч, у части альтов 4ч или
    1ч (`get_funding_interval`). Поэтому не сопоставляем «ставку к метке», а
    СУММИРУЕМ все выплаты, попавшие в 8-часовое окно — у символа с 4ч-периодом
    в окно попадут две выплаты, и это корректно отражает его денежный поток.
    """
    start_ms = int((time.time() - days * 86400) * 1000)
    # решающая сетка: ровные 8ч от начала окна
    grid_start = (start_ms // STEP_MS + 1) * STEP_MS
    grid = list(range(grid_start, int(time.time() * 1000), STEP_MS))

    funding: dict[str, dict[int, float]] = {}
    price: dict[str, dict[int, float]] = {}
    for i, sym in enumerate(symbols, 1):
        fr = fetch_funding(sess, sym, start_ms)
        kl = fetch_klines(sess, sym, "60", start_ms)
        if len(fr) < 20 or len(kl) < 200:
            if verbose:
                print(f"    пропуск {sym}: ставок={len(fr)} баров={len(kl)}")
            continue
        bars = dict(kl)
        # цена ровно на метке сетки (часовой бар всегда есть на 00/08/16)
        price[sym] = {ts: bars[ts] for ts in grid if ts in bars}
        # выплаты, сгруппированные в окно (ts, ts+8ч]
        acc: dict[int, float] = defaultdict(float)
        for ts_pay, rate in fr:
            slot = ((ts_pay - grid_start) // STEP_MS) * STEP_MS + grid_start
            if ts_pay > slot:
                slot += STEP_MS
            acc[slot] += rate
        funding[sym] = dict(acc)
        if verbose:
            n_int = len({t // 3600000 % 24 for t, _ in fr})
            print(f"    [{i}/{len(symbols)}] {sym}: ставок={len(fr)} "
                  f"баров={len(kl)} цен_на_сетке={len(price[sym])} "
                  f"часов_выплат={n_int}")
    covered = [ts for ts in grid
               if sum(1 for s in price if ts in price[s]) >= 2]
    return funding, price, covered


def _nearest_rate(rates: dict[int, float], ts: int, tol_ms: int) -> float | None:
    """Суммарная выплата, отнесённая к окну, закрывающемуся на ``ts``."""
    return rates.get(ts)


def backtest(funding: dict, price: dict, grid_ts: list[int],
             n_legs: int, lookback: int, hold: int, fee: float
             ) -> dict:
    """Перекрёстный carry: лонг самых отрицательных ставок, шорт самых
    положительных. Возврат периодов равен доходу фандинга + движение цены
    − издержки на смену состава.
    """
    tol = 30 * 60 * 1000
    per_period: list[float] = []
    prev_pos: dict[str, float] = {}
    turnover_total = 0.0

    # решение принимаем на метке i, держим hold периодов до i+hold
    i = lookback
    while i + hold < len(grid_ts):
        ts = grid_ts[i]
        # сигнал: средняя ОПЛАЧЕННАЯ ставка за lookback интервалов (без t+1)
        scores: list[tuple[float, str]] = []
        for sym in funding:
            if ts not in price[sym]:
                continue
            hist = []
            for j in range(i - lookback + 1, i + 1):
                r = _nearest_rate(funding[sym], grid_ts[j], tol)
                if r is not None:
                    hist.append(r)
            if len(hist) < max(2, lookback // 2):
                continue
            scores.append((statistics.mean(hist), sym))
        if len(scores) < 2 * n_legs:
            i += hold
            continue
        scores.sort()
        longs = [s for _, s in scores[:n_legs]]     # самые отрицательные ставки
        shorts = [s for _, s in scores[-n_legs:]]   # самые положительные

        pos = {s: +1.0 / n_legs for s in longs}
        pos.update({s: -1.0 / n_legs for s in shorts})

        # издержки: только на изменение веса (вход/выход/переворот)
        syms = set(pos) | set(prev_pos)
        turn = sum(abs(pos.get(s, 0.0) - prev_pos.get(s, 0.0)) for s in syms)
        turnover_total += turn
        cost = turn * fee

        # доход за hold интервалов
        ret = 0.0
        ok = True
        for s, w in pos.items():
            p0 = price[s].get(ts)
            p1 = price[s].get(grid_ts[i + hold])
            if not p0 or not p1:
                ok = False
                break
            ret += w * (p1 - p0) / p0
            # фандинг: получаем −rate * позиция за каждый интервал удержания
            for j in range(i + 1, i + hold + 1):
                r = _nearest_rate(funding[s], grid_ts[j], tol)
                if r is not None:
                    ret += w * (-r)
        if not ok:
            i += hold
            continue

        per_period.append(ret - cost)
        prev_pos = pos
        i += hold

    n = len(per_period)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(per_period)
    sd = statistics.stdev(per_period) if n > 1 else 0.0
    periods_per_year = (365 * 24 / FUNDING_INTERVAL_H) / hold
    sharpe = (mean / sd * math.sqrt(periods_per_year)) if sd else 0.0
    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in per_period:
        equity *= (1 + r)
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    return {
        "n": n,
        "mean_pct": mean * 100,
        "total_pct": (equity - 1) * 100,
        "apr_pct": ((equity ** (periods_per_year / n) - 1) * 100
                    if n > 0 and equity > 0 else float("nan")),
        "sharpe": sharpe,
        "mdd_pct": mdd * 100,
        "ci_lo_pct": (mean - 1.96 * se) * 100,
        "ci_hi_pct": (mean + 1.96 * se) * 100,
        "turnover": turnover_total / n,
        "series": per_period,
    }


def tsmom_backtest(price: dict, grid_ts: list[int], lookback: int, hold: int,
                   fee: float) -> dict:
    """Контроль: time-series момент на тех же данных и тех же издержках.

    Research: SSRN 4675565 «Time-Series and Cross-Sectional Momentum in the
    Cryptocurrency Market» — TS-момент сильнее кросс-секционного, эффект
    сосредоточен в winners, losers часто отскакивают. Здесь нужен как
    точка сравнения: если carry не лучше момента, гипотеза не нужна.
    """
    per_period: list[float] = []
    prev_pos: dict[str, float] = {}
    i = lookback
    while i + hold < len(grid_ts):
        ts, syms = grid_ts[i], []
        for s in price:
            p0 = price[s].get(grid_ts[i - lookback])
            p1 = price[s].get(ts)
            if p0 and p1:
                syms.append((1.0 if p1 > p0 else -1.0, s))
        if not syms:
            i += hold
            continue
        w = 1.0 / len(syms)
        pos = {s: sgn * w for sgn, s in syms}
        allsym = set(pos) | set(prev_pos)
        turn = sum(abs(pos.get(s, 0.0) - prev_pos.get(s, 0.0)) for s in allsym)
        ret = -turn * fee
        for s, wt in pos.items():
            p0 = price[s].get(ts)
            p1 = price[s].get(grid_ts[i + hold])
            if p0 and p1:
                ret += wt * (p1 - p0) / p0
        per_period.append(ret)
        prev_pos = pos
        i += hold
    n = len(per_period)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(per_period)
    sd = statistics.stdev(per_period) if n > 1 else 0.0
    ppy = (365 * 24 / FUNDING_INTERVAL_H) / hold
    se = sd / math.sqrt(n)
    eq = 1.0
    for r in per_period:
        eq *= (1 + r)
    return {"n": n, "mean_pct": mean * 100, "total_pct": (eq - 1) * 100,
            "sharpe": (mean / sd * math.sqrt(ppy)) if sd else 0.0,
            "ci_lo_pct": (mean - 1.96 * se) * 100,
            "ci_hi_pct": (mean + 1.96 * se) * 100}


def fetch_spot_symbols(sess) -> set[str]:
    rows = sess.get_tickers(category="spot")["result"]["list"]
    return {r["symbol"] for r in rows if r.get("symbol", "").endswith("USDT")}


def build_spot_prices(sess, symbols: list[str], days: int, grid: list[int],
                      verbose: bool) -> dict:
    """Закрытия spot на решающей сетке (для delta-neutral ноги)."""
    start_ms = int((time.time() - days * 86400) * 1000)
    out: dict[str, dict[int, float]] = {}
    for sym in symbols:
        rows: dict[int, float] = {}
        end_ms = int(time.time() * 1000)
        while True:
            try:
                resp = sess.get_kline(category="spot", symbol=sym,
                                      interval="60", start=start_ms,
                                      end=end_ms, limit=1000)
            except Exception:
                break
            lst = resp.get("result", {}).get("list") or []
            if not lst:
                break
            oldest = end_ms
            for r in lst:
                ts = int(r[0])
                rows[ts] = float(r[4])
                oldest = min(oldest, ts)
            if len(lst) < 1000 or oldest <= start_ms:
                break
            end_ms = oldest - 1
        got = {ts: rows[ts] for ts in grid if ts in rows}
        if len(got) >= 200:
            out[sym] = got
        if verbose:
            print(f"    spot {sym}: баров_на_сетке={len(got)}")
    return out


def carry_neutral_backtest(funding: dict, perp: dict, spot: dict,
                           grid_ts: list[int], n_legs: int, lookback: int,
                           hold: int, fee_perp: float, fee_spot: float,
                           min_rate: float) -> dict:
    """Delta-neutral funding carry: LONG spot + SHORT perp равными нотионалами.

    Каноничная версия из литературы (Kraken/ArbitrageScanner/Chen-Ma-Nie):
    ценовой риск гасится между ногами, доходом остаётся сам фандинг. Шорт
    бессрочного ПОЛУЧАЕТ выплату, когда ставка положительна, поэтому отбираем
    символы с устойчиво ПОЛОЖИТЕЛЬНОЙ ставкой (в отличие от секции A, где
    perp-only лонг ловил падающие ножи).

    P&L периода = Σ ставок (получены шортом) + (доходность spot − доходность
    perp) [базис] − издержки на смену состава по обеим ногам.
    """
    per_period: list[float] = []
    prev: dict[str, float] = {}
    turnover_total = 0.0
    i = lookback
    while i + hold < len(grid_ts):
        ts = grid_ts[i]
        cand: list[tuple[float, str]] = []
        for s in funding:
            if s not in spot or ts not in spot[s] or ts not in perp.get(s, {}):
                continue
            hist = [funding[s][grid_ts[j]]
                    for j in range(i - lookback + 1, i + 1)
                    if grid_ts[j] in funding[s]]
            if len(hist) < max(2, lookback // 2):
                continue
            m = statistics.mean(hist)
            if m >= min_rate:
                cand.append((m, s))
        if not cand:
            # нет подходящих ставок — сидим в кэше, но платим за выход
            turn = sum(abs(prev.get(s, 0.0)) for s in prev)
            if turn:
                turnover_total += turn
                per_period.append(-turn * (fee_perp + fee_spot))
                prev = {}
            i += hold
            continue
        cand.sort(reverse=True)
        chosen = [s for _, s in cand[:n_legs]]
        w = 1.0 / len(chosen)
        pos = {s: w for s in chosen}
        allsym = set(pos) | set(prev)
        turn = sum(abs(pos.get(s, 0.0) - prev.get(s, 0.0)) for s in allsym)
        turnover_total += turn
        ret = -turn * (fee_perp + fee_spot)
        ok = True
        for s, wt in pos.items():
            sp0, sp1 = spot[s].get(ts), spot[s].get(grid_ts[i + hold])
            pp0, pp1 = perp[s].get(ts), perp[s].get(grid_ts[i + hold])
            if not (sp0 and sp1 and pp0 and pp1):
                ok = False
                break
            ret += wt * ((sp1 - sp0) / sp0 - (pp1 - pp0) / pp0)
            for j in range(i + 1, i + hold + 1):
                r = funding[s].get(grid_ts[j])
                if r is not None:
                    ret += wt * r
        if not ok:
            i += hold
            continue
        per_period.append(ret)
        prev = pos
        i += hold

    n = len(per_period)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(per_period)
    sd = statistics.stdev(per_period) if n > 1 else 0.0
    ppy = (365 * 24 / FUNDING_INTERVAL_H) / hold
    se = sd / math.sqrt(n)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in per_period:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {"n": n, "mean_pct": mean * 100, "total_pct": (eq - 1) * 100,
            "apr_pct": (eq ** (ppy / n) - 1) * 100 if eq > 0 else float("nan"),
            "sharpe": (mean / sd * math.sqrt(ppy)) if sd else 0.0,
            "mdd_pct": mdd * 100, "turnover": turnover_total / n,
            "ci_lo_pct": (mean - 1.96 * se) * 100,
            "ci_hi_pct": (mean + 1.96 * se) * 100}


def carry_persistent_backtest(funding: dict, perp: dict, spot: dict,
                              grid_ts: list[int], n_legs: int, lookback: int,
                              enter_rate: float, exit_rate: float,
                              fee_rt: float) -> dict:
    """Delta-neutral carry БЕЗ периодической ротации: держим, пока платят.

    Секция C показала, что убыток съедается оборотом: при обороте ~1.0 за
    период round-trip по двум ногам стоит 2*taker=0.11%, а фандинг мажоров
    всего 0.005–0.008% за 8ч. Литература предписывает ровно обратное
    поведение — «Exit when the funding rate drops below your break-even cost
    threshold» (Kraken), «focusing on periods when funding is consistently
    elevated rather than chasing single-interval spikes». Здесь состояние
    позиции меняется только по порогу ставки, поэтому оборот падает на
    порядок.

    Издержка берётся на КАЖДОЕ открытие/закрытие пары ног, доход — фандинг
    плюс дрейф базиса. Решение на метке t применяется к периоду t+1
    (без look-ahead).
    """
    held: dict[str, bool] = {}
    per_period: list[float] = []
    n_entries = 0
    for i in range(lookback, len(grid_ts) - 1):
        ts, nxt = grid_ts[i], grid_ts[i + 1]
        ret = 0.0
        # оценка ставок и решение о составе
        scored: list[tuple[float, str]] = []
        for s in funding:
            if s not in spot or ts not in spot[s] or ts not in perp.get(s, {}):
                continue
            hist = [funding[s][grid_ts[j]]
                    for j in range(i - lookback + 1, i + 1)
                    if grid_ts[j] in funding[s]]
            if len(hist) < max(2, lookback // 2):
                continue
            scored.append((statistics.mean(hist), s))
        scored.sort(reverse=True)
        want: set[str] = set()
        for m, s in scored:
            if held.get(s):
                if m >= exit_rate:
                    want.add(s)
            elif m >= enter_rate and len(want) < n_legs:
                want.add(s)
        # добираем свободные слоты
        for m, s in scored:
            if len(want) >= n_legs:
                break
            if s not in want and m >= enter_rate:
                want.add(s)

        opened = want - {s for s, v in held.items() if v}
        closed = {s for s, v in held.items() if v} - want
        n_entries += len(opened)
        w = 1.0 / n_legs
        ret -= (len(opened) + len(closed)) * w * fee_rt

        for s in want:
            sp0, sp1 = spot[s].get(ts), spot[s].get(nxt)
            pp0, pp1 = perp[s].get(ts), perp[s].get(nxt)
            if not (sp0 and sp1 and pp0 and pp1):
                continue
            ret += w * ((sp1 - sp0) / sp0 - (pp1 - pp0) / pp0)
            r = funding[s].get(nxt)
            if r is not None:
                ret += w * r
        held = {s: True for s in want}
        per_period.append(ret)

    n = len(per_period)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(per_period)
    sd = statistics.stdev(per_period) if n > 1 else 0.0
    ppy = 365 * 24 / FUNDING_INTERVAL_H
    se = sd / math.sqrt(n)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in per_period:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {"n": n, "mean_pct": mean * 100, "total_pct": (eq - 1) * 100,
            "apr_pct": (eq ** (ppy / n) - 1) * 100 if eq > 0 else float("nan"),
            "sharpe": (mean / sd * math.sqrt(ppy)) if sd else 0.0,
            "mdd_pct": mdd * 100, "entries": n_entries,
            "ci_lo_pct": (mean - 1.96 * se) * 100,
            "ci_hi_pct": (mean + 1.96 * se) * 100}


def split_halves(funding, price, grid_ts):
    mid = len(grid_ts) // 2
    return grid_ts[:mid], grid_ts[mid:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--min-turnover", type=float, default=20_000_000)
    ap.add_argument("--legs", type=int, default=5)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    sess = _session(args.demo)
    print(f"универсум: USDT-бессрочные, оборот24ч ≥ ${args.min_turnover:,.0f}, "
          f"топ {args.symbols}; история {args.days} дней; "
          f"сетка {FUNDING_INTERVAL_H}ч")
    syms = fetch_liquid_symbols(sess, args.min_turnover, args.symbols)
    print(f"отобрано символов: {len(syms)}")
    print("загрузка ставок и баров (пагинация)…")
    funding, price, grid = build_panel(sess, syms, args.days, args.verbose)
    perp_price = price
    print(f"панель: символов={len(funding)} меток={len(grid)}")
    if len(grid) < 60 or len(funding) < 2 * args.legs:
        print("данных мало — вывод делать нельзя")
        return 1
    t0 = time.strftime('%Y-%m-%d', time.gmtime(grid[0] / 1000))
    t1 = time.strftime('%Y-%m-%d', time.gmtime(grid[-1] / 1000))
    print(f"период: {t0} .. {t1}")

    # ── сетка задана ЗАРАНЕЕ и печатается целиком ────────────────────────────
    lookbacks = [1, 3, 9]      # интервалов по 8ч (8ч / 1сут / 3сут)
    holds = [1, 3, 9]
    fees = [("taker", 2 * TAKER_FEE), ("maker", 2 * MAKER_FEE)]

    trials = 0
    print("\n" + "=" * 100)
    print("A. ПЕРЕКРЁСТНЫЙ FUNDING-CARRY (лонг отрицательных ставок / "
          "шорт положительных)")
    print("=" * 100)
    ih, oh = split_halves(funding, price, grid)
    for fee_name, fee in fees:
        print(f"\n--- издержка {fee_name} round-trip "
              f"{fee * 100:.3f}% на единицу оборота ---")
        print(f"{'lb':>3}{'hold':>6}{'n':>6}{'ср.%':>9}{'итог%':>9}"
              f"{'APR%':>9}{'Sharpe':>8}{'проc.%':>8}"
              f"{'95% CI периода':>24}{'обор':>7}  IS/OOS знак")
        for lb in lookbacks:
            for hold in holds:
                trials += 1
                full = backtest(funding, price, grid, args.legs, lb, hold, fee)
                if full.get("n", 0) < 5:
                    print(f"{lb:>3}{hold:>6}{full.get('n', 0):>6}   мало данных")
                    continue
                a = backtest(funding, price, ih, args.legs, lb, hold, fee)
                b = backtest(funding, price, oh, args.legs, lb, hold, fee)
                sa = a.get("mean_pct", 0)
                sb = b.get("mean_pct", 0)
                agree = ("ДА" if (sa > 0) == (sb > 0) and a.get("n", 0) >= 5
                         and b.get("n", 0) >= 5 else "нет")
                ci = f"[{full['ci_lo_pct']:+.4f}; {full['ci_hi_pct']:+.4f}]"
                print(f"{lb:>3}{hold:>6}{full['n']:>6}"
                      f"{full['mean_pct']:>9.4f}{full['total_pct']:>9.2f}"
                      f"{full['apr_pct']:>9.1f}{full['sharpe']:>8.2f}"
                      f"{full['mdd_pct']:>8.1f}{ci:>24}"
                      f"{full['turnover']:>7.2f}  {agree}"
                      f" ({sa:+.4f}/{sb:+.4f})")

    print("\n" + "=" * 100)
    print("B. КОНТРОЛЬ: TIME-SERIES МОМЕНТ на тех же данных/издержках "
          "(SSRN 4675565)")
    print("=" * 100)
    print(f"{'lb':>3}{'hold':>6}{'n':>6}{'ср.%':>9}{'итог%':>9}{'Sharpe':>8}"
          f"{'95% CI периода':>24}")
    for lb in lookbacks:
        for hold in holds:
            trials += 1
            r = tsmom_backtest(price, grid, lb, hold, 2 * TAKER_FEE)
            if r.get("n", 0) < 5:
                print(f"{lb:>3}{hold:>6}{r.get('n', 0):>6}   мало данных")
                continue
            ci = f"[{r['ci_lo_pct']:+.4f}; {r['ci_hi_pct']:+.4f}]"
            print(f"{lb:>3}{hold:>6}{r['n']:>6}{r['mean_pct']:>9.4f}"
                  f"{r['total_pct']:>9.2f}{r['sharpe']:>8.2f}{ci:>24}")

    print("\n" + "=" * 100)
    print("C. DELTA-NEUTRAL CARRY: LONG spot + SHORT perp (каноничная версия)")
    print("=" * 100)
    spot_avail = fetch_spot_symbols(sess)
    pairs = [s for s in funding if s in spot_avail]
    print(f"символов с обеими ногами (perp+spot): {len(pairs)} из {len(funding)}")
    if len(pairs) < args.legs:
        print("пар мало — секцию пропускаем")
    else:
        spot = build_spot_prices(sess, pairs, args.days, grid, args.verbose)
        print(f"spot-панель: символов={len(spot)}")
        if len(spot) < args.legs:
            print("spot-данных мало — секцию пропускаем")
        else:
            print(f"{'lb':>3}{'hold':>6}{'мин.ставка':>12}{'n':>6}{'ср.%':>9}"
                  f"{'итог%':>9}{'APR%':>8}{'Sharpe':>8}{'проc.%':>8}"
                  f"{'95% CI периода':>24}{'обор':>7}  IS/OOS")
            ih2, oh2 = split_halves(funding, perp_price, grid)
            for lb in [3, 9]:
                for hold in [3, 9]:
                    for min_rate in [0.0, 0.0001]:
                        trials += 1
                        r = carry_neutral_backtest(
                            funding, perp_price, spot, grid, args.legs, lb,
                            hold, TAKER_FEE, TAKER_FEE, min_rate)
                        if r.get("n", 0) < 5:
                            print(f"{lb:>3}{hold:>6}{min_rate:>12.4%}"
                                  f"{r.get('n', 0):>6}   мало данных")
                            continue
                        a = carry_neutral_backtest(
                            funding, perp_price, spot, ih2, args.legs, lb,
                            hold, TAKER_FEE, TAKER_FEE, min_rate)
                        b = carry_neutral_backtest(
                            funding, perp_price, spot, oh2, args.legs, lb,
                            hold, TAKER_FEE, TAKER_FEE, min_rate)
                        sa, sb = a.get("mean_pct", 0), b.get("mean_pct", 0)
                        agree = ("ДА" if (sa > 0) == (sb > 0)
                                 and a.get("n", 0) >= 5 and b.get("n", 0) >= 5
                                 else "нет")
                        ci = (f"[{r['ci_lo_pct']:+.4f}; "
                              f"{r['ci_hi_pct']:+.4f}]")
                        print(f"{lb:>3}{hold:>6}{min_rate:>12.4%}{r['n']:>6}"
                              f"{r['mean_pct']:>9.4f}{r['total_pct']:>9.2f}"
                              f"{r['apr_pct']:>8.1f}{r['sharpe']:>8.2f}"
                              f"{r['mdd_pct']:>8.1f}{ci:>24}"
                              f"{r['turnover']:>7.2f}  {agree}"
                              f" ({sa:+.4f}/{sb:+.4f})")

            print("\n" + "-" * 100)
            print("D. DELTA-NEUTRAL CARRY БЕЗ РОТАЦИИ (держим, пока платят) — "
                  "версия, которую предписывает литература")
            print("-" * 100)
            print(f"{'lb':>3}{'вход%':>9}{'выход%':>9}{'n':>6}{'входов':>8}"
                  f"{'ср.%':>9}{'итог%':>9}{'APR%':>8}{'Sharpe':>8}"
                  f"{'проc.%':>8}{'95% CI периода':>24}  IS/OOS")
            for lb in [3, 9, 21]:
                for enter_rate, exit_rate in [(0.00005, 0.0),
                                              (0.0001, 0.00002),
                                              (0.0002, 0.00005)]:
                    trials += 1
                    r = carry_persistent_backtest(
                        funding, perp_price, spot, grid, args.legs, lb,
                        enter_rate, exit_rate, 2 * TAKER_FEE)
                    if r.get("n", 0) < 5:
                        print(f"{lb:>3}{enter_rate:>9.4%}{exit_rate:>9.4%}"
                              f"{r.get('n', 0):>6}   мало данных")
                        continue
                    a = carry_persistent_backtest(
                        funding, perp_price, spot, ih2, args.legs, lb,
                        enter_rate, exit_rate, 2 * TAKER_FEE)
                    b = carry_persistent_backtest(
                        funding, perp_price, spot, oh2, args.legs, lb,
                        enter_rate, exit_rate, 2 * TAKER_FEE)
                    sa, sb = a.get("mean_pct", 0), b.get("mean_pct", 0)
                    agree = ("ДА" if (sa > 0) == (sb > 0)
                             and a.get("n", 0) >= 5 and b.get("n", 0) >= 5
                             else "нет")
                    ci = f"[{r['ci_lo_pct']:+.4f}; {r['ci_hi_pct']:+.4f}]"
                    print(f"{lb:>3}{enter_rate:>9.4%}{exit_rate:>9.4%}"
                          f"{r['n']:>6}{r['entries']:>8}{r['mean_pct']:>9.4f}"
                          f"{r['total_pct']:>9.2f}{r['apr_pct']:>8.1f}"
                          f"{r['sharpe']:>8.2f}{r['mdd_pct']:>8.1f}{ci:>24}"
                          f"  {agree} ({sa:+.4f}/{sb:+.4f})")

    print("\n" + "=" * 100)
    print(f"испытаний в прогоне: {trials} — порог значимости дефлировать на это "
          f"число (Bailey/Lopez de Prado)")
    print("Вывод принимается ТОЛЬКО если: CI периода не включает ноль И знак "
          "совпал в IS и OOS.")
    print("Бэктест имеет право вернуть «края нет» — это результат, а не "
          "неудача (no-data-fitting.mdc).")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
