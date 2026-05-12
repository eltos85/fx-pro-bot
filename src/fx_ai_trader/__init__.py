"""FX AI Trader — LLM-агент для торговли gold (XAUUSD spot) и Brent oil (BRENT)
на cTrader FxPro demo.

Phase 1 (MVP): paper-mode, dual-timer (15+5 мин), DeepSeek-V4, free RSS + EIA macro.

Изолирован от:
- `fx_pro_bot/` (advisor торгует GC=F futures — другой инструмент, label="fx-pro-bot")
- `ai_trader/` (Bybit crypto-агент, отдельная экосистема)

См. план в `.cursor/plans/fx_ai_oil_trader_mvp_44cb1e89.plan.md`.
"""
