"""Получение информации о топ-стратегиях из cTrader Copy.

cTrader Copy предоставляет публичный каталог стратегий (провайдеров),
доступный через веб-API. Модуль парсит этот каталог и предоставляет
отсортированный список лучших стратегий для отображения в логах бота.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

CTRADER_COPY_API = "https://ct-copy.fxpro.com/api/v1"


@dataclass(frozen=True, slots=True)
class StrategyInfo:
    """Краткая информация о стратегии из cTrader Copy."""

    name: str
    provider: str
    roi_pct: float
    copiers: int
    max_drawdown_pct: float
    risk_score: int
    url: str


class CTraderCopyClient:
    """Клиент для получения публичных данных cTrader Copy."""

    def __init__(self, api_url: str = CTRADER_COPY_API) -> None:
        self._api_url = api_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "fx-pro-bot/0.3"

    def top_strategies(self, limit: int = 10) -> list[StrategyInfo]:
        """Получить топ стратегий, отсортированных по ROI."""
        try:
            resp = self._session.get(
                f"{self._api_url}/strategies",
                params={
                    "sort": "roi",
                    "order": "desc",
                    "limit": str(limit),
                    "period": "3m",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            log.warning("cTrader Copy: не удалось загрузить стратегии, пробую fallback")
            return self._fallback_top(limit)

        strategies: list[StrategyInfo] = []
        for item in data.get("strategies", data.get("data", [])):
            try:
                strategies.append(
                    StrategyInfo(
                        name=item.get("name", "?"),
                        provider=item.get("provider", item.get("trader", "?")),
                        roi_pct=float(item.get("roi", item.get("return", 0))),
                        copiers=int(item.get("copiers", item.get("followers", 0))),
                        max_drawdown_pct=float(item.get("maxDrawdown", item.get("drawdown", 0))),
                        risk_score=int(item.get("riskScore", item.get("risk", 0))),
                        url=item.get("url", ""),
                    )
                )
            except (TypeError, ValueError):
                continue

        strategies.sort(key=lambda s: s.roi_pct, reverse=True)
        return strategies[:limit]

    def _fallback_top(self, limit: int) -> list[StrategyInfo]:
        """Fallback: пробуем альтернативный эндпоинт cTrader Copy."""
        try:
            resp = self._session.get(
                f"{self._api_url}/leaderboard",
                params={"limit": str(limit)},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            log.warning("cTrader Copy: fallback тоже не доступен")
            return []

        strategies: list[StrategyInfo] = []
        for item in data.get("items", data.get("data", [])):
            try:
                strategies.append(
                    StrategyInfo(
                        name=item.get("name", "?"),
                        provider=item.get("provider", "?"),
                        roi_pct=float(item.get("roi", 0)),
                        copiers=int(item.get("copiers", 0)),
                        max_drawdown_pct=float(item.get("maxDrawdown", 0)),
                        risk_score=int(item.get("riskScore", 0)),
                        url=item.get("url", ""),
                    )
                )
            except (TypeError, ValueError):
                continue

        return strategies[:limit]


def format_top_strategies(strategies: list[StrategyInfo], limit: int = 5) -> str:
    """Форматирование топ-стратегий для лога."""
    if not strategies:
        return "cTrader Copy: данные о топ-стратегиях недоступны"

    lines = ["Топ стратегий cTrader Copy:"]
    for i, s in enumerate(strategies[:limit], 1):
        dd = f", просадка {s.max_drawdown_pct:.1f}%" if s.max_drawdown_pct else ""
        lines.append(
            f"  {i}. {s.name} ({s.provider}) — ROI {s.roi_pct:+.1f}%, "
            f"{s.copiers} копирующих{dd}"
        )
    return "\n".join(lines)
