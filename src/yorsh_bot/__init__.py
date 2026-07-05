"""yorsh_bot — изолированный сканер «ёрш»-паттернов на MEXC/Bitget spot.

Фаза 1 (это ТЗ): data-only коллектор + сканер повторяющихся прострелов от
genuine density на низколиквидных спот-парах MEXC и Bitget. **Без торговли**.
Торговое исполнение (Фаза 3) практически недостижимо по числовому критерию
аудита (см. docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md) и в это ТЗ не входит.

ИЗОЛЯЦИЯ (strategy-guard.mdc): этот пакет НЕ импортирует ничего из
``fx_pro_bot.*``, ``fx_ai_trader.*``, ``scalp_bot.*``, ``flowzone_bot.*``,
``fx_momentum_bot.*`` и наоборот. Самостоятельная экосистема. Модуля
``trading/`` нет вообще (не «выключен флагом», а отсутствует) — Фаза 1
data-only.

Родительские документы:
- docs/RESEARCH_SCAM_TOKEN_SCALP.md — исходная стратегия
- docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md — аудит реализуемости, фазы, критерии
- docs/TZ_YORSH_SCANNER.md — это ТЗ
"""

__version__ = "0.1.0"
