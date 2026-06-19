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

### feat(tradecard-bybit): per-strategy разрез (baseline / P&L / детекторы)
`<pending commit>`

Запрос пользователя: у scalp 3 основные страты (sweep_fade, density_break,
density_bounce; sweep_fade_canon — A/B-вариант, остаётся отдельной линией) —
изучать их раздельно, и baseline (точка отсчёта правки логики) у страт разный.

Что добавлено:
- **Per-strategy baseline:** env `TRADECARD_BYBIT_{SCALP,FLOWZONE}_BASELINE_DATES`
  формата `strat=YYYY-MM-DD,strat2=YYYY-MM-DD` (приоритетнее bot-wide
  `*_BASELINE_DATE`-fallback). `baseline_ts(bot, strategy)`,
  `min_baseline_ts(bot)`. CLI грузит от min-baseline и режет per-trade по дате
  правки логики конкретной страты (`_load_floor` + `_filter_baseline`); отчёт
  пишет список активных baseline. До правки логики — «другая стратегия»,
  смешивать нельзя (no-data-fitting + sample-size).
- **P&L по стратегиям:** `summarize_by_strategy` (per-trade verified, сорт по
  net — худшие сверху) + секция в weekly report card.
- **Детекторы per-strategy:** `overtrading` и `big_game_hunting` теперь гоняются
  раздельно по стратам в движке (scope.strategy); `paper_live_divergence` уже
  бинил по `(strategy, symbol)`; остальные (grade/regime/sl_cluster/factor_noise/
  exit_left_money) и так per-strategy.

Тесты: +4 (`test_baseline_ts_botwide_and_per_strategy`,
`test_filter_baseline_per_strategy`, `test_summarize_by_strategy`,
`test_overtrading_per_strategy_in_engine`). 35 пакета / 1018 общий — зелёные.

**Файлы:** `src/tradecard_bybit/config/settings.py`, `app/main.py`,
`analysis/{pnl,engine,detectors}.py`, `report/weekly.py`, `docker-compose.yml`,
`.env.example`, `tests/test_tradecard_bybit.py`

### fix(tradecard-bybit): R-multiple взрывается при SL≈entry (float-эпсилон)
`<pending commit>`

Симптом: в weekly report card flowzone EXP/avgR показывал мусор — триллионы
(напр. `EXP=4069962070250.05`, бакет C), из-за чего грейд-кривая и тема №1
строились на испорченных средних.

Причина (проверено на реальных данных `/bots/flowzone/flowzone_bot.sqlite`,
read-only): у части сделок `sl` практически равен `entry`, отличаясь лишь на
float-эпсилон округления цены (напр. `entry=0.23430000000000004`,
`sl=0.2343` → dist≈2.8e-17, risk≈9.5e-14). `r_multiple = pnl/risk` делил на
почти-ноль → R≈−6.4e11. Случаи точного `dist==0` уже отсекались (R=None), а
эпсилон-случаи проскакивали и доминировали в среднем EXP (mean чувствителен к
выбросам). Это артефакт данных (трейл в безубыток / SL не пишется отдельно),
не реальный риск-план.

Решение: `planned_risk_usd` считает риск только если |entry−sl| > entry×1e-6
(относительный структурный фильтр; наблюдаемые риск-дистанции ботов ≈0.4–1.3%
entry — на порядки выше порога, подгонки P&L нет). Иначе R=None и сделка не
входит в EXP/avgR (но остаётся в WR/net). Тест
`test_r_multiple_none_when_sl_equals_entry`. 31 тест пакета зелёный.

**Файлы:** `src/tradecard_bybit/analysis/trade.py`,
`tests/test_tradecard_bybit.py`

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
