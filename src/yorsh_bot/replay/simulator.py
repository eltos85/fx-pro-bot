"""Replay-симулятор exit-механизмов на записанной ленте (Фаза 2, M7).

Replay сырой ленты (raw/*.jsonl.gz), восстановление книги и трейдов в
хронологии, воспроизведение сигналов сканера point-in-time (без look-ahead).
Exit-машина: time-stop / density-routed limit exit / spoof-pull cancel /
kill-switch. Метрики: WR, EXP, PF, средний hold, tail-loss rate, **средний
slippage time-stop-выходов относительно mid** (ключевая метрика по аудиту),
net P&L после slippage и комиссий. M0: заглушка (Фаза 2 — после M6).
"""
from __future__ import annotations


def _stub() -> None:
    """M0 placeholder. M7: replay + exit engine + metrics."""
    return None
