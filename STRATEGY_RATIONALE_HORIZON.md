# Стратегии horizon_bot: daytrend и swing

Два изолированных контейнера на Bybit linear, демо, long/flat, без плеча.
Минутный ORB сюда не входит: `scripts/scalp_daytrend_research.py` закрыл
UTC/NY коробку (−67% за 2 года на BTC, VIP 0).

| Бот | Правило | ТФ | Источник | Типичный холд |
|---|---|---|---|---|
| `daytrend-bot` | close > SMA(200) → long, иначе кэш | D | Murphy 1999 | недели–месяцы |
| `swing-bot` | SMA(20) > SMA(50) → long, иначе кэш | 4h | Murphy 1999 | ~2–7 дней |

Параметры окон не тюнятся (strategy-guard, no-data-fitting).

**Сверка.** Daytrend: `scripts/scalp_vip0_trend_research.py`, 5 лет BTC,
+192% vs B&H +70%, просадка лучше. Swing: `scripts/scalp_swing_research.py`,
2 года, IS/OOS не сошёлся — форвард на демо, край не заявляется.

Исполнение: сигнал на закрытом баре, вход/выход рынком (taker 0.055% × 2).
Размер: 15% equity на символ, 1x. Чужую позицию на общем счёте не трогает.
