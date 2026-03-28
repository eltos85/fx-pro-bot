#!/bin/sh
set -e
# Том /data живёт отдельно от слоёв образа — статистика и календарь не пропадают при пересборке.
mkdir -p /data
if [ ! -f /data/events_calendar.yaml ] && [ -f /opt/fx-pro-bot/default_events_calendar.yaml ]; then
  cp /opt/fx-pro-bot/default_events_calendar.yaml /data/events_calendar.yaml
fi
exec "$@"
