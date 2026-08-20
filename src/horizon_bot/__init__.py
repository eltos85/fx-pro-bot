"""Два изолированных Bybit-бота длинного горизонта: daytrend и swing.

Не импортирует scalp_bot / hybrid_bot / fx_pro_bot (strategy-guard.mdc).
Два контейнера, две БД, разные orderLinkId. Торговая логика — канон,
без подбора окон.
"""

__version__ = "0.1.0"
