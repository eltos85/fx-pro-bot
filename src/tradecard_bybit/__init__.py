"""tradecard_bybit — advisory-ревьюер над данными детерминированных Bybit-ботов.

Покрывает ``scalp_bot`` (orderflow sweep/density) и ``flowzone_bot`` (auction /
volume-profile). Читает БД ботов **строго read-only**, считает SMB-style report
card, грейдит сделки по полю ``score``, гоняет 5 Why через DeepSeek и отдаёт
рекомендации человеку. Он **НЕ** меняет пороги/правила/конфиг ботов и **НИЧЕГО**
не пишет в их БД (см. TASKSPEC_TRADECARD_BYBIT.md, STRATEGY_TRADECARD_BYBIT.md;
правила no-data-fitting / sample-size / strategy-guard / stats-collection).

Пакет самостоятелен и НЕ импортирует из ``src/tradecard_fx/`` (изоляция §9).
"""
from __future__ import annotations

__version__ = "0.1.0"
