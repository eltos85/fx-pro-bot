#!/usr/bin/env python3
"""Артефакт анализа для flowzone_bot C4: какая сессия несёт основной объём.

Канон Fabervaale («The Only Orderflow Guide» 28:54): *«I only trade in the New
York session for US indices because it's where the majority of the volume get
traded and I find it from statistical validation the London session to be
usually for US indices not so valuable to add to the profile. So I only use the
cash session profile.»* — то есть профиль строится по ОДНОЙ сессии, той, где
торгуется большинство объёма, а не по склейке нескольких.

Для крипто-инструментов (BTC/ETH/SOL, 24/7) «cash session» не определена, и
выбор окна обязан опираться на измерение, а не на аналогию с US indices
(no-data-fitting.mdc). Скрипт считает распределение оборота (turnover, USD) по
часам UTC на часовых барах Bybit и сравнивает долю оборота кандидат-окон.

Запуск::

    python3 scripts/flowzone_session_volume.py

Результат прогона 2026-07-29 (1000 часовых баров ≈ 41 день, среднее по трём
символам, нормировка внутри символа чтобы BTC не доминировал):

    пик 12:00-17:00 UTC (13:00 = 8.91%, 14:00 = 8.34%, 15:00 = 7.61%)
    London 07-16   46.8% за 9ч
    NY     12-21   51.4% за 9ч   ← максимум среди 9-часовых окон
    union  07-21   67.2% за 14ч  (разбавлено: +5ч ради +15.8 п.п.)

Вывод: канон-сессия для крипты = NY 12:00-21:00 UTC.
"""
from __future__ import annotations

import json
import urllib.request
from collections import defaultdict

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
# Кандидат-окна UTC для сравнения (name, start_hour, end_hour).
CANDIDATES = (
    ("London 07-16", 7, 16),
    ("NY 12-21", 12, 21),
    ("union 07-21", 7, 21),
    ("US cash 13-20", 13, 20),
    ("NY ext 12-22", 12, 22),
    ("Asia 00-07", 0, 7),
)


def fetch_hourly(symbol: str, limit: int = 1000) -> list[list[str]]:
    """Часовые бары Bybit v5 (public endpoint, без авторизации).

    https://bybit-exchange.github.io/docs/v5/market/kline — limit max 1000,
    ответ: [startTime, open, high, low, close, volume, turnover].
    """
    url = ("https://api.bybit.com/v5/market/kline?category=linear"
           f"&symbol={symbol}&interval=60&limit={limit}")
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.load(resp)
    return payload["result"]["list"]


def hourly_turnover_share() -> tuple[dict[int, float], dict[str, int]]:
    """Доля оборота по часам UTC, усреднённая по символам.

    Внутри символа доли нормируются на его суммарный оборот — иначе BTC
    определял бы результат единолично.
    """
    share: dict[int, float] = defaultdict(float)
    bars: dict[str, int] = {}
    for symbol in SYMBOLS:
        rows = fetch_hourly(symbol)
        bars[symbol] = len(rows)
        per_hour: dict[int, float] = defaultdict(float)
        total = 0.0
        for row in rows:
            ts = int(row[0]) // 1000
            turnover = float(row[6])
            per_hour[(ts // 3600) % 24] += turnover
            total += turnover
        if total <= 0:
            continue
        for hour, value in per_hour.items():
            share[hour] += value / total
    norm = sum(share.values()) or 1.0
    return {h: share[h] / norm for h in range(24)}, bars


def main() -> None:
    share, bars = hourly_turnover_share()
    print("часовые бары:", bars)
    print("\nдоля оборота по часам UTC (среднее по символам):")
    for hour in range(24):
        print(f"  {hour:02d}:00  {share[hour]:6.2%}  {'#' * int(share[hour] * 400)}")
    print("\nдоля оборота по кандидат-окнам:")
    for name, start, end in CANDIDATES:
        window = sum(share[h] for h in range(24) if start <= h < end)
        print(f"  {name:<16}{window:7.1%}  ({end - start}ч)")


if __name__ == "__main__":
    main()
