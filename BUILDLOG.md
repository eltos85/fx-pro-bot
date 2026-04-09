# Build Log

Лог изменений FX Pro Bot с момента подключения демо-счёта cTrader (07.04.2026).

---

## 2026-04-09

### fix: broker PNL — sanity check executionPrice + grossProfit fallback
`301c540`

cTrader иногда возвращает некорректный `executionPrice` в deal history
(например, цена GBPJPY вместо EURJPY → +2753 pips вместо +7).
Добавлен sanity check (>5% от entry → отклонение), fallback на
`closePositionDetail.grossProfit` для расчёта пипсов.
Используется `closePositionDetail.entryPrice` как реальная цена входа.

**Файлы:** `executor.py`, `main.py`

### fix: broker trailing для ensemble/outsiders теперь ATR-based
`f5087ae`

Брокерский trailing использовал фиксированные 3 pips — на волатильных
инструментах (XPTUSD, metals) cTrader закрывал позицию слишком рано.
Теперь логика совпадает с клиентской: `max(0.15×ATR, 3 pips)`.
STRATEGIES.md: ensemble добавлен в таблицу серверных TP/SL, порог входа
уточнён до 4/5 (MIN_STRENGTH=0.7 → strength 3/5=0.6 не проходит).

**Файлы:** `main.py`, `STRATEGIES.md`

### fix: atomic TP/SL in order + ensemble strategy TP + ATR-based exits
`21d2296`

Переход на атомарное выставление SL/TP через `relativeStopLoss`/`relativeTakeProfit`
в `ProtoOANewOrderReq` — TP/SL ставятся вместе с ордером, а не отдельным amend.
Ensemble теперь получает TP (`max(0.5×ATR, 10 pips)`). Outsiders/ensemble exit
использует ATR-мультипликаторы. Металлы (GC=F, HG=F, PL=F) возвращены.
Scalping accordion: TP=5, trigger=3, distance=2 pips.

**Файлы:** `main.py`, `executor.py`, `client.py`, `monitor.py`, `settings.py`, `cost_model.py`

### Auto-reconnect cTrader on connection drop
`1475fd6`

Автопереподключение к cTrader при обрыве TCP/TLS. Обработка
`AccountDisconnectEvent` и `TokenInvalidatedEvent` с автообновлением
access token через refresh token. Heartbeat каждые 10 сек.

**Файлы:** `client.py`

---

## 2026-04-08

### Document server-side TP/SL + dynamic trailing
`9b32fa6`

Документация серверного TP/SL в STRATEGIES.md: таблица уровней по стратегиям,
описание атомарного выставления через cTrader API.

### Leaders: server-side TP (50 pips) + dynamic trailing SL
`37db250`

Leaders получает серверный TP=50 pips. Динамический trailing SL на cTrader:
каждый цикл пересчитывается и обновляется через `amend_position_sl_tp`.

### Return metals, accordion 3/5, fix SL in sync
`8a9279d`

Возврат металлов (GC=F, HG=F, PL=F). Scalping: TP=5, trigger=3, distance=2.
Исправлен SL при синхронизации старых позиций.

### Server-side TP on cTrader + detect broker closures
`7645bf8`

Серверный TP для outsiders (10 pips), скальпинга (5 pips).
Детект закрытий на стороне брокера: каждый цикл сверка DB vs cTrader.

### Remove/add metals, scalping TP/trailing
`12d4774` `cf1da6c`

Итерации с металлами (убраны, потом возвращены) и настройка
take-profit/trailing для скальпинговых стратегий.

### Stats: P&L in USD, correct pip_value
`dda0a41` `18b8cc8` `b294c99` `88df80a`

Переход статистики с пипсов на USD. Использование реального `broker_volume`
и `contract_size` из cTrader для точного расчёта pip_value.
P&L берётся из cTrader API (unrealized PnL endpoint).

### Remove silver, prevent oversized positions
`b0cf74c` `515960b`

Серебро (SI=F) убрано. Защита от открытия слишком больших позиций
при подтягивании к `min_volume` cTrader.

---

## 2026-04-07

### Подключение cTrader демо-счёта

Начальный депозит $500 + пополнение $1000 = **$1500 итого**.

### feat: expand market coverage
`1097d2d`

Расширение списка инструментов: индексы (ES=F, NQ=F), крипто (BTC, ETH),
облигации (ZN=F), дополнительный форекс и commodities.

### cTrader API integration
`41578cb` → `0343a5e`

Полный цикл интеграции cTrader Open API:
- TLS handshake + service-identity
- Авторизация приложения и аккаунта
- Автообнаружение `ctidTraderAccountId`
- Маппинг yfinance → cTrader символов (статический + динамический prefix)
- Исполнение ордеров для всех стратегий
- Синхронизация бумажных позиций с брокером
- Reconciliation при старте + защита от зависших сделок
- Округление SL/TP до digits символа
- Закрытие с реальным volume из cTrader

### Broker reconciliation + stuck trade protection
`47cf771` `8c48149`

Сверка открытых позиций при старте. Если позиция есть в DB но нет
на cTrader — закрытие как `broker_closed`. Использование
`tradeData.volume` из `ProtoOAPosition`.
