#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${1:-/root/fx-pro-bot}"

# Runtime-состояние, которое пишется сервисами и НЕ должно приезжать из git:
# `reset --hard` ниже вернул бы файл к версии коммита. Инцидент 2026-08-20
# (BUILDLOG.md): откат токена cTrader на трёхмесячный + рестарт token-service
# оставил бы все cTrader-боты без авторизации. Копия лежит вне рабочего дерева,
# чтобы деплой её не затрагивал.
RUNTIME_STATE_DIR="${FX_RUNTIME_STATE_DIR:-/root/.fx-pro-bot-state}"
RUNTIME_FILES=("data/ctrader_tokens.json")

cd "$DEPLOY_PATH"

echo ">>> backup runtime state"
mkdir -p "$RUNTIME_STATE_DIR"
chmod 700 "$RUNTIME_STATE_DIR"
for rel in "${RUNTIME_FILES[@]}"; do
    if [ -s "$rel" ]; then
        install -m 600 "$rel" "$RUNTIME_STATE_DIR/$(basename "$rel")"
        echo "    сохранён $rel"
    fi
done

echo ">>> git pull"
git fetch origin main
git reset --hard origin/main

# Файл ещё может быть tracked в старом HEAD: тогда reset его удалит.
echo ">>> restore runtime state"
for rel in "${RUNTIME_FILES[@]}"; do
    bak="$RUNTIME_STATE_DIR/$(basename "$rel")"
    if [ ! -s "$rel" ] && [ -s "$bak" ]; then
        mkdir -p "$(dirname "$rel")"
        install -m 600 "$bak" "$rel"
        echo "    восстановлен $rel"
    fi
done

echo ">>> Stopping ALL bot containers"
docker compose down --remove-orphans 2>/dev/null || true
docker ps -a --filter "ancestor=fx-pro-bot:local" -q | xargs -r docker rm -f 2>/dev/null || true
docker ps -a --filter "name=fx.pro.bot" -q | xargs -r docker rm -f 2>/dev/null || true
docker container prune -f 2>/dev/null || true
sleep 2

echo ">>> docker compose build"
docker compose build

echo ">>> docker compose up -d"
docker compose up -d

echo ">>> Waiting for container to start..."
sleep 5

echo ">>> Container status:"
docker compose ps

echo ">>> Last 10 log lines:"
# advisor живёт в профиле `disabled`; его отсутствие не повод падать под set -e.
docker logs fx-pro-bot-advisor-1 --tail 10 2>/dev/null || \
    echo "    advisor не запущен (профиль disabled)"

echo ">>> Deploy complete"
