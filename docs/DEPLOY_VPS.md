# Автодеплой: push в `main` → VPS

При каждом push в ветку `main` workflow [`.github/workflows/deploy-vps.yml`](../.github/workflows/deploy-vps.yml) подключается к серверу по SSH и выполняет [`scripts/deploy-on-vps.sh`](../scripts/deploy-on-vps.sh): `git fetch` + `reset` на `origin/main`, затем `docker compose build` и `docker compose up -d`.

## 1. Подготовка VPS

- Установлены **Docker** и **Docker Compose** (plugin `docker compose`).
- Репозиторий клонирован, например:  
  `git clone git@github.com:eltos85/fx-pro-bot.git && cd fx-pro-bot`
- На VPS настроен доступ к GitHub: **Deploy key** (только read) или SSH-ключ пользователя с доступом к репозиторию — иначе `git fetch` не сработает.
- Каталог клонирования запомните — он пойдёт в секрет `VPS_DEPLOY_PATH` (абсолютный путь, например `/home/deploy/fx-pro-bot`).

Локальные правки в клоне на VPS нежелательны: скрипт делает `git reset --hard origin/main`.

## 2. SSH с GitHub Actions на VPS

На VPS в `~/.ssh/authorized_keys` пользователя деплоя добавьте **публичный** ключ, парный тому, чей **приватный** ключ вы положите в секрет `VPS_SSH_KEY`.

Сгенерировать пару только для деплоя:

```bash
ssh-keygen -t ed25519 -f github-actions-deploy -N ""
```

- `github-actions-deploy.pub` → в `authorized_keys` на VPS.
- Содержимое `github-actions-deploy` (приватный ключ, целиком) → секрет **`VPS_SSH_KEY`** в GitHub.

## 3. Секреты репозитория

GitHub → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret            | Пример                         |
|-------------------|--------------------------------|
| `VPS_HOST`        | `203.0.113.10` или домен       |
| `VPS_USER`        | `deploy`                       |
| `VPS_SSH_KEY`     | приватный ключ (весь PEM)      |
| `VPS_DEPLOY_PATH` | `/home/deploy/fx-pro-bot`      |

Пока секреты не заданы, job будет падать — это ожидаемо.

## 4. Проверка

- **Actions** → workflow **Deploy to VPS** → при push в `main` или **Run workflow** вручную.
- На сервере: `docker compose ps`, логи контейнера при необходимости.

## 5. Данные (SQLite)

Том `advisor_data` в `docker-compose.yml` сохраняет статистику между деплоями. Не выполняйте `docker compose down -v`, если не хотите удалить том.
