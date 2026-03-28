# Сборка образа для публикации в реестре (GitHub Container Registry и др.)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --upgrade pip

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# Котировки yfinance в образе — чтобы DATA_SOURCE=yfinance работал из коробки
RUN pip install --no-cache-dir ".[quotes]"

ENV DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "-m", "fx_pro_bot.app.main"]
