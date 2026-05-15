# Миграция ai_arena: demo → real money

Runbook для одноразового перехода c Bybit demo (sandbox-обманка
`virtual_capital_usd=$1000` на $50k demo equity) на реальный live
аккаунт. Все шаги обязательны. Порядок имеет значение.

> **Когда читать:** строго перед моментом когда ты собираешься
> переключить `AI_ARENA_BYBIT_DEMO=false` и подать в `.env` live
> API-ключ. До этого — не нужно.

> **Источник правды контракта:** правило
> [`.cursor/rules/ai-arena-sources.mdc`](.cursor/rules/ai-arena-sources.mdc),
> секция «Equity scaling — offset-based». Этот файл —
> инструкция-исполнение, контракт описан там.

---

## 0. TL;DR

```
1. Stop bot
2. Backup БД
3. Get live API key (НЕ withdraw, IP whitelist)
4. Update .env (3 переменные)
5. Code patch: вернуть REAL-MONEY GUARD + kv_delete (из этого файла)
6. Reset anchor (одна SQL-команда)
7. (опц.) Reset experiment history
8. Deploy + verify первый цикл
```

Время выполнения: ~20 минут с тестированием.

---

## 1. Корень риска (зачем все эти шаги)

Текущая схема — **sandbox-only контракт**: LLM считает
`Position Size = sandbox_cash × leverage × allocation%`, **НО**
`quantity` из его JSON-ответа исполняется на Bybit **ровно как есть,
без масштабирования** (`executor.py` берёт число из ответа и шлёт
в `place_order`).

| Сценарий (real_cash vs virtual_capital) | Что произойдёт |
|---|---|
| `real_cash >> virtual_capital` (например $5000 vs $1000) | LLM посчитает quantity от $1k. На бирже это микроразмер от $5k — **бот недозагружает капитал** в 5×. |
| `real_cash << virtual_capital` (например $300 vs $1000) | LLM посчитает quantity от $1k. На бирже не хватит margin → **order reject / margin call** на больших leverage. |
| `real_cash ≈ virtual_capital` ✅ | `Position Size` совпадает с реальным размером → корректная торговля. |

**Вывод:** на live МОДЕЛЬ должна видеть тот же баланс, что реально
на бирже. Это значит `AI_ARENA_VIRTUAL_CAPITAL` = реальный депозит
USDT **ровно**, а offset-anchor — сброшен (иначе LLM в первый
цикл увидит «PnL −$49000» из-за разницы между demo $50k anchor и
live ~$1k equity).

---

## 2. Pre-flight checklist

- [ ] **Bybit live API key** создан, права: `Derivatives → Position` и
      `Derivatives → Order`, **БЕЗ Withdraw**. IP whitelist на VPS
      `204.168.149.140`.
- [ ] Решение по депозиту: какая сумма USDT будет на live аккаунте.
      **Минимум для теста — $100**. Меньше — большинство BTC/ETH
      ордеров не пройдут min_qty.
- [ ] SSH доступ на VPS работает: `ssh root@204.168.149.140 "echo ok"`.
- [ ] Свежий бэкап БД (см. шаг 4).
- [ ] Прочитан раздел «Что НЕЛЬЗЯ делать при переходе» (внизу).

---

## 3. Шаг 1: остановить бот

```bash
ssh root@204.168.149.140 "cd /root/fx-pro-bot && docker compose stop ai-arena"
```

Проверка:

```bash
ssh root@204.168.149.140 "docker ps -a --filter name=ai-arena --format '{{.Names}} {{.Status}}'"
# должен быть Exited
```

---

## 4. Шаг 2: бэкап БД (на случай отката)

```bash
ssh root@204.168.149.140 "docker run --rm -v ai_arena_data:/data \
  -v /root/backups:/backup alpine sh -c 'cp /data/ai_arena.sqlite \
  /backup/ai_arena.pre-live-$(date +%Y%m%d-%H%M).sqlite'"
```

Проверь что файл создан:

```bash
ssh root@204.168.149.140 "ls -la /root/backups/ai_arena.pre-live-*"
```

Этот бэкап понадобится только в случае rollback (см. секцию 11).

---

## 5. Шаг 3: обновить `.env` на VPS

Открой `.env`:

```bash
ssh root@204.168.149.140 "nano /root/fx-pro-bot/.env"
```

Изменения (три ключа):

```dotenv
# Демо OFF
AI_ARENA_BYBIT_DEMO=false

# Live API ключи (НЕ demo)
AI_ARENA_BYBIT_API_KEY=<новый live key>
AI_ARENA_BYBIT_API_SECRET=<новый live secret>

# КРИТИЧНО: должно совпадать с реальным USDT на счёте РОВНО
# Пример: депозит $1000 → 1000. Депозит $500 → 500.
AI_ARENA_VIRTUAL_CAPITAL=1000
```

**ОБЯЗАТЕЛЬНО проверь** что `AI_ARENA_VIRTUAL_CAPITAL` совпадает
с тем что фактически положишь на live. Расхождение >5% — критическая
ошибка sizing'а.

---

## 6. Шаг 4: вернуть REAL-MONEY GUARD в код

Эти правки были откачены 2026-05-15 (BUILDLOG: `revert: REAL-MONEY
GUARD из main.py`) как «не нужны пока торгуем на demo». Сейчас они
становятся обязательными.

### 6.1 `src/ai_arena/state/db.py` — добавить `kv_delete()`

Найди класс `AiArenaStore`, секцию с `kv_get` / `kv_set`, добавь
**после `kv_set`**:

```python
def kv_delete(self, key: str) -> None:
    """Удаляет ключ из ``kv_state``.

    Используется при миграции demo → real money: нужно сбросить
    `real_equity_at_start_usd`, чтобы бот при следующем старте
    зафиксировал anchor от **live** equity, а не от demo.

    См. MIGRATION_AI_ARENA_TO_REAL_MONEY.md шаг 7.
    """
    with self._conn() as c:
        c.execute("DELETE FROM kv_state WHERE key = ?", (key,))
```

### 6.2 `src/ai_arena/app/main.py` — добавить guard на старте

В функции `run()`, **после** проверки `DEEPSEEK_API_KEY` и **до**
создания `AiArenaStore`, добавь:

```python
# ⚠️ Real-money safety: offset-based scaling работает только когда
# `virtual_capital_usd` ≈ реальный депозит USDT. Если бот пошёл в
# live (`bybit_demo=False`) с дефолтным $1000 (которое заточено под
# demo $50k sandbox-обманку), quantity от LLM поедет на бирже либо
# с грубой недогрузкой капитала (real >> $1k), либо с margin call /
# order reject (real << $1k). Падаем на старте — лучше чем тихо
# сжечь депозит. См. MIGRATION_AI_ARENA_TO_REAL_MONEY.md.
if not settings.bybit_demo and settings.virtual_capital_usd == 1000.0:
    log.error(
        "REAL-MONEY GUARD: bybit_demo=False, но virtual_capital_usd=$1000 "
        "(дефолт sandbox). Это приведёт к некорректному position sizing. "
        "Установи AI_ARENA_VIRTUAL_CAPITAL = реальный стартовый депозит "
        "USDT перед запуском в live."
    )
    return
```

> **Зачем именно `== 1000.0`**: это дефолт `Field(default=1000.0)`
> в `settings.py`. Если оператор явно поставил $1000 в env (т.е.
> реально на счёте $1000) — guard не сработает (см. секцию 12 FAQ).
> Чтобы guard сработал даже при ручном указании `1000`, поменяй
> дефолт в `settings.py` на `0.0` и проверяй `<= 0`.

### 6.3 (опционально) расширить docstring в `settings.py`

В `src/ai_arena/config/settings.py` найди `virtual_capital_usd`
и добавь над `Field(...)`:

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ⚠️  ПЕРЕХОД НА РЕАЛЬНЫЕ ДЕНЬГИ — sandbox MUST совпадать с real
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Контракт «sandbox-only»: LLM считает Position Size от sandbox-cash,
# но quantity из его JSON исполняется на Bybit как есть. Если
# `real_cash != virtual_capital_usd`, бот будет либо торговать ниже
# capacity (при real >> virtual), либо отправлять заведомо невыполнимые
# ордера (при real << virtual → margin call / reject).
#
# Demo: $50k real >> $1k sandbox = ОК (demo «амортизирует»).
# Real: ОБЯЗАН выставить `AI_ARENA_VIRTUAL_CAPITAL` = реальный
# стартовый депозит USDT ровно. См. MIGRATION_AI_ARENA_TO_REAL_MONEY.md.
```

### 6.4 Прогнать тесты

```bash
cd /Users/aa_solnyshkin/bots/fx_pro_bot
python3 -m pytest tests/test_ai_arena_*.py -q
```

Должно быть **all passed** (≥254 ai_arena тестов). Если падают —
вероятно, опечатка при копипасте, проверь.

### 6.5 Закоммитить + запушить

```bash
git add src/ai_arena/state/db.py src/ai_arena/app/main.py \
  src/ai_arena/config/settings.py BUILDLOG_AI_ARENA.md
git commit -m "feat(ai-arena): REAL-MONEY GUARD + kv_delete for live migration"
git push origin main
```

GitHub Actions сделает deploy на VPS автоматически (см.
`.cursor/rules/deploy-vps.mdc`).

---

## 7. Шаг 5: сбросить anchor от demo

Без этого LLM в первом live-цикле увидит:
`cumulative_real_pnl = real_now ($1000) − real_at_start_anchor ($50000) = −$49000`
→ sandbox_equity показан как `$1000 + (−$49000) = −$48000`.

Это сломает не только sizing — это пошлёт LLM в режим максимально
агрессивных позиций («нужно отыграть −98%»).

**Сбросить anchor** (выполнять только когда контейнер УЖЕ остановлен):

```bash
ssh root@204.168.149.140 "docker run --rm -v ai_arena_data:/data \
  alpine sh -c 'apk add -q sqlite && sqlite3 /data/ai_arena.sqlite \
  \"DELETE FROM kv_state WHERE key=\\\"real_equity_at_start_usd\\\";\"'"
```

Проверка что ключа больше нет:

```bash
ssh root@204.168.149.140 "docker run --rm -v ai_arena_data:/data \
  alpine sh -c 'apk add -q sqlite && sqlite3 /data/ai_arena.sqlite \
  \"SELECT * FROM kv_state WHERE key=\\\"real_equity_at_start_usd\\\";\"'"
# Должно вернуть пустую строку
```

> **Альтернатива через kv_delete метод** (если контейнер ещё бегает
> либо вернул guard в код): `docker exec ... python -c '...'`. Но
> stop-then-sqlite надёжнее: гарантирует что бот не перезапишет
> anchor параллельно.

---

## 8. Шаг 6 (опционально): чистый эксперимент

Если хочешь чтобы Sharpe / `total_return_pct` считались с нуля от
live-старта (а не от demo-снапшотов):

```bash
ssh root@204.168.149.140 "docker run --rm -v ai_arena_data:/data \
  alpine sh -c 'apk add -q sqlite && sqlite3 /data/ai_arena.sqlite <<EOF
DELETE FROM equity_snapshots;
DELETE FROM decisions;
DELETE FROM positions;
DELETE FROM daily_pnl;
EOF'"
```

> **ВНИМАНИЕ:** это **необратимая** очистка истории demo-эксперимента.
> Делай это ТОЛЬКО если бэкап (шаг 4) подтверждённо лежит в
> `/root/backups/`. Если нужна история demo для анализа — пропусти
> этот шаг, бот спокойно продолжит копить статистику поверх (но
> Sharpe первые недели будет искажён).

---

## 9. Шаг 7: пополнить live аккаунт

Только сейчас — **после** шагов 1-7 — пополни Bybit live USDT-кошелёк
суммой РАВНОЙ `AI_ARENA_VIRTUAL_CAPITAL` из шага 5.

Минимум — $100 для тестовых пары часов.

---

## 10. Шаг 8: запустить + verify

```bash
# Запуск
ssh root@204.168.149.140 "cd /root/fx-pro-bot && docker compose up -d ai-arena"

# Логи первых 60 секунд
ssh root@204.168.149.140 "docker logs -f fx-pro-bot-ai-arena-1 --tail 50"
```

**Что проверить в логах первого цикла:**

1. **REAL-MONEY GUARD НЕ сработал** — нет строки `REAL-MONEY GUARD:
   bybit_demo=False, но virtual_capital_usd=$1000 (дефолт sandbox)`.
2. **Anchor зафиксирован на live**: строка
   `Real-equity anchor зафиксирован: $X.XX` где X ≈ твоему депозиту
   (не $50k от demo, не 0).
3. **LLM call показывает корректные числа**:
   `LLM call: positions=0 real=$1000.00 anchor=$1000.00 → sandbox=$1000.00 (PnL +0.00, +0.00%)`
4. **Bybit отвечает** (нет `ERROR ai_arena.trading.client: get_*`).

Если хоть один пункт не выполнен — **немедленно** stop + см. секцию 11.

---

## 11. Rollback (если что-то пошло не так)

Откат на demo:

```bash
# 1. Стоп
ssh root@204.168.149.140 "cd /root/fx-pro-bot && docker compose stop ai-arena"

# 2. Восстановить БД из бэкапа
ssh root@204.168.149.140 "docker run --rm -v ai_arena_data:/data \
  -v /root/backups:/backup alpine sh -c 'cp /backup/ai_arena.pre-live-<TIMESTAMP>.sqlite /data/ai_arena.sqlite'"

# 3. Откатить .env (вернуть AI_ARENA_BYBIT_DEMO=true и demo ключи)
ssh root@204.168.149.140 "nano /root/fx-pro-bot/.env"

# 4. Откатить code-патчи (REAL-MONEY GUARD + kv_delete)
git revert <SHA коммита из шага 6.5>
git push origin main

# 5. Закрыть открытые позиции на live руками через Bybit UI
# (бот не будет их видеть после rollback)

# 6. Старт
ssh root@204.168.149.140 "cd /root/fx-pro-bot && docker compose up -d ai-arena"
```

---

## 12. FAQ

**Q: Что если у меня на счёте РОВНО $1000 — guard заблокирует?**
A: Да, потому что `1000.0` — дефолт. Поменяй в `settings.py` дефолт
на `0.0` и условие на `<= 0`. Тогда `AI_ARENA_VIRTUAL_CAPITAL=1000`
явно из env пройдёт guard, а отсутствие переменной — нет.

**Q: Можно увеличивать депозит после старта?**
A: Можно, но **`AI_ARENA_VIRTUAL_CAPITAL` менять нельзя**.
Offset-схема работает по принципу `scaled_equity = virtual + (real_now − real_anchor)`. Доливание $500 на live → `real_now += 500` → LLM
видит `sandbox_equity += 500` (как «прибыль»). Это исказит метрики
и поведение модели. Если планируешь часто доливать — нужен другой
scaling mode (см. правило, секция «Альтернатива»).

**Q: Что если LLM в первом цикле решит закрыть позицию которой нет?**
A: `executor._apply_close` отдаст `executed=False, error=close: no
open position`. Это нормально, бот продолжит работу. Опасности нет.

**Q: Как отслеживать что бот реально торгует на live, а не висит?**
A: Bybit UI → Account → API Logs. Если 4+ часа нет API-вызовов от
ключа — бот завис. Проверь `docker logs`.

**Q: Что насчёт fees? На live они выше demo?**
A: Bybit fees одинаковые на demo и live для derivatives (taker
0.055%, maker 0.02%). Но spread / slippage на live может быть
заметно хуже на низколиквидных альтах (BNB / DOGE / SOL). Поэтому
старт на минимальном депозите.

**Q: Bot спорадически шлёт `pnl=pending…` в Telegram?**
A: Это balance-delta fallback при тишине Bybit `closed-pnl`. На
live latency обычно <10s — pending должен встречаться редко. На
demo было до 5+ минут. См. BUILDLOG 2026-05-15 «balance-delta
fallback».

---

## 13. Что НЕЛЬЗЯ делать при переходе на live

- ❌ Оставить `virtual_capital_usd=1000` если на счёте не $1000 ровно.
- ❌ Не сбросить anchor — бот посчитает что реальный equity «прыгнул»
  с demo $50k на live $1k и покажет LLM `PnL = −$49000` в первом
  цикле.
- ❌ Включить `bybit_demo=False` без шагов 5-7 → потеря депозита из-за
  oversized orders (margin call).
- ❌ Добавить server-side max-loss / max-leverage / killswitch как
  «компенсацию» — это нарушит Nof1-design (риск-менеджмент только в
  prompt'е, см. правило `ai-arena-sources.mdc` секцию «Что
  ЗАПРЕЩЕНО»). Безопасность достигается ограничением **депозита**,
  не логики бота.
- ❌ Запустить live без бэкапа БД (шаг 4). Без него rollback
  невозможен.

---

## 14. Связанное

- Правило: [`.cursor/rules/ai-arena-sources.mdc`](.cursor/rules/ai-arena-sources.mdc)
  — контракт source-of-truth, секции «Equity scaling — offset-based»
  и «Что ЗАПРЕЩЕНО».
- Deploy: [`.cursor/rules/deploy-vps.mdc`](.cursor/rules/deploy-vps.mdc)
  — селективный rebuild + GH Actions.
- BUILDLOG: [`BUILDLOG_AI_ARENA.md`](BUILDLOG_AI_ARENA.md) — историю
  откатов REAL-MONEY GUARD ищи 2026-05-15.
