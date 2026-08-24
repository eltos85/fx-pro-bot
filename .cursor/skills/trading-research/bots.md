# Карта ботов

Канон стратегии и BUILDLOG должны быть от **того же** бота. Не тащи пороги из соседнего.

## Форекс / gold (cTrader)

| Бот | Код | Канон | Лог |
|---|---|---|---|
| advisor / fx_pro_bot | `src/fx_pro_bot/` | `STRATEGIES.md` | `BUILDLOG.md` |
| fx_ai_trader | `src/fx_ai_trader/` | промпты внутри модуля | `BUILDLOG_AI_FX_TRADER.md` |
| fx_momentum_bot | `src/fx_momentum_bot/` | `STRATEGIES.md` §8 | `BUILDLOG.md` |

## Крипта (Bybit / Solana)

| Бот | Код | Канон | Лог |
|---|---|---|---|
| scalp_bot | `src/scalp_bot/` | `STRATEGY_RATIONALE_SCALP.md` | `BUILDLOG_SCALP.md` |
| hybrid_bot | `src/hybrid_bot/` | `STRATEGY_HYBRID.md` | `BUILDLOG_HYBRID.md` |
| impulse_bot | `src/impulse_bot/` | `STRATEGY_RATIONALE_IMPULSE.md` | `BUILDLOG_IMPULSE.md` |
| horizon_bot | `src/horizon_bot/` | `STRATEGY_RATIONALE_HORIZON.md` | `BUILDLOG_HORIZON.md` |
| solana_bot | `src/solana_bot/` | `STRATEGY_RATIONALE_SOLANA.md` | `BUILDLOG_SOLANA.md` |
| tradecard_bybit | `src/tradecard_bybit/` | `STRATEGY_TRADECARD_BYBIT.md` | `BUILDLOG_TRADECARD_BYBIT.md` |
| tradecard_momentum | `src/tradecard_momentum/` | `STRATEGY_TRADECARD.md` | `BUILDLOG_TRADECARD_MOMENTUM.md` |

## Не крипта и не форекс

`ru_stocks_analyst` — не вызывай crypto-trader / forex-trader. Это другой рынок.
