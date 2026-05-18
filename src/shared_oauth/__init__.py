"""shared_oauth — клиент для ctrader-token-service, используется обоими ботами.

Допустимый общий код между fx_pro_bot и fx_ai_trader: это
**infrastructure**, не торговая логика (см. правило strategy-guard.mdc:
импорт fx_pro_bot.* из fx_ai_trader.* разрешён для infra).
"""
