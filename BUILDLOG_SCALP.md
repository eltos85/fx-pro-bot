# Build Log — scalp_bot (orderflow-скальпер Bybit)

Лог нового бота `src/scalp_bot/`. Изолирован от `ai_trader`/`bybit_bot`/
`fx_pro_bot`/`fx_ai_trader` (strategy-guard.mdc). Решения детерминированные,
по микроструктуре в реалтайме, БЕЗ LLM.

## 2026-05-30

### v0.2.1 — Telegram-нотификатор + переиспользование аккаунта ai_arena
`<hash>`

От удалённого ai_arena на VPS остались в `.env` отдельный demo-аккаунт
Bybit (`AI_ARENA_BYBIT_*`) и отдельный Telegram-бот (`AI_ARENA_TELEGRAM_*`).
Переиспользуем для scalp-bot:
- отдельный Bybit-аккаунт → чистый аудит PnL, не мешается с ai-trader
  (stats-collection.mdc);
- свой Telegram-бот для алертов.

Добавлен лёгкий `telegram/notifier.py` (только sendMessage, без поллинга
команд — не конфликтует с другими ботами на токене). Алерты: старт,
открытие/закрытие (PAPER и LIVE), killswitch. No-op если выключен/нет
token. На VPS `SCALP_BYBIT_*` и `SCALP_TELEGRAM_*` маппятся на
`AI_ARENA_*` через `.env` (compose их прокидывает).

**Файлы:** `telegram/notifier.py` (new), `config/settings.py`
(telegram_bot_token/chat_id), `trading/executor.py` (notify open/close),
`app/main.py` (notify старт/killswitch), `docker-compose.yml`,
`.env.example`, `tests/test_scalp_bot.py`.

### v0.2.0 — LIVE на demo по умолчанию, депо $1000, лот $10+, funding-guard
`<hash>`

По требованию пользователя: запускаем сразу на биржу (демо-счёт, риска нет),
PAPER больше НЕ дефолт.

- `trading_enabled=true` по умолчанию (LIVE на Bybit demo). PAPER остаётся
  опциональным режимом (false), но не навязывается.
- Капитал $1000; killswitch дневной $500 / совокупный $800 (буфер до
  обнуления депо); max 2 позиции; 20 сделок/час.
- Сайзинг переведён с фикс-риска на **фикс-notional**: лот $100, **минимум
  $10** (мельче — комиссия/спред съедают прибыль скальпа; пользователь
  мыслит «лотами в $»). Биржевой `minOrderQty` уважается.
- **Учёт комиссий**: LIVE-PnL = Bybit `closedPnl` (net, уже после maker/taker
  fee). Вход post-only maker (0.02%) дешевле taker (0.055%).
- **Funding-guard**: Bybit списывает/начисляет funding раз в 8ч
  (00:00/08:00/16:00 UTC) по открытой позиции. Для 90-сек скальпа почти не
  задевает, но бот НЕ открывает позиции в окне `avoid_funding_window_sec`
  (120с) перед списанием — funding-cost исключён полностью.

**Файлы:** `config/settings.py` (position_usd/min_position_usd, trading_enabled
default true, kill $500/$800, avoid_funding_window_sec), `trading/executor.py`
(position_size по notional + min-floor), `app/main.py` (funding-окно,
sec_to_next_funding), `docker-compose.yml`, `.env.example`, `tests/test_scalp_bot.py`.

### v0.1.0 — каркас orderflow-скальпера
`<hash>`

Новый отдельный бот по скальпингу в собственном Docker-контейнере
(`scalp-bot`, volume `scalp_bot_data`, env-namespace `SCALP_*`). Причина:
бэктесты свечного подхода (1H/15m/5m) показали отсутствие edge на
скальп-таймфреймах — для скальпа нужна микроструктура, которой у
`ai_trader` нет (см. чат «тупик свечной страты», BUILDLOG_AI_TRADER).

**Архитектура (rule-based, без LLM):**
- Данные: Bybit public WS (`publicTrade`→CVD, `orderbook.50`→imbalance,
  `tickers`→funding/OI, `allLiquidation`→каскады). Все потоки бесплатны и
  официальны (api-docs.mdc). Coinglass heatmap отвергнут — платный $699/мес,
  бесплатный план heatmap не даёт; Bybit `allLiquidation` отдаёт реальные
  ликвидации бесплатно (push 500ms).
- Сигнал: «свип ликвидности + поглощение» (mean-reversion fade). 5 микро-
  правил, CVD-дивергенция обязательна + ≥3/5 конфлюенс:
  1) sweep (свежий экстремум), 2) cvd_div [обяз.], 3) liq_flush,
  4) funding-перекос толпы против сделки, 5) ob_imbalance.
- Risk: фикс-риск $5/сделка (1% от $500, Van Tharp), плечо 5x, killswitch
  (дневной $50 / совокупный $150 / max 2 позиции / 20 сделок/час).
- Исполнение: post-only LIMIT вход (maker 0.02% вместо taker 0.055% —
  round-trip taker съедает 10-20% цели скальпа), reduce-only MARKET выход
  по тайм-стопу 90с; TP 1.5R, SL за свипнутым уровнем + 8 б.п.
- Режимы: PAPER (default, ордера симулируются на live-цене с учётом
  модельных комиссий) / LIVE на demo (флаг `SCALP_TRADING_ENABLED`).

**Валидация:** orderflow почти не бэктестится (нет дешёвой истории L2),
поэтому edge проверяется forward-тестом на **demo-счёте** (риска нет).
Набор ≥100 сделок, анализ WR/expectancy с учётом комиссий (sample-size.mdc)
до любых выводов об отключении/тюнинге. На реальные деньги — отдельное
решение пользователя после подтверждённого положительного expectancy.

**Smoke:** живой WS-коннект к Bybit подтверждён — приходят сделки (CVD),
стакан (imbalance), funding по BTC/ETH/SOL. 29 юнит-тестов зелёные
(сигналы, агрегаты, sizing, killswitch).

**Файлы:** `src/scalp_bot/` (config/settings, data/aggregates+market_stream,
analysis/signals, trading/client+executor, safety/killswitch, state/db,
app/main), `Dockerfile.scalp-bot`, `docker-compose.yml` (сервис scalp-bot
+ volume scalp_bot_data), `.env.example`, `pyproject.toml` (пакет+скрипт),
`tests/test_scalp_bot.py`.
