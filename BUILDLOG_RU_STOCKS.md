# Build Log — RU Stocks Analyst

## 2026-06-03

### Новости RSS + ИИ-аналитика по заголовкам

Пакет `ru_stocks_analyst/news/`: агрегатор RSS (РБК, Интерфакс, Коммерсантъ,
Ведомости, ТАСС), сопоставление с тикерами портфеля, блок в дайджесте.
`llm/market_brief.py` — DeepSeek-обзор рынка и бумаг портфеля **только** по
переданным заголовкам + сверка с техскринером. `RU_STOCKS_NEWS_ENABLED` и
`RU_STOCKS_LLM_ENABLED` по умолчанию true.

**Файлы:** `news/*`, `llm/market_brief.py`, `digest/builder.py`, `app/main.py`

### MVP: советник MOEX + Tinkoff REST + Telegram
`(локально, до коммита)`

Новый пакет `src/ru_stocks_analyst/`: чтение портфеля брокерского счёта,
широкий скринер ликвидных акций MOEX (EMA/RSI/ATR, swing 1–3 дня), утренний
дайджест и алерты в Telegram. Без выставления заявок. REST-клиент вместо
официального gRPC SDK (pip `tinkoff-investments` недоступен в окружении).

**Файлы:** `ru_stocks_analyst/*`, `tests/test_ru_stocks_analyst.py`,
`README_RU_STOCKS.md`, `pyproject.toml` (entrypoints)
