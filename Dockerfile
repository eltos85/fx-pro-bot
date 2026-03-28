# Сборка образа для публикации в реестре (GitHub Container Registry и др.)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --upgrade pip

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY data/events_calendar.yaml /opt/fx-pro-bot/default_events_calendar.yaml
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN pip install --no-cache-dir .

# База advisor_stats.sqlite и календарь — только в смонтированном томе, не в слоях образа
ENV DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "fx_pro_bot.app.main"]
