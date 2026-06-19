# BUILDLOG — tradecard-bybit

Журнал сборки advisory-ревьюера `tradecard-bybit` над детерминированными
Bybit-ботами `scalp_bot` и `flowzone_bot`. Канон — `STRATEGY_TRADECARD_BYBIT.md`
(SMB Momentum Model, адаптированный под rule-based системы), тех-задание —
`TASKSPEC_TRADECARD_BYBIT.md`.

`tradecard-bybit` — строго **read-only** аналитик: читает БД ботов, считает
report card, грейдит сделки по `score`, гоняет 5 Why (DeepSeek) над темой №1 и
отдаёт рекомендации человеку. НИЧЕГО не пишет в БД ботов и НЕ меняет их конфиг
(TASKSPEC §1/§9). Правки самих ботов в этот лог не пишем — только сам ревьюер.

Формат: записи группируются по дням (новые сверху). Для багов: симптом →
причина → решение. Для фич: что добавлено и на что влияет.

---

## 2026-06-19

### feat(tradecard-bybit): первичная реализация advisory-ревьюера (все 6 фаз)
`<pending commit>`

Что добавлено: новый пакет `src/tradecard_bybit/` — периодический (cron, не
realtime) ревьюер scalp/flowzone. Реализованы все фазы TASKSPEC:

- **Каркас (§3, §10):** `config/settings.py` (Pydantic, env-префикс
  `TRADECARD_BYBIT_`), `data/bot_db.py` (`BotDBReadOnly` через SQLite
  `mode=ro` — read-only инвариант), `data/reasons.py` (парсинг/атомизация
  токенов `reasons`), `analysis/trade.py` (нормализованная модель `Trade` с
  производными `r_multiple`/`session`/`iso_week`/`is_decided`), `state/db.py`
  (собственная SQLite: themes/hypotheses/theme_freq/small_wins).
- **Bybit ground-truth (§3.2, фаза 2):** `data/bybit_client.py`
  (`TradecardBybitReadOnly` — `closedPnl` с full pagination `while cursor:` +
  klines для MFE), `analysis/pnl.py` (`summarize_mode` + `bybit_net`; closedPnl
  как ground truth для live, приоритет над БД — stats-collection.mdc).
- **Детекторы §4 (фаза 3):** `analysis/detectors.py` — 8 паттернов
  (`grade_not_predictive`, `strategy_regime_leak`, `sl_cluster`,
  `exit_left_money`, `factor_noise`, `overtrading`, `big_game_hunting`,
  `paper_live_divergence`). Пороги структурные/относительные, без подгонки под
  P&L (no-data-fitting.mdc); запланированный SL ≠ ошибка.
- **Грейдинг §5 (фаза 4):** `analysis/grading.py` — `score`→бакет по
  квантилям, метрики WR/EXP/net на бакет, монотонность через Spearman(rank,
  EXP). `rank` = качество грейда (выше = лучше, A+ максимум).
- **5 Why (фаза 5):** `llm/client.py` (inline DeepSeek, Anthropic-compatible),
  `llm/five_why.py` (промпт из агрегатов паттерна + канон страты, парсинг
  цепочки и гипотезы). Гипотеза пишется в собственную БД, не в бота.
- **Small wins / momentum (§6, фаза 6):** `analysis/small_wins.py` —
  OOS-гейт через two-proportion z-test над частотой темы до/после внедрения
  гипотезы человеком; `report/weekly.py` — недельный markdown report card,
  `report/digest.py` — дневной Telegram-дайджест.
- **Оркестрация:** `analysis/engine.py` (прогон детекторов + выбор темы №1 по
  impact с sample-size гейтом), `app/main.py` (CLI `daily|weekly --bot
  scalp|flowzone [--since] [--dry-run] [--paper|--live]`).

Sample-size гейт (sample-size.mdc): тема объявляется и уходит в 5 Why только при
n ≥ порога; small win фиксируется лишь при статзначимом (p<0.05) снижении
частоты на OOS — иначе «наблюдение». Решения об отключении факторов/инструментов
— всегда человеку (strategy-guard.mdc).

Тесты §11: `tests/test_tradecard_bybit.py` — 30 тестов (парсинг reasons,
производные `Trade`, stats-хелперы, грейдинг + монотонность, все 8 детекторов,
оркестрация движка, closedPnl мок с pagination, 5 Why мок, small wins OOS,
read-only инвариант `BotDBReadOnly`). Полный набор репозитория: 1013 passed.

Баг при разработке (симптом → причина → решение): тесты грейдинга и
big_game_hunting падали — `GradeBucket.rank` был инвертирован (0 = A+),
из-за чего Spearman давал обратный знак монотонности, а `max(..., key=rank)`
выбирал низший грейд как top. Решение: `rank` переопределён как качество
(`len(LABELS)-1-index`, A+ максимум) — предиктивная кривая ⇒ положительный ρ.

Инфраструктура: `pyproject.toml` (entrypoint `tradecard-bybit` + пакет в wheel),
`Dockerfile.tradecard-bybit`, сервис `tradecard-bybit` в `docker-compose.yml`
(профиль `tools`, mount БД ботов read-only `:ro`, свой volume
`tradecard_bybit_data:/data`), `.env.example` (env-переменные ревьюера).

**Файлы:** `src/tradecard_bybit/**` (config, data, analysis, llm, report, state,
app), `tests/test_tradecard_bybit.py`, `pyproject.toml`,
`Dockerfile.tradecard-bybit`, `docker-compose.yml`, `.env.example`
