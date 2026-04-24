"""Скальпинг-стратегии.

Активные:
- `gold_orb` — изолированный Opening Range Breakout на XAU/USD
  (см. STRATEGIES.md §3b-bis).

Архивные (deprecated, не использовать):
- `session_orb`, `vwap_reversion`, `stat_arb` → `strategies/_archive/`
  (убыточны на 90d backtest, см. BUILDLOG.md 2026-04-24).
"""

from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy

__all__ = ["GoldOrbStrategy"]
