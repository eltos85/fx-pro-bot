# BUILDLOG — FX AI Trader (DeepSeek-V4 на cTrader FxPro: gold + Brent oil)

## 2026-05-12 — token rotation hardening (изоляция OAuth-токенов)

`коммит при deploy`

**Контекст.** В тот же день что и MVP deploy случился инцидент с OAuth:
`refresh_token` в shared `/data/ctrader_tokens.json` оказался spent
после single-use rotation. Advisor с 09.05 не торговал, fx-ai-trader не
смог стартануть. Детальный post-mortem — в `BUILDLOG.md` 12.05
«defensive token sync + startup token-status log».

После полного re-auth через `fx-pro-auth`-flow тот же риск остаётся
для будущего: если callback `_on_token_refreshed → token_store.save`
упадёт между OAuth-call и `save()` — refresh_token потеряется.

**Защита на 3 уровнях (см. BUILDLOG.md детали):**

**A. Defensive sync** в `CTraderClient._do_auth` — после каждого
успешного auth in-memory токены пишутся в shared store через
callback (идемпотентно). Закрывает race «refresh прошёл, callback
упал».

**B. Startup logging** через `auth.log_token_status` — INFO/WARN/ERROR
в `docker logs` обоих ботов про сколько дней до expiration. Видимость.

**C. Изоляция token-store** между Advisor и fx-ai-trader. Дефолтный
`AiFxTraderSettings.ctrader_token_path` сменён с
`/data/ctrader_tokens.json` (shared) на `/data/ctrader_tokens_ai_fx.json`
(отдельный grant). После этого refresh одного бота **не задевает** refresh
другого: они живут на двух независимых OAuth grant'ах одного приложения
client_id.

**Что НЕ изменилось.**

- `client_id` / `client_secret` остаются общие (это credentials самого
  приложения, не пары access/refresh).
- `CTRADER_ACCOUNT_ID=46883073` (demo-аккаунт) общий — оба бота
  торгуют на одном счёте, но с **разными labels** (`fx-pro-bot` для
  Advisor, `ai-fx-trader` для AI). Reconcile-логика отделяет позиции
  по label (см. `client_adapter.get_open_positions`).
- `token_lock.py` (file-flock advisory lock) остаётся, но теперь
  имеет academic смысл — два разных файла не конкурируют. Хранится
  как defence-in-depth: если кто-то в `.env` пропишет тот же path —
  flock спасёт от corrupted writes.

**OAuth-flow для fx-ai-trader (выполнено вручную перед push'ем).**

1. Сгенерирован тот же `grantingaccess/?client_id=...&redirect_uri=...`
   URL как для Advisor (один client_id = одно приложение cTrader).
2. Пользователь авторизовался в браузере **второй раз** — cTrader
   выдал **новый** authorization code (тот же приложение, тот же
   аккаунт, но второй независимый grant — refresh_token будет
   собственный).
3. На VPS внутри образа `fx-pro-bot:local` сделан
   `exchange_code_for_tokens(...)` → атомарная запись в
   `/data/ctrader_tokens_ai_fx.json`.
4. После этого запушен код-change (default path) и сделан selective
   rebuild fx-ai-trader.

**Verification после деплоя:**
- Логи fx-ai-trader при старте: `FX-AI-Trader cTrader OAuth: токен
  валиден до 2026-06-11..., осталось 30.0 дней` (INFO).
- Логи Advisor при старте: `Advisor cTrader OAuth: токен валиден до
  2026-06-11..., осталось 30.0 дней` (INFO).
- Файлы:
  - `/data/ctrader_tokens.json` — Advisor (refresh_token = `Slyfr...`)
  - `/data/ctrader_tokens_ai_fx.json` — fx-ai-trader (другой refresh_token)
- Оба бота подключаются к cTrader через свои OAuth grant'ы.

**Operational follow-up.** Раз в 2-3 недели смотреть `docker logs` обоих
ботов на наличие `WARNING ... токен истекает через X дней` — если
появилось, пробросить `fx-pro-auth` заранее на оба бота (отдельные
OAuth-flow для каждого token-файла).

**Файлы:** `BUILDLOG.md` 12.05 «defensive token sync + startup
token-status log» содержит полный список.

---

## 2026-05-12 — Phase 1 deploy + fix LLM max_tokens 4096→8000

`коммит при deploy`

**Контекст.** После коммита `871e67c` (MVP scaffold) — selective rebuild
fx-ai-trader на VPS. Контейнер изначально упал в restart-loop:

```
RuntimeError: cTrader refresh error: Access denied
```

**Причина (НЕ наш код).** Shared `/data/ctrader_tokens.json` содержал
`refresh_token`, который Spotware уже признал недействительным (cTrader
OAuth2: refresh_token rotation = single-use grant, RFC 6749 §6).
Последний успешный refresh-callback Advisor'а датируется ≈9 апреля
(вычислено по `expires_at = 1778349598` минус 30-дневное окно). С тех пор
файл не обновлялся, а в `client.py` proactive-refresh (`fb0ffd1`,
11.05.2026) использует тот же spent token.

На момент моей диагностики Advisor сам тоже шёл с `cTrader: торговля
отключена` (видно в его свежих логах) — то есть проблема общая, не у
fx-ai-trader специфическая.

**Решение.** Полный re-auth через `fx-pro-auth`-flow:
1. Сгенерировал auth URL, пользователь авторизовался в браузере
   ([id.ctrader.com](https://id.ctrader.com/my/settings/openapi/grantingaccess/)).
2. На VPS внутри образа `fx-pro-bot:local` (`docker run --rm
   --env-file .env`) сделал `exchange_code_for_tokens(...)` →
   `TokenStore.save(...)`. Новый `expires_at = 1781200153` (≈11 июня).
3. `docker compose restart advisor` + `docker compose up -d --no-deps
   fx-ai-trader` → оба контейнера подхватили свежие токены из
   `/data/ctrader_tokens.json` через свои `TokenStore.load()` / наш
   `ensure_valid_token_race_safe()`.

После рестарта:
- Advisor: `cTrader: аккаунт 46883073 авторизован, готов к торговле`.
- fx-ai-trader: `SymbolCache 252`, `XAUUSD → XAUUSD (id=41)`,
  `BZ=F → BRENT (id=1117)`, `Full cycle 1`, RSS 40 items (8–9 после
  gold+oil фильтра), DeepSeek 200 OK, цена цикла $0.00174.

**Урок и follow-up для будущего.** Когда оба бота держат `ctrader_tokens.json`
в общем volume — потеря синхронизации возможна (Advisor рефрешит in-memory,
но если callback `_on_token_refreshed → token_store.save` падает между
вызовом OAuth-endpoint и `json.dump` — token-store остаётся со spent refresh).
Наш `token_lock.py` решает concurrent refresh между процессами, но не
решает «callback failed mid-write». Phase 2: либо отдельный
`/data/ctrader_tokens_ai_fx.json` (полная изоляция OAuth у каждого бота),
либо health-check «is refresh-token still valid» в Advisor с alert через
RSS/Telegram. Сейчас зафиксировано как known-risk, без code-fix.

---

### fix(fx_ai_trader): LLM max_tokens 4096 → 8000 (JSON режется)

`коммит при deploy`

**Симптом.** После успешного старта в `07:50–08:08 UTC` 12.05 контейнер
работал, но 2 full-cycle подряд:

```
LLM tokens: in=4247 out=4096    ← упёрся в max_tokens
LLM response: ## Analysis Commentary 1. TREND: XAUUSD 4H EMA20 (4699)...
Parse error: no JSON object with 'action' found
Parse error: JSON parse error: not a decision dict (missing 'action'): dict
```

LLM выдавал полный analysis commentary (TREND / VOLATILITY / MACRO /
SENTIMENT для двух символов) и **обрезался по `max_tokens=4096`** до того
как добирался до финального JSON-блока. В результате `parse_action` не
находил decision-объект → `apply_action` не вызывался → бот не торговал.

**Причина (тех-параметр, не торговая логика).** Анти-Anthropic-compat
endpoint DeepSeek-V4 включает thinking-блок (внутренний reasoning) поверх
max_tokens. На bybit-варианте `ai_trader` 4096 достаточно (1 символ,
без EIA блока). У `fx_ai_trader` промпт ×2 длиннее:
- 2 символа × (current/1H × 24 / 4H × 30 / индикаторы)
- macro-block (DXY + EIA для oil)
- multi-dim sentiment блок per news × 5 items
- двойной EXIT MANAGEMENT блок (full vs review)

Соответственно ответ тоже ×2 длиннее. 4096 = thinking + commentary, на
JSON ничего не остаётся.

**Решение.** В `AiFxTraderSettings.deepseek_max_tokens` дефолт
`4096 → 8000`. Anthropic-compat API DeepSeek поддерживает до 8192
(см. [api-docs.deepseek.com/guides/anthropic_api](https://api-docs.deepseek.com/guides/anthropic_api)).
Стоимость full-cycle растёт с $0.00174 до ~$0.0028 (+60%) — для
paper-mode наблюдения за ~14 дней при 96 циклах/сутки = $3.9/мес вместо
$2.4, незначимо.

**Без изменений.** Промпт `SYSTEM_PROMPT` НЕ редактируется (он заморожен
правилом `no-data-fitting.mdc` на ≥14 дней paper-observation). Только
бюджет на output. KillSwitch, multi-dim sentiment, R:R 1.5 gate, paper
reconcile — без изменений.

**Compliance.** Не нарушает `strategy-guard.mdc` (тех-параметр клиента
LLM, не торговый порог), `no-data-fitting.mdc` (выход режется по
объективной причине: `out=4096` в логе, не подгонка под результаты
бэктеста), `sample-size.mdc` (не connection между сделками, n=0 на
момент фикса).

**Файлы:**
- `src/fx_ai_trader/config/settings.py` (default 4096 → 8000 + комментарий)
- `.env.example` (раздел AI_FX_TRADER, новый default)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

**Тесты.** 34/34 fx-ai-trader pass; полный suite 513/513 pass (max_tokens
не влияет ни на parser, ни на бизнес-логику).

---

## 2026-05-12 — Phase 1 MVP scaffold (paper-mode)

**Запрос пользователя:** «создать AI-агента для FX (gold + oil), аналог
Bybit ai_trader». Источник: chat-thread `[fx_ai_oil_trader_mvp_44cb1e89]`
и план `/.cursor/plans/fx_ai_oil_trader_mvp_44cb1e89.plan.md`.

**Что сделано.** Создан изолированный пакет `src/fx_ai_trader/` — третий
бот в репо (после `fx_pro_bot/` advisor и `bybit_bot/`/`ai_trader/`).
Phase 1: paper-mode на XAUUSD (spot gold CFD) + BZ=F (Brent oil →
cTrader `BRENT`), dual-timer 15+5 мин, DeepSeek-V4 через
Anthropic-compatible endpoint.

### Структура пакета

```
src/fx_ai_trader/
├── __init__.py / __main__.py
├── app/main.py                  # dual-timer 15+5 cycle (full + review)
├── config/settings.py           # AI_FX_TRADER_* env-prefix, paper-by-default
├── llm/
│   ├── prompts.py              # SYSTEM_PROMPT FX + multi-dim sentiment блок
│   └── client.py               # shim над ai_trader.llm.client (DeepSeek-V4)
├── trading/
│   ├── client_adapter.py       # CTraderFxAdapter поверх fx_pro_bot.CTraderClient
│   ├── token_lock.py           # race-safe OAuth refresh (fcntl.flock + re-check)
│   ├── executor.py             # Pydantic schema + parse_action + apply_action
│   ├── paper_reconcile.py      # SL/TP touch detection через M1 свечи
│   └── context.py              # collect + format full/review
├── news/
│   ├── rss.py                  # ForexLive/Investing/OilPrice/Kitco + double filter
│   └── eia.py                  # EIA Open Data weekly petroleum
├── state/db.py                 # AiFxTraderStore (positions/decisions/daily_pnl)
├── safety/killswitch.py        # FX-параметры + correlation-aware checks
└── analysis/indicators.py      # shim над ai_trader.analysis.indicators
```

### Ключевые решения и source-of-truth

**Изоляция от существующего Advisor (cTrader Gold-ORB):**

- Advisor торгует **GC=F futures** (`scalping_gold_orb.py`, MAX_POSITIONS=10),
  AI-агент торгует **XAUUSD spot** + **BRENT** — это другие cTrader
  `symbolId` → полная broker-side изоляция, никаких пересечений на
  margin pool.
- Все наши ордера маркируются `label="ai-fx-trader"`. Advisor ставит
  `label="fx-pro-bot"`. Reconcile у обоих фильтрует по своему label —
  никто не закроет чужую позицию как «orphan». Спецификация cTrader OpenAPI
  forum 41177 + FAQ подтверждают что `label` — string ≤100 chars,
  устойчивый к рестартам и виден в `ProtoOAReconcileRes.position`.
- Отдельная БД: `/data/fx_ai_trader.sqlite` (vs `stats.sqlite` advisor'а).
- OAuth token-store шарится: `/data/ctrader_tokens.json` — общий demo-аккаунт
  FxPro. Concurrent refresh защищён через `fcntl.flock` advisory lock с
  re-check после acquire (см. ниже Risk 4).

**Multi-dim sentiment block (Risk 1 mitigation):**

- Промпт требует от LLM 5-мерный sentiment per news item:
  `relevance / polarity / intensity / uncertainty / forwardness ∈ [0,1]`
  + `aggregate_uncertainty` per cycle.
- `executor.parse_action(max_uncertainty=0.7)` — gate: при
  `aggregate_uncertainty > 0.7` open-decisions reject'ятся ДО broker
  call'а. Cost-savings + предотвращает entry на low-conviction LLM.
- Sentiment JSON пишется в `decisions.sentiment_json` для post-hoc
  валидации калибровки uncertainty.

**Pydantic schema validation:**

- `OpenAction / CloseAction / HoldAction` — все три варианта решения.
- Структурные ошибки (неверный тип, отсутствующее поле, side ≠
  BUY/SELL, lots ≤ 0, lots > 10, uncertainty out of [0,1]) ловятся
  ДО apply-стадии (Risk 1: schema at agent boundaries — Tauric Research
  PR #458).

**KillSwitch — FX-параметры + correlation-aware:**

| Параметр | Значение | Обоснование |
|---|---|---|
| `MAX_DAILY_LOSS_USD` | 150 | `$25 risk × 6 убыточных = $150` |
| `MAX_TOTAL_LOSS_USD` | 300 | halt эксперимента (60% капитала) |
| `MAX_POSITIONS` | 3 | для 2 инструментов |
| `MAX_POSITIONS_PER_SYMBOL` | 2 | защита от over-allocation (Janus Henderson 2026) |
| `RISK_PER_TRADE_USD` | 25 | от virtual capital $500 (5%) |
| `MAX_LOT_SIZE` | 0.50 | hard cap, защита от extreme-волатильности |
| `CORRELATION_HAIRCUT` | 0.7 | gold↔oil correlated в risk-off (finaur 2026) |

Дополнительные guards:
- **Same-direction concentration**: 3-я позиция в одну сторону по
  correlated assets (XAUUSD + BZ=F = correlated group) REJECT'ится.
- **Correlation-haircut**: 2-я позиция в ту же сторону по correlated set
  → `volume_lots × 0.7` (research: finaur «correlation spikes in crisis»).

**Paper-mode reconcile через M1 свечи:**

В paper-mode broker не отрабатывает SL/TP (мы не ставили реальный ордер).
`paper_reconcile.reconcile_paper_positions()` тянет M1 свечи с момента
`opened_at` и для каждого бара проверяет touch SL/TP. Gap-day
(одновременный пробой SL и TP в одном баре) → preferred SL (conservative,
worst execution assumption).

NB: используются M1 свечи **самого cTrader** (`get_trendbars` period=1),
а не yfinance — это уменьшает «фантомные» touch'и из-за wide-spread
wicks. Weekend gaps корректны (бары просто отсутствуют в этом промежутке).

### Risks & mitigations (community-verified)

#### Risk 1 — LLM hallucinations / invalid output

**Mitigation:** Pydantic schema at boundary + multi-dim sentiment +
uncertainty gate `> 0.7 → reject open`.

**Source:**
- Tauric Research/TradingAgents PR #458 «schema at agent boundaries».
- Medium 2026 «Hallucination prevention out of the prompt and into the
  schema».
- arxiv 2603.11408 «Beyond Polarity: Multi-Dimensional LLM Sentiment
  Signals for WTI Crude Oil Futures Return Prediction».

#### Risk 2 — RSS / news noise

**Mitigation:**
- Source weights (OilPrice/Kitco = 0.7, ForexLive/Investing = 1.0).
- 12h time-window filter.
- Dedupe by normalized title (lowercase + strip punctuation, не по URL).
- Двойной keyword filter (gold-set + oil-set, news может попасть в оба).

**Source:**
- stock-market.live 2026 «Build a News-Driven Trade Bot — selectivity > speed».
- stockalpha.ai sentiment guide — entity extraction + time-window.

#### Risk 3 — cTrader rate-limit (50 req/s non-historical, 5 req/s historical)

**Mitigation:**
- Реюз `fx_pro_bot.trading.client.CTraderClient` — он уже имеет
  heartbeat 8s, smart-reset backoff `(5,10,30,60,120,300,900)`,
  `STABLE_UPTIME_SEC=300` (см. `BUILDLOG.md` 06-11.05.2026).
- Один общий клиент на процесс через `CTraderFxAdapter`.
- Bars-запросы только в начале каждого full-cycle (15 мин) + review
  (5 мин) — нагрузка ~5–10 req/cycle, далеко от лимитов.

**Source:** cTrader Open API docs `help.ctrader.com/open-api/connection/`,
community forum 45954 (silent token rotation).

#### Risk 4 — OAuth refresh race condition (shared token-store с Advisor)

**Mitigation:** `fx_ai_trader/trading/token_lock.py` —
`fcntl.flock` advisory exclusive lock + re-read под локом + atomic
write через `os.rename`. Если другой процесс уже refresh'нул токен
пока мы ждали — используем on-disk значение, refresh НЕ вызывается
(single-use refresh_token cTrader защищён).

**Source:**
- Coder PR #22904 «singleflight + optimistic locking».
- Nango blog «How to handle concurrency with OAuth token refreshes».
- openai/codex issue #10332 «file lock + re-check pattern».

#### Risk 5 — Paper-mode статистическая значимость

**Mitigation:** Phase 1 = ≥14 дней paper-observation (≥30 трейдов,
p-value < 0.05). LIVE не включаем без подтверждённой стабильности
парсера, калибровки uncertainty-gate, в разных режимах рынка
(тренд/флет/новости). Правило `sample-size.mdc` соблюдено.

**Source:**
- NexusTrade 2026 «30–90 days paper trading before live».
- Kiploks «weeks, not hours» calibration guide.
- `.cursor/rules/sample-size.mdc` (this repo).

#### Risk 6 — Gold instrument overlap с Advisor (GC=F)

**Mitigation:** Advisor торгует **GC=F futures** (cTrader: `GOLD_*` —
front-month contract), AI-агент торгует **XAUUSD spot** — это разные
символы на cTrader, разные `symbolId`, **независимый margin pool на
аккаунте**. На FxPro demo они существуют параллельно как разные
instruments. Дополнительно `label`-isolation на уровне reconcile.

**Source:**
- cTrader OpenAPI symbols catalog (`get_symbols` + `get_symbol_details`).
- FxPro symbols specification.

#### Risk 7 — Gold↔Oil correlation в risk-off

**Mitigation:**
- `CORRELATION_HAIRCUT = 0.7` на 2-ю позицию в одну сторону.
- Same-direction concentration check блокирует 3-ю позицию.
- LLM-промпт явно предупреждает: «moderately correlated during risk-off
  (both up on geopolitical, both down on USD strength)».

**Source:**
- finaur «Asset Correlation in Times of Crisis» 2026.
- Janus Henderson «Building smarter commodity exposure» 2026.

### Тесты

`tests/test_fx_ai_trader.py` — **34 теста, все зелёные**.
Polный pytest репо: **513 / 513 passed**.

Покрытие:
- `TestParseActionSchema` (15 тестов): hold + sentiment, open XAUUSD,
  open BRENT, high-uncertainty gate, custom threshold, invalid side,
  unknown symbol, negative lots, lots > 10, close, review-mode open
  reject, markdown fence, extra commentary, no JSON.
- `TestKillSwitch` (8): correlated set, empty store, max positions,
  per-symbol cap, correlation-haircut, same-dir concentration,
  opposite-dir allowed, daily loss block.
- `TestPaperReconcile` (6): long SL/TP, long no-touch, short SL/TP,
  gap-day prefer-SL.
- `TestVolumeRounding` (1): round-down к step + clamp [min,max].
- `TestTokenLockRecheck` (2): fresh-token no refresh, concurrent
  re-check (другой процесс перезаписал → refresh не вызван).
- `TestSettings` (2): defaults, db_path.

### Deploy (Phase 1 paper)

`Dockerfile.fx-ai-trader` (Python 3.12-slim, `pip install .`, entry
`python -m fx_ai_trader`) + сервис `fx-ai-trader` в `docker-compose.yml`
с bind-mount `./data:/data` (общий с Advisor для token-store).

Дефолты в compose:
- `AI_FX_TRADER_TRADING_ENABLED=false` (paper)
- `AI_FX_TRADER_SYMBOLS=XAUUSD,BZ=F`
- `AI_FX_TRADER_POLL_INTERVAL_SEC=900` / `REVIEW=300`

Шарит `DEEPSEEK_API_KEY` с `ai-trader` (один аккаунт-level rate-limit).

**Файлы:**
- new: `src/fx_ai_trader/` (полное дерево, 13 файлов)
- new: `tests/test_fx_ai_trader.py` (34 теста)
- new: `Dockerfile.fx-ai-trader`
- modified: `docker-compose.yml` (добавлен сервис `fx-ai-trader`)
- modified: `.env.example` (документация env'ов FX AI Trader)
- modified: `pyproject.toml` (`fx-ai-trader` entry-point + hatch packages)
