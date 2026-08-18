# solana-bot: импульс щитков Solana

Отдельный пакет и контейнер. Не клеится к impulse/scalp.

| Правило | Значение | Источник |
|---|---|---|
| Скринер | GeckoTerminal trending Solana, срез 5м | Teletype lexdollar + офдок CoinGecko onchain |
| Объём | ≥$100k / 5 мин | Teletype lexdollar |
| Ход | ≥5% за 5 мин | операционный пол «щиток уже пошёл» (в посте цели, не вход) |
| Цели | +7% тейк, кап +30% | Teletype +7…+30% |
| Стоп | −12% | риск-кап: в источнике стопа нет |
| Ликвидность / возраст | ≥$25k, ≥30 мин | риск-кап против мгновенного rug |
| Исполнение | Jupiter Swap API v2 `/order` + `/execute` | https://developers.jup.ag/docs/swap/order-and-execute |

`SOLANA_TRADING_ENABLED` по умолчанию **false**: на VPS может не быть
кошелька. Скан крутится без ключа. `solders` только в `Dockerfile.solana-bot`
(`[solana]` extra), не в общих deps репозитория.
