# BUILDLOG — FX AI Trader (DeepSeek-V4 на cTrader FxPro: gold + Brent oil)

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
