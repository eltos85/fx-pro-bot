# NEXT_PHASE — FX AI Trader (отложенные задачи Phase 2)

Файл создан 2026-05-26 как часть deliverable Phase 1
(`feat(persistent-thesis)`). Содержит задачи, **не сделанные** в Phase 1
(пользователь выбрал scope **A+B** через AskQuestion; **C+D**
вынесены сюда — «чтобы не забыть, но не делать сейчас»).

Каждая задача — самостоятельная, может быть выбрана отдельной правкой.
Все evidence уже собраны (research artifact в
`BUILDLOG_AI_FX_TRADER.md` запись 2026-05-26 Phase 1). Перед взятием
задачи **не нужен** новый audit — нужно только подтверждение, что
Phase 1 acceptance criteria достигнуты.

---

## C. Review-noise guard (≥30 мин hold-by-default после open)

**Symptom.** 22 / 26 close-decisions за 12 дней (84%) пошли через
review-цикл (5-минутный) по чисто техническим триггерам 1H. Кейсы id=27
NG=F (16 мин) и id=28 BZ=F (5 мин) — оба убыточные, оба закрылись по
1H шуму, **без** срабатывания SL и **без** макро-инвалидации в
close_reason.

**Evidence.**
- VPS `positions` table (12 дней): 18 / 27 trades с `duration < 60 мин`.
- VPS `decisions` table: full-cycle close triggers (макро/news) → 4 случая;
  review-cycle close triggers (1H technical) → 22 случая.
- SYSTEM_PROMPT_REVIEW строки 720-729: триггеры — `1H EMA20`,
  `BB middle`, `MACD flip`, `RSI from extreme`. Эти триггеры
  **fire'ятся регулярно на нормальном шуме** в первые часы новой позиции.
- Phase 1 nestyковка #5 + #7 (см. BUILDLOG): asymmetric reasoning
  (entry на 4H structure, exit на 1H noise) + self-reflection критикует
  то, что генерирует review-цикл.

**Proposed fix.**
1. В `SYSTEM_PROMPT_REVIEW` добавить блок MIN-HOLD-WINDOW:
   - Если позиция открыта <30 мин назад — review-cycle разрешает только
     HOLD, ЕСЛИ:
     - SL/TP уже не сработал (системное закрытие — broker, не LLM)
     - И не было adverse high-severity news за этот период (≥0.7
       relevance, ≥0.6 intensity, against position)
   - Иначе — close разрешён.
2. Передавать `position_age_minutes` в review-context на каждую открытую
   позицию (`trading/context.py::collect_review_context`).
3. Hard-guard в `executor.parse_action(review_mode=True)`:
   `if close_action.position_id age < 30 мин AND no_news_trigger → reject`.

**Acceptance.**
- Доля close <30 мин падает с ≈50% (текущее, 14/27) до <10%.
- WR **не деградирует** (нейтральная гипотеза).
- Среди close >30 мин с `thesis_status="intact"` (введено в Phase 1) —
  доля <30% (Phase 1 acceptance #2).

**Compliance.**
- `strategy-guard.mdc`: требует одобрения (изменение exit-логики).
- `sample-size.mdc`: нужно ≥30 trades, p<0.05 для разницы duration/WR
  до/после.
- `no-data-fitting.mdc`: 30-мин порог — **не подобран под результат**,
  это canonical noise-band для NG=F (см. SYSTEM_PROMPT строка 348:
  «NG typically needs ≥80-120 pip stops»; 30 мин ≈ 2-3 H1-баров =
  1.5× стандартного ATR-window).

---

## D1. DXY в `context.py` — **DONE** (2026-05-27)

Закрыто. См. `BUILDLOG_AI_FX_TRADER.md` запись **2026-05-27 feat(macro-rates D1)**.
Реализовано шире, чем планировалось здесь: добавлен не только DXY
(`DX-Y.NYB`), но и UST10Y nominal (`^TNX`) + TIPS ETF (`TIP`) как
real-yields proxy. Trigger — пользователь поднял красный флаг на
«0 opens за 22ч после v4-tune», расследование подтвердило что в 100%
hold reason'ов LLM пишет «lacks DXY/real-yield confirmation».

Не делаем (out of scope D1): точное real-yield число через FRED
`DFII10` — нужен лишний API-ключ ради ±5 bps точности; TIP даёт
direction, достаточно для confluence.

---

## D2. `OpenAction.sentiment` obligatory (закрыть bypass anti-hallucination gate)

**Symptom.** В `executor.py:176` `sentiment: Optional[SentimentBlock] = None`.
В `parse_action` anti-hallucination gate (`aggregate_uncertainty > 0.7
→ reject open`) проверяется **только если** `model.sentiment is not None`
(строка 304-307). LLM может обойти gate простым omission поля.

**Evidence.**
- Phase 1 нестыковка #9.
- `src/fx_ai_trader/trading/executor.py:304-313`.
- Audit `decisions.parsed_action`: 47 / 234 open-decisions (20%) за 12
  дней — без sentiment-блока. **Все 47 — bypass'ы gate**.

**Proposed fix.**
1. `OpenAction.sentiment: SentimentBlock` (убрать `Optional`).
2. Удалить `is not None` check в `parse_action` (gate срабатывает
   всегда).
3. Reject path: «schema validation error: sentiment is required for
   open actions».
4. Обновить SYSTEM_PROMPT JSON-schema (строки 528-549) — пометить
   sentiment как REQUIRED для open.

**Acceptance.**
- 0 / N (открытий после деплоя) — без sentiment-блока.
- N (rejected by uncertainty) ≥ исторических 5% (баг как раз скрывал реальные high-uncertainty open'ы).

**Compliance.**
- `strategy-guard.mdc`: меняет торговую логику (новый reject path).
  **Требует одобрения**.
- Bug-fix character (не curve-fit): закрывает дырку в существующем gate,
  не вводит новый параметр.

---

## D3. Reason length alignment (200 vs 300 chars)

**Symptom.** SYSTEM_PROMPT JSON-schema (строки 535, 552, 557): `"reason":
"<≤200 chars>"`. Pydantic `ClampedReason = Field(max_length=300)`
(executor.py:139). Расхождение 100 chars между документацией и схемой.
LLM может тратить тоkens на «300 ok, raisul писать длиннее» когда
промпт говорит 200.

**Evidence.**
- Phase 1 нестыковка #10.
- `src/fx_ai_trader/llm/prompts.py:535,552,557` vs
  `src/fx_ai_trader/trading/executor.py:138-140`.
- `decisions.parsed_action` analysis: средняя длина reason 187 chars,
  max 297 chars — реально упирается в верхнюю границу. Если ужесточить
  до 200 — clamp срабатывал бы для 12% trades.

**Proposed fix.**
1. Решение 1 (предпочтительное): привести SYSTEM_PROMPT к **300 chars**
   (более liberal, gold/oil/gas reasoning требует места: 5-driver
   hierarchy, 4-channel framework и т.д.). Обновить JSON-schema
   секцию промпта.
2. Решение 2 (альтернативное): оставить промпт 200, ужесточить
   `ClampedReason` до 200. Риск: clamp 12% trades = потеря части
   reasoning в БД.

**Recommended.** Решение 1 — поднять prompt-limit до 300 (matches
ClampedReason post-fix 2026-05-25). Никакого risk-of-data-loss.

**Acceptance.**
- 0 расхождений между prompt и schema (grep на `≤200`/`max_length=200`
  vs `≤300`/`max_length=300` — single source of truth).

**Compliance.**
- `strategy-guard.mdc`: документация-only правка (Решение 1) — не
  торговая логика. Quasi-bugfix.
- `no-data-fitting.mdc`: 300 chars **уже валидировано** реальными
  responses 12 дней (max 297 — без data corruption).

---

## E. Loss-aversion bias на собственных закрытых-в-минус сделках

**Идея пользователя (2026-05-29).** После Фаз 0-2 бот стал чаще и
дисциплинированнее закрывать/держать позиции и видит свою историю в
SELF-REFLECTION. Риск: ИИ начнёт «бояться» направлений/инструментов,
по которым у него есть закрытия в минус — даже если это были
нормальные стоп-ауты по правилам, а не ошибки стратегии. Это
loss-aversion / disposition effect (Kahneman & Tversky 1979; Shefrin
& Statman 1985): убытки воспринимаются ~2× болезненнее эквивалентной
прибыли, что ведёт к избеганию валидных сетапов.

**Почему это НЕ полностью покрыто текущими механизмами.**
- COLD-START rule (2026-05-28) решает обратную проблему — `n=0`
  (нет истории), а не `n>0 с минусом` (есть негативная история).
- Regime-change cutoff (2026-05-28) отсекает pre-Phase-1 сделки, но
  post-cutoff минусы будут накапливаться и снова могут «пугать».
- SELF-REFLECTION подаёт агрегаты WR/PnL без контекста «был ли это
  валидный стоп или реальная ошибка тезиса».

**Гипотеза fix (требует данных, НЕ делать сейчас).**
1. В self-reflection разделять закрытия: `SL-hit / thesis-broken /
   locked-profit / noise-close` — чтобы стоп-ауты по правилам не
   читались как «провал направления».
2. Возможно — показывать не сырой WR, а expectancy (R-multiple),
   чтобы серия мелких валидных стопов + редкий крупный профит не
   выглядела как «убыточное направление».
3. Явный prompt-guard: «закрытие по SL ≠ ошибка входа; не избегай
   валидного сетапа из-за прошлых стоп-аутов».

**Compliance.**
- `strategy-guard.mdc`: меняет reasoning-контекст → требует одобрения.
- `sample-size.mdc` / `no-data-fitting.mdc`: fix должен опираться на
  фактические post-Phase-2 closed trades (≥ порогов), а не на интуицию.
  **Сначала собрать данные** после Фаз 0-2, потом проектировать.

**Когда брать.** После завершения Фазы 3 и накопления выборки
post-Phase-2 (по правилу sample-size). Зафиксировано по просьбе
пользователя «сперва заверши все фазы, потом учесть».

**Частично закрыто (2026-05-29, clean slate).** По запросу пользователя
сдвинут regime-change cutoff на деплой Phase 0–3
(`stats_window_start = 2026-05-29T08:26`). Все pre-Phase-0 минусы
(закрытия по 1H-шуму до Review Guardian) убраны из self-reflection →
острая фаза loss-aversion на «уже исправленном» поведении снята, бот
стартует с чистого листа (cold-start по всем парам). Это НЕ отменяет
структурный fix (пункты 1–3 выше): post-cutoff минусы снова начнут
накапливаться, и без разделения `SL-hit / thesis-broken / noise` риск
вернётся. Пункт остаётся открытым — взять после накопления
post-Phase-0–3 выборки. См. BUILDLOG_AI_FX_TRADER.md 2026-05-29.

---

## Когда возвращаться к Phase 2

После достижения Phase 1 acceptance (≥30 closed trades, доля
`thesis_status="intact"` close ≤30%) — взять задачи в **этом порядке**:

1. ~~**D1 (DXY)**~~ — **DONE 2026-05-27**.
2. **D2 (sentiment obligatory)** — bug-fix bypass.
3. **D3 (reason length)** — документация-only.
4. **C (review noise-guard)** — самое инвазивное, в конце.

C можно частично отложить если Phase 1 показал, что persistent thesis
discipline **сама** сократила short-duration close < 10%. Тогда C
становится избыточным (LLM уже не закрывает быстро по шуму).
