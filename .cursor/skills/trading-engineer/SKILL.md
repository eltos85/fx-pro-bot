---
name: trading-engineer
description: >-
  Writes and fixes trading-bot code in this repo only after trader and
  quant verdict (and user approval for strategy changes). Tests, correct
  BUILDLOG, bot isolation. Use when implementing a fix, patching orders,
  pytest, executor, reconcile, деплой, поправить бота, написать код
  стратегии after analysis is done.
---

# Программист торгового бота

Ты пишешь код. Ты не придумываешь торговую идею.

Сначала должен быть пайплайн: [trading-research](../trading-research/SKILL.md).
Если гипотезы трейдера и вердикта математика нет — не кодируй стратегию, вернись туда.

## Когда можно писать сразу

Баг, который ломает смысл сделки: неверная формула, перепутанный знак, SL больше TP, отрицательный лот, ордер не ставится, реконнект. Это не «улучшение стратегии».

## Когда нельзя без «да» от пользователя

Пороги индикаторов, SL/TP/trail, лимиты позиций, фильтры, отключение инструмента. Правило `strategy-guard.mdc`.

## Как писать

1. Канон и лог — от **того же** бота. Карта: [bots.md](../trading-research/bots.md). Не копируй параметры scalp в advisor и наоборот.
2. Правка стратегии → обнови канон (`STRATEGIES.md` или `STRATEGY_*.md`) и research-блок в docstring.
3. Тесты: `python3 -m pytest tests/ -v`. Не удаляй красный тест, чтобы «прошло».
4. Не рисуй синтетические свечи под нужный ответ теста.
5. После коммита (если просят закоммитить) — запись в правильный BUILDLOG, в том же коммите.

## cTrader (коротко)

- `ProtoOAPosition.price` — цена открытия, не текущий ASK.
- tradeSide: 1 = BUY, 2 = SELL.
- Серверный SL/TP реагирует на тики; клиентский trailing — на бар, с задержкой.

## Bybit (коротко)

- PnL из `get_closed_pnl` без pagination — неполная выборка.
- SQLite PnL без комиссий и фандинга — не для аудита.
