#!/usr/bin/env bash
# Запуск на VPS из корня клонированного репозитория (вызывается из GitHub Actions по SSH).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REMOTE="${DEPLOY_REMOTE:-origin}"
BRANCH="${DEPLOY_BRANCH:-main}"

git fetch "$REMOTE"
git reset --hard "$REMOTE/$BRANCH"

docker compose build
docker compose up -d

echo "Deploy OK: $(git rev-parse --short HEAD)"
