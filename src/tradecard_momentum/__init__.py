"""tradecard_momentum — advisory-ревьюер бота fx_momentum_bot (cTrader/FxPro).

Самостоятельный пакет (НЕ импортирует tradecard_bybit). Читает БД momentum-бота
read-only (traceability сигналов) и cTrader deal-list (ground truth по P&L),
строит SMB-style report card, грейдит сделки по силе сигнала, гоняет 5 Why через
DeepSeek и отдаёт рекомендации человеку. НЕ меняет стратегию/пороги momentum-бота
и НЕ пишет в его БД (advisory-only, strategy-guard.mdc).
"""
