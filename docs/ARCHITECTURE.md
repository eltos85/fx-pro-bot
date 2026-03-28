# Архитектура

## Общий поток

```
Цикл (каждые POLL_INTERVAL_SEC секунд):
  1. Сканирование: yfinance → бары для каждого инструмента → MA-кроссовер → сигналы
  2. Запись: новые LONG/SHORT сигналы → SQLite (suggestions)
  3. Верификация: созревшие сигналы → текущая цена → расчёт профита → SQLite (verifications)
  4. Статистика: win-rate, профит по инструментам и горизонтам → лог
```

## Модули

| Пакет | Модуль | Назначение |
|-------|--------|------------|
| `config/` | `settings.py` | Настройки: инструменты, интервалы, горизонты проверки |
| `market_data/` | `yfinance_feed.py` | Загрузка свечей через Yahoo Finance |
| `market_data/` | `models.py` | `Bar`, `Tick`, `InstrumentId` |
| `analysis/` | `signals.py` | MA-кроссовер, `Signal`, `TrendDirection` |
| `analysis/` | `scanner.py` | Сканирование списка инструментов, ранжирование |
| `advice/` | `human.py` | Текст совета на русском |
| `events/` | `calendar_loader.py` | Экономический календарь из YAML |
| `stats/` | `store.py` | SQLite: таблицы `suggestions` и `verifications` |
| `stats/` | `verifier.py` | Автопроверка сигналов: сравнение цен, расчёт пунктов |
| `app/` | `main.py` | Основной цикл сканера |
| `app/` | `stats_cli.py` | CLI: отчёт, список сигналов, ручная оценка |

## Docker

- Один сервис `advisor` с `restart: unless-stopped`
- Том `advisor_data` → `/data` (SQLite и календарь)
- Настройки через переменные окружения (см. `.env.example`)
