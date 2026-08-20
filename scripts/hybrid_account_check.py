"""На каком счёте окажется hybrid_bot: read-only проверка позиций и баланса.

Нужна, чтобы понять, будет ли новый бот делить позицию с другими ботами
(STRATEGY_HYBRID.md §18.4). Запускается внутри контейнера бота — берёт ключи из
его окружения. Ничего не отправляет на биржу, только читает.

    ssh root@204.168.149.140 "docker exec -i <контейнер> python3 - PREFIX" \
      < scripts/hybrid_account_check.py

PREFIX — префикс env-переменных с ключами (SCALP, FLOWZONE, HYBRID, SWING...).
"""

from __future__ import annotations

import os
import sys

SYMBOLS = ("ETHUSDT", "BTCUSDT", "SOLUSDT")


def main() -> int:
    prefix = (sys.argv[1] if len(sys.argv) > 1 else "HYBRID").upper()
    key = os.environ.get(f"{prefix}_BYBIT_API_KEY", "")
    secret = os.environ.get(f"{prefix}_BYBIT_API_SECRET", "")
    if not key or not secret:
        print(f"{prefix}: ключей в окружении нет")
        return 1
    demo = os.environ.get(f"{prefix}_BYBIT_DEMO", "true").lower() in (
        "1", "true", "yes")

    from pybit.unified_trading import HTTP
    sess = HTTP(demo=demo, api_key=key, api_secret=secret, recv_window=20000)

    # Хвост ключа — чтобы сравнить счета между ботами, не раскрывая сам ключ.
    print(f"{prefix}: ключ …{key[-4:]}, demo={demo}")

    wallet = sess.get_wallet_balance(accountType="UNIFIED")
    for acc in wallet.get("result", {}).get("list") or []:
        for coin in acc.get("coin") or []:
            if coin.get("coin") == "USDT":
                print(f"  баланс USDT: equity={float(coin.get('equity') or 0):,.2f} "
                      f"available={float(coin.get('availableToWithdraw') or 0) or 0:,.2f}")

    for sym in SYMBOLS:
        resp = sess.get_positions(category="linear", symbol=sym)
        rows = resp.get("result", {}).get("list") or []
        if not rows:
            print(f"  {sym}: позиции нет")
            continue
        for p in rows:
            size = float(p.get("size") or 0)
            if size <= 0:
                print(f"  {sym}: позиции нет")
                continue
            print(f"  {sym}: {p.get('side')} {size:.4f} по средней "
                  f"{float(p.get('avgPrice') or 0):.2f}, "
                  f"нереализовано {float(p.get('unrealisedPnl') or 0):+,.2f} $")
    return 0


if __name__ == "__main__":
    sys.exit(main())
