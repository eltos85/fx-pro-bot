"""Выбор счёта: брокерский (не ИИС) для торговых советов."""
from __future__ import annotations

from typing import Any

# https://tinkoff.github.io/investAPI/users/#accounttype
ACCOUNT_TYPE_TINKOFF = 1
ACCOUNT_TYPE_TINKOFF_IIS = 2


def _normalize_account_type(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    s = str(raw or "")
    if "IIS" in s:
        return ACCOUNT_TYPE_TINKOFF_IIS
    if "TINKOFF" in s:
        return ACCOUNT_TYPE_TINKOFF
    return 0


def account_type_name(raw: Any) -> str:
    t = _normalize_account_type(raw)
    if t == ACCOUNT_TYPE_TINKOFF_IIS:
        return "ИИС"
    if t == ACCOUNT_TYPE_TINKOFF:
        return "Брокерский"
    return f"type={raw}"


def pick_brokerage_account(
    accounts: list[dict[str, Any]],
    *,
    preferred_id: str = "",
) -> dict[str, Any]:
    """Вернуть брокерский счёт; preferred_id — если задан и найден."""
    if preferred_id:
        for a in accounts:
            if a.get("id") == preferred_id:
                return a
        raise ValueError(f"Счёт {preferred_id} не найден в GetAccounts")

    open_broker = [
        a
        for a in accounts
        if _normalize_account_type(a.get("type")) == ACCOUNT_TYPE_TINKOFF
        and a.get("status") in ("ACCOUNT_STATUS_OPEN", 2, "2")
    ]
    if open_broker:
        return open_broker[0]

    open_any = [
        a for a in accounts if a.get("status") in ("ACCOUNT_STATUS_OPEN", 2, "2")
    ]
    if open_any:
        return open_any[0]

    raise ValueError("Нет открытых счетов в Tinkoff Invest API")


def format_accounts_list(accounts: list[dict[str, Any]]) -> str:
    lines = []
    for a in accounts:
        lines.append(
            f"• {a.get('name', '?')} | id={a.get('id')} | "
            f"{account_type_name(a.get('type'))} | {a.get('status', '?')}"
        )
    return "\n".join(lines) if lines else "(пусто)"
