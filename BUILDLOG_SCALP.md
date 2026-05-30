# Build Log — scalp_bot (orderflow-скальпер Bybit)

Лог нового бота `src/scalp_bot/`. Изолирован от `ai_trader`/`bybit_bot`/
`fx_pro_bot`/`fx_ai_trader` (strategy-guard.mdc). Решения детерминированные,
по микроструктуре в реалтайме, БЕЗ LLM.

## 2026-05-30

### v0.1.0 — каркас orderflow-скальпера (PAPER-ready)
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
поэтому edge проверяется forward-тестом — сначала PAPER, набор ≥100 сделок,
анализ WR/expectancy с учётом комиссий (sample-size.mdc), только потом
demo-live. Никаких реальных денег до подтверждения положительного
expectancy после комиссий.

**Smoke:** живой WS-коннект к Bybit подтверждён — приходят сделки (CVD),
стакан (imbalance), funding по BTC/ETH/SOL. 29 юнит-тестов зелёные
(сигналы, агрегаты, sizing, killswitch).

**Файлы:** `src/scalp_bot/` (config/settings, data/aggregates+market_stream,
analysis/signals, trading/client+executor, safety/killswitch, state/db,
app/main), `Dockerfile.scalp-bot`, `docker-compose.yml` (сервис scalp-bot
+ volume scalp_bot_data), `.env.example`, `pyproject.toml` (пакет+скрипт),
`tests/test_scalp_bot.py`.
