# fx-pro-bot

**Советник** (не автоторговля): смотрит на историю цен, выдаёт **простые текстовые советы** на русском, учитывает **ваш календарь событий** и пишет всё в **SQLite** для подсчёта, как часто советы оказываются верными при ручной проверке.

Сделок, логина в кабинет брокера и API-ключей **нет**.

## Возможности

- Источник данных: встроенный **stub** или публичные котировки **Yahoo** через `yfinance` (тикеры вроде `EURUSD=X`, `CL=F` для нефти).
- Стратегия-заглушка: пересечение двух средних (`simple_ma_crossover`) — её можно заменить на свою в `analysis/`.
- Календарь: файл `data/events_calendar.yaml`.
- Статистика: `data/advisor_stats.sqlite` (в git не попадает).

## Установка

```bash
cd fx_pro_bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# для реальных котировок Yahoo:
pip install -e ".[quotes]"
pytest -q
```

Запуск советника:

```bash
fx-pro-bot
# или
python -m fx_pro_bot.app.main
```

Оценка советов после факта (по id из списка):

```bash
fx-pro-stats list
fx-pro-stats mark <uuid> right
fx-pro-stats mark <uuid> wrong --notes "рынок развернулся после новости"
fx-pro-stats report
```

## Переменные окружения

См. [.env.example](.env.example). Главное: `DATA_DIR`, `DATA_SOURCE` (`stub` / `yfinance`), `YFINANCE_*`, `DISPLAY_NAME`.

## Docker

```bash
docker compose build
docker compose run --rm advisor
```

Каталог `./data` монтируется в контейнер как `/data` (база и календарь сохраняются на хосте).

## Git

Репозиторий готов к публикации: `LICENSE` (MIT), `.gitignore`, `.dockerignore`. Инициализация:

```bash
git init
git add .
git commit -m "Initial commit: advisor + stats + docker"
```

Файлы `data/*.sqlite` в индекс не добавляйте — они в `.gitignore`.

## Документация по модулям

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Отказ от ответственности

Код предназначен для обучения и тестов. Это не индивидуальная инвестиционная рекомендация.
