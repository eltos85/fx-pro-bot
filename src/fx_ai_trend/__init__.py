"""FX AI Trend — LLM-агент в **trend-following** парадигме для торговли
gold (XAUUSD spot), Brent oil (BRENT) и natural gas (NG=F → NAT.GAS) на
cTrader FxPro demo.

Контраст с `fx_ai_trader` (Discretionary):
- ``fx_ai_trader`` — discretionary trader (Mark Douglas / Van Tharp /
  KenMacro): ждёт specific high-conviction setup'ов, маленькие позиции
  выше WR, sub-day holds, fade-the-extremes mindset.
- ``fx_ai_trend``  — trend-follower (Faith / Covel / Clenow / AQR
  Asness-Moskowitz): рулсы entry/exit жёстче, breakout-driven
  (20/55-day Donchian), 2N ATR stops, pyramid 0.5N, hold-the-trend
  weeks-months, ожидаемый WR 30-45% с асимметричным R:R.

Два бота торгуют **на одном** cTrader account (ctid=46883073), но имеют
**разные label**'ы: ``ai-fx-trader`` (discretionary) vs ``ai-fx-trend``
(trend-following). Broker-side изоляция полная — ни один бот не закроет
позиции другого, reconcile-фильтры срабатывают только на свой label.

Это enable'ит чистый A/B-эксперимент:
**Trend-follower vs Discretionary на одних и тех же инструментах
(XAUUSD + BRENT + NAT.GAS) в одной рыночной среде**.

Архитектурно бот скопирован с ``fx_ai_trader`` (та же инфраструктура:
``CTraderClient``, ``KillSwitch``, ``broker_reconcile``,
``ctrader-token-service``, sentiment-pydantic schema). Различия:
1. ``llm/prompts.py`` — полностью переписан под trend-following.
2. ``config/settings.py`` env-prefix ``AI_FX_TREND_*``.
3. ``state/db.py`` filename ``fx_ai_trend.sqlite``.
4. ``order_label = "ai-fx-trend"``.
5. Killswitch caps те же (broker-safety class), но trend-follower может
   накопить дольше unrealised drawdown — типичный CTA drawdown 20-30%
   на тренд (Clenow «Following the Trend»). Не reactionary disable.

Изоляция от:
- `fx_pro_bot/` (rule-based Advisor, отключён 2026-05-18 на VPS — см.
  BUILDLOG.md; код остаётся в репо).
- `fx_ai_trader/` (Discretionary LLM, активен).
- `bybit_bot/` / `ai_trader/` / `ai_arena/` (Bybit-экосистема).
"""
