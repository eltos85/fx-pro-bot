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

### Где хранится статистика в Docker

База **`advisor_stats.sqlite`** пишется в **именованный том** `advisor_data`, смонтированный в `/data`. Он **не входит в слои образа** и **не стирается** при `docker compose build` или при пересборке образа. Данные теряются только если явно удалить том, например `docker compose down -v`.

При первом запуске пустого тома `entrypoint` копирует в `/data` шаблон `events_calendar.yaml` из образа; дальше вы правите календарь внутри тома или подключаете свой файл.

CLI статистики с тем же томом:

```bash
docker compose run --rm advisor python -m fx_pro_bot.app.stats_cli report
```

Локально без тома (папка `data/` на машине): задайте вручную `docker compose run --rm -v "$(pwd)/data:/data" advisor` или создайте `docker-compose.override.yml` (не коммитьте) с `volumes: ["./data:/data"]` вместо именованного тома.

## Автодеплой на VPS (push в `main`)

После push в ветку `main` GitHub Actions подключается к серверу по SSH и выполняет `scripts/deploy-on-vps.sh` (обновление кода из Git и `docker compose build && up -d`).

Нужны секреты репозитория: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_DEPLOY_PATH`. Пошаговая настройка: [docs/DEPLOY_VPS.md](docs/DEPLOY_VPS.md).

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
