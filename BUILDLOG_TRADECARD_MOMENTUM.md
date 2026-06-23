# BUILDLOG — tradecard-momentum

Журнал сборки advisory-ревьюера `tradecard-momentum` над детерминированным
FX-ботом `fx_momentum_bot` (time-series momentum на cTrader/FxPro demo). Канон —
SMB Momentum Model (5-Step Process), тех-задание — `TASKSPEC_TRADECARD_FX.md`
(адаптировано: исходный таскспек написан под LLM-агента `fx_ai_trader`, здесь —
rule-based momentum).

`tradecard-momentum` — строго **read-only** аналитик: читает БД momentum-бота
(контекст входа) + cTrader deal-list (ground truth по P&L), считает report card,
грейдит сделки по силе сигнала (`|momentum_value|`), гоняет 5 Why (DeepSeek) над
темой №1 и отдаёт рекомендации человеку. НИЧЕГО не пишет в БД бота и НЕ меняет
его конфиг (advisory-only, strategy-guard.mdc). Правки самого momentum-бота в
этот лог не пишем — только сам ревьюер (они в `BUILDLOG.md`).

Формат: записи группируются по дням (новые сверху). Для багов: симптом →
причина → решение. Для фич: что добавлено и на что влияет.

---

## 2026-06-23

### feat(tradecard-momentum): новый advisory-ревьюер для fx_momentum_bot
`<pending commit>`

Запрос пользователя: «добавить `TASKSPEC_TRADECARD_FX.md` к momentum_bot». Так
как таскспек написан под LLM-агента `fx_ai_trader` (поля `decisions`/
`sentiment_json`/`thesis_status`/`llm_reason`, которых у rule-based momentum-бота
нет), детекторы/грейдинг/5-Why адаптированы под механику momentum (TSMOM
sign-rule, ATR-trailing, BE@1R, partial@1.5R, edge-trigger вход). Реализация — по
аналогии с `tradecard_bybit`, **отдельный самостоятельный пакет**
`src/tradecard_momentum/` (без импортов из `tradecard_bybit`), своя SQLite, свой
Dockerfile/сервис.

**Источник правды по P&L — cTrader deal-list** (broker-净 gross+swap+commission,
stats-collection.mdc/ctrader-pnl.mdc), НЕ локальная БД (momentum хранит только
факт открытия без realized PnL). Атрибуция деалов — по торговой вселенной
momentum (FX-мажоры → symbolId), т.к. `ProtoOADeal` не несёт label (логика как в
проверенном `scripts/momentum_pnl_audit.py`). Сделки `fx_ai_trader`
(XAUUSD/BRENT/NG) на общем счёте исключаются.

**R-multiple** реконструируется в ценовых единицах как `signed_move / risk_price`,
где `risk_price = atr × atr_stop_mult` — плановая SL-дистанция входа (бот так и
считает: `fx_momentum_bot/app/main.py`). ATR берётся из совпавшего по времени
executed-решения `momentum_decisions` (таблица `momentum_position_state`
чистится при закрытии, ATR-решение персистит). Грейд §5 — по `|momentum_value|`
(сила сигнала входа): квантильные бакеты A+/A/B/C, проверка монотонности
Spearman.

**Детекторы (нейтральные/относительные пороги, no-data-fitting.mdc):**
- `signal_not_predictive` — сила сигнала не монотонна по EXP (грейд сломан);
- `symbol_session_leak` — срез symbol/session/side EXP<0 при общем плюсе;
- `loss_cluster` — доля убытков на (symbol×side) ≥ factor × базовой (без
  `close_reason`, которого нет в deal-list, кластеризуем по факту убытка);
- `overtrading` — перегретые часы хуже спокойных (дребезг edge-trigger);
- `swap_drag` — overnight financing съедает ≥ доли валовой прибыли на удерживаемых
  TSMOM-позициях (momentum-специфичный детектор).

**5 Why / small wins / momentum** — как в bybit-ревьюере: тема №1 проходит
sample-гейт (≥100 сделок), 5 Why через DeepSeek (momentum-канон в промпте),
гипотеза-кандидат в собственную БД; small win засчитывается ТОЛЬКО OOS после
одобренного человеком внедрения (≥100 сделок, ≥2 недели, p<0.05). В конфиг
momentum-бота tradecard не пишет.

**⚠️ cTrader connection limit (api-docs.mdc):** лимит 2 connections per
application. momentum + fx_ai_trader уже держат 2 коннекта на общем app
(client_id). Для конкурентного запуска tradecard нужен ОТДЕЛЬНЫЙ cTrader-app
(`TRADECARD_MOMENTUM_CTRADER_CLIENT_ID`) либо запуск в окно, когда слот свободен.
По умолчанию creds дефолтятся на `MOMENTUM_BOT_CTRADER_*` (compose).

**Деплой:** сервис `tradecard-momentum` в `docker-compose.yml` (profile `tools`,
запуск планировщиком/вручную: `docker compose run --rm tradecard-momentum
tradecard-momentum daily|weekly`). БД momentum монтируется read-only
(`momentum_bot_data:/bots/momentum:ro`), свой volume `tradecard_momentum_data`.

**Тесты:** `tests/test_tradecard_momentum.py` (23 теста) — детекторы, грейдинг,
P&L, R-multiple, 5-Why prompt/parse, small-wins OOS-гейт, read-only инвариант
БД momentum (запись физически невозможна, `mode=ro`), broker-хелперы
(`_scale_price`, `_match_decision`). Полный набор — 1043 passed.

**Файлы:** `src/tradecard_momentum/**` (config, analysis, data, llm, report,
state, app), `Dockerfile.tradecard-momentum`, `docker-compose.yml` (сервис +
volume), `pyproject.toml` (пакет + entry-point `tradecard-momentum`),
`tests/test_tradecard_momentum.py`, `BUILDLOG_TRADECARD_MOMENTUM.md`.
