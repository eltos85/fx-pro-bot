#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH="${1:-/root/fx-pro-bot}"

cd "$DEPLOY_PATH"

echo ">>> git pull"
git fetch origin main
git reset --hard origin/main

echo ">>> Stopping ALL bot containers (old and new naming)"
docker compose down 2>/dev/null || true
docker ps -a --filter "ancestor=fx-pro-bot:local" -q | xargs -r docker rm -f 2>/dev/null || true
docker ps -a --filter "name=fx.pro.bot" -q | xargs -r docker rm -f 2>/dev/null || true
docker container prune -f 2>/dev/null || true

echo ">>> docker compose build"
docker compose build --no-cache

echo ">>> docker compose up -d"
docker compose up -d

echo ">>> Waiting for container to start..."
sleep 5

echo ">>> Container status:"
docker compose ps

echo ">>> Last 10 log lines:"
docker logs fx-pro-bot-advisor-1 --tail 10

echo ">>> Deploy complete"
