# Build Log — Crypto Predictions Experiment

Отдельный лог эксперимента по предсказанию роста альткоинов на основе новостных,
инсайдерских и информационных ресурсов. Проверка через Bybit API.

**Не связано с боевой торговлей** — бот по этим гипотезам сделки не открывает.
Это AI-рисерч для оценки качества прогноза на горизонте 1–2 недели.

**Файлы эксперимента:**
- `PREDICTIONS.md` — тезисы, цены входа, цели, стопы.
- `scripts/check_predictions.py` — проверка через публичное Bybit API.
- `data/predictions_track.csv` — снимки цен по дням (append-only).

---

## 2026-04-18

### init: первая итерация предсказаний (8 long + 3 short-бета)

Сформирован `PREDICTIONS.md` с 8 long-гипотезами и 3 негативными ставками
(токены, которые, по моей оценке, НЕ должны вырасти >15% за 14 дней).

**Макро-контекст на 18.04.2026:**
- BTC $76,625, dominance 56–60% (topping out), Fear & Greed 8/100 (extreme fear).
- Stablecoin supply +$12B за 30 дней → $245B dry powder.
- Whale accumulation BTC 270k за 30 дней (максимум с 2013).
- Altcoin Season Index 27–35 — ещё BTC-season, но нарративные сектора растут.

**Long-гипотезы (цели на 02.05.2026):**

| # | Символ | Entry | Target | Stop | Upside | Уверенность |
|---|--------|-------|--------|------|--------|-------------|
| 1 | ENAUSDT    | 0.12367 | 0.17  | 0.098 | +37% | Высокая |
| 2 | PENDLEUSDT | 1.4461  | 1.95  | 1.25  | +35% | Высокая |
| 3 | ZECUSDT    | 334.48  | 420   | 285   | +26% | Высокая |
| 4 | HYPEUSDT   | 44.804  | 58    | 38    | +29% | Средняя |
| 5 | TAOUSDT    | 252.75  | 320   | 218   | +27% | Средняя |
| 6 | SUIUSDT    | 0.9876  | 1.25  | 0.85  | +27% | Средняя |
| 7 | RENDERUSDT | 1.8415  | 2.50  | 1.60  | +36% | Средняя |
| 8 | POPCATUSDT | 0.06469 | 0.095 | 0.055 | +47% | Низкая (спекул.) |

**Ключевые тезисы:**
- **AI/DePIN**: TAO (Grayscale ETF + Covenant-72B), RENDER (GPU, MACD positive).
- **Privacy + ETF**: ZEC (Grayscale S-3 filed, 30% supply в shielded pools).
- **DeFi + fee switch**: ENA (ожидается активация, USDe ≈ $5.88B / триггер $6B).
- **Perps infrastructure**: PENDLE (Boros $200M OI), HYPE (Hayes target $150).
- **L1 accumulation**: SUI (whale range $0.86–0.96, CME futures запущены).
- **Meme contra-Fear**: POPCAT — маленький размер, чистая спекуляция на отскок.

**Негативные ставки (должны остаться <+15%):**
- WLDUSDT ($0.2833) — Worldcoin, regulatory headwinds.
- ARBUSDT ($0.13059) — L2 нарратив мёртв, continual unlocks.
- JUPUSDT ($0.18547) — SOL ETF уже отыгран, JUP не получил institutional bid.

**Сценарии:**
- Base (65%): 4/8 long в цель, средний +12–18%.
- Bull (20%): BTC > $82k, 6+/8 в цель, средний +25–35%.
- Bear (15%): BTC < $72k, 5–6 стопов, средний -10 до -15%.

**Методология проверки:**
1. Baseline зафиксирован в PREDICTIONS.md по ценам Bybit linear perpetual.
2. `python3 scripts/check_predictions.py --save` — ежедневный снапшот в CSV.
3. `python3 scripts/check_predictions.py --verdict --since 2026-04-18` — финал
   с анализом high/low за период (срабатывание target/stop).
4. Финальная оценка 02.05.2026 (T+14).

**Файлы:** `PREDICTIONS.md`, `scripts/check_predictions.py`,
`BUILDLOG_PREDICTIONS.md`
