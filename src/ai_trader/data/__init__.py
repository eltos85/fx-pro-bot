"""External macro data providers for AI-Trader (v0.30, 2026-05-28).

Содержит:
- ``macro_rates``: US rates feed (DXY + UST10Y) через yfinance — port из
  fx_ai_trader/data/macro_rates.py.
- ``crypto_macro``: BTC dominance + total crypto market cap через
  CoinGecko `/global` (free, no key required).

Оба модуля — read-only data fetchers с in-memory cache. Не торгуют, не
пишут в БД. Используются в ``trading/context.py`` для подачи в LLM
context (закрывает «hidden-disconnect» когда промпт ссылается на BTC.D
/ DXY, а context их не отдаёт — та же нестыковка #4 из FX-trader Phase 1
audit, BUILDLOG_AI_FX_TRADER.md 2026-05-26).
"""
