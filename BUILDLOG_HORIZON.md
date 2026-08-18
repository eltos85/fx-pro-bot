# Build Log — daytrend-bot и swing-bot

Изолированы от `scalp_bot` / `flowzone` / advisor. Bybit linear, демо.

## 2026-08-18

### feat(horizon): два бота — дневной SMA200 и свинг 4h SMA 20/50
`c610dab`

Пользователь попросил рассмотреть свинг и запустить два бота по дневному
тренду и свингу.

**Дневной тренд на часах (ORB) не берём.** `scripts/scalp_daytrend_research.py`,
2 года 15м, VIP 0 0.110%: UTC ORB 15м n=647 ср −0.17% CI без нуля, обе
половины минус, медиана удержания 1.2ч, итог −67% на BTC. NY ORB шум около
нуля. Запускать это — торговать заведомо убыточное правило.

**Свинг рассмотрен, стат. гейт не пройден.** `scripts/scalp_swing_research.py`,
2 года: 4h Turtle 20/10, 10/5, SMA 20/50 и Daily SMA50. Удержание 1.8–6.2 дня
(это свинг), но IS/OOS знак разошёлся на всех правилах с n≥30. Край не
заявляется. На демо запускаем канон SMA 20/50 4h (Murphy 1999), чтобы копить
форвард, не подкручивая окна.

**Что запущено.** `daytrend-bot`: SMA200 daily long/flat (Murphy 1999),
артефакт `scripts/scalp_vip0_trend_research.py` (5 лет BTC +192% vs B&H +70%,
просадка −32% vs −67%). `swing-bot`: SMA20/50 на 4h. Оба: BTC+ETH, 15% equity
на символ, 1x, taker, чужие позиции scalp на том же демо не трогают.

**Файлы:** `src/horizon_bot/`, `Dockerfile.horizon-bot`, `docker-compose.yml`,
`tests/test_horizon_bot.py`, `STRATEGY_RATIONALE_HORIZON.md`,
`scripts/scalp_daytrend_research.py`, `scripts/scalp_swing_research.py`.
