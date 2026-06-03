# RU Stocks Analyst

Советник по акциям MOEX: портфель Tinkoff + **новости RSS** + **ИИ-аналитика** по заголовкам + техскринер swing 1–3 дня → Telegram. **Без автосделок.**

## Что внутри дайджеста

1. **Портфель** — Tinkoff Invest API  
2. **Новости** — RSS (РБК, Интерфакс, Коммерсантъ, Ведомости, ТАСС), привязка к вашим тикерам  
3. **ИИ-аналитика** — DeepSeek только по переданным заголовкам (не выдумывает новости)  
4. **Техскринер** — EMA/RSI/объём/ATR

## Быстрый старт

1. Токен API: [Т-Инвестиции → Настройки → Токен для API](https://www.tbank.ru/invest/settings/) (достаточно **только чтения**).

2. В `.env`:

```bash
RU_STOCKS_TINKOFF_TOKEN=your_token
RU_STOCKS_TELEGRAM_BOT_TOKEN=...
RU_STOCKS_TELEGRAM_CHAT_ID=...
# Опционально LLM-комментарий:
# RU_STOCKS_LLM_ENABLED=true
# DEEPSEEK_API_KEY=...
```

3. Узнать `account_id` брокерского счёта:

```bash
pip install -e .
ru-stocks-discover
# → RU_STOCKS_ACCOUNT_ID=...
```

4. Один тестовый дайджест:

```bash
ru-stocks-once
```

5. Постоянный цикл (каждый час + утренний дайджест 09:05 МСК):

```bash
ru-stocks-analyst
```

## Переменные

| Переменная | По умолчанию | Смысл |
|------------|--------------|--------|
| `RU_STOCKS_ACCOUNT_ID` | авто брокерский | Не ИИС |
| `RU_STOCKS_UNIVERSE_TOP_N` | 50 | Сколько тикеров грузить свечи |
| `RU_STOCKS_MIN_PRICE_RUB` | 50 | Отсечь дешёвые бумаги |
| `RU_STOCKS_POLL_INTERVAL_SEC` | 3600 | Пауза между циклами |
| `RU_STOCKS_USE_SANDBOX` | false | Песочница API |
| `RU_STOCKS_DRY_RUN` | false | Не слать в TG, только лог |

## API

REST Tinkoff Invest: https://tinkoff.github.io/investAPI/swagger-ui/

## Ограничения MVP

- Не инсайды и не инвестрекомендация — публичные данные + техправила.
- ИИС не используется для советов «купить/продать» (счёт — брокерский).
- Лимиты API: при `UNIVERSE_TOP_N` > 50 цикл дольше; не поднимать без нужды.
