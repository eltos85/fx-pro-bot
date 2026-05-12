"""Diag-скрипт: разбор всех LLM-decisions fx-ai-trader.

Запуск:
    docker exec fx-pro-bot-advisor-1 python /data/_diag2.py

Скрипт временный, для разовой проверки LLM-поведения в Phase 1.
"""
from __future__ import annotations

import json
import sqlite3
import time


def _short(s: str | None, n: int = 80) -> str:
    if not s:
        return ""
    return s.replace("\n", " ")[:n]


c = sqlite3.connect("file:/data/fx_ai_trader.sqlite?mode=ro", uri=True)

print("=== ВСЕ decisions fx-ai-trader (по убыванию времени) ===")
rows = list(
    c.execute(
        "SELECT id, ts, cycle_type, parsed_action, executed, error, "
        "tokens_input, tokens_output, cost_usd "
        "FROM decisions ORDER BY id DESC"
    )
)
print(f"Всего: {len(rows)}")
print()

n_full = sum(1 for r in rows if r[2] == "full")
n_review = sum(1 for r in rows if r[2] == "review")
n_executed = sum(1 for r in rows if r[4])
n_errors = sum(1 for r in rows if r[5])
total_cost = sum((r[8] or 0) for r in rows)
print(
    f"by type: full={n_full}, review={n_review} | "
    f"executed={n_executed}, errors={n_errors} | "
    f"total cost ${total_cost:.4f}"
)
print()

# Группировка ошибок по типу.
err_types: dict[str, int] = {}
for r in rows:
    e = r[5] or ""
    if not e:
        continue
    if "risk_usd" in e:
        err_types["risk_usd > limit"] = err_types.get("risk_usd > limit", 0) + 1
    elif "R:R" in e:
        err_types["R:R < 1.5"] = err_types.get("R:R < 1.5", 0) + 1
    elif "direction" in e:
        err_types["wrong SL/TP direction"] = err_types.get("wrong SL/TP direction", 0) + 1
    elif "parse" in e.lower():
        err_types["parse_error"] = err_types.get("parse_error", 0) + 1
    else:
        err_types["other"] = err_types.get("other", 0) + 1

print("=== Ошибки по типам ===")
for k, v in sorted(err_types.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")

print()
print("=== Recent decisions (последние 15) ===")
for r in rows[:15]:
    action = ""
    try:
        if r[3]:
            j = json.loads(r[3])
            action = (
                f"{j.get('action'):5s} {j.get('symbol','?'):6s} {j.get('side','?'):4s} "
                f"lots={j.get('volume_lots','?'):>5} "
                f"SL={j.get('stop_loss','?'):>8} TP={j.get('take_profit','?'):>8}"
            )
    except Exception:
        action = _short(r[3], 50)
    err = _short(r[5], 70)
    print(f"  id={r[0]:>3} {r[1][:19]} {r[2]:6s} exec={r[4]} {action}")
    if err:
        print(f"         err: {err}")
