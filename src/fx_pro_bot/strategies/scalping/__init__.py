"""Скальпинг- и swing-стратегии для FxPro.

Активные стратегии (по результатам 2-летнего backtest, см. STRATEGIES.md):

| Strategy       | Instruments  | TF  | Edge (OOS)            |
|----------------|--------------|-----|-----------------------|
| `gold_orb`     | GC=F         | M5  | +6146 pips за 90d     |
| `squeeze_h4`   | GC=F, BZ=F   | H4  | +10799 / +1606 pips   |
| `turtle_h4`    | GC=F, BZ=F   | H4  | +7320 / +1539 pips    |
| `gbpjpy_fade`  | GBPJPY       | M5  | +1332 pips (WFO OOS)  |

Архивные (deprecated, не использовать):
- `session_orb`, `vwap_reversion`, `stat_arb` → `strategies/_archive/`
  (убыточны на 90d/2y backtest, см. BUILDLOG.md).
"""

from fx_pro_bot.strategies.scalping.gbpjpy_fade import GbpjpyFadeStrategy
from fx_pro_bot.strategies.scalping.gold_orb import GoldOrbStrategy
from fx_pro_bot.strategies.scalping.squeeze_h4 import SqueezeH4Strategy
from fx_pro_bot.strategies.scalping.turtle_h4 import TurtleH4Strategy

__all__ = [
    "GoldOrbStrategy",
    "SqueezeH4Strategy",
    "TurtleH4Strategy",
    "GbpjpyFadeStrategy",
]
