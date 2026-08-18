"""Jupiter Swap API v2: GET /order + POST /execute.

Офдок: https://developers.jup.ag/docs/swap/order-and-execute
Base: https://api.jup.ag/swap/v2
Keyless 0.5 RPS без x-api-key — https://developers.jup.ag/docs/portal/rate-limits
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("solana_bot.jupiter")

_BASE = "https://api.jup.ag/swap/v2"


def _headers(api_key: str) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def order(*, input_mint: str, output_mint: str, amount: int, taker: str,
          slippage_bps: int, api_key: str = "") -> dict | None:
    try:
        resp = requests.get(
            f"{_BASE}/order",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "taker": taker,
                "slippageBps": str(slippage_bps),
            },
            headers=_headers(api_key),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("jupiter /order")
        return None
    if not data.get("transaction"):
        log.warning("jupiter /order без tx: %s", data.get("errorMessage"))
        return None
    return data


def execute(*, signed_tx_b64: str, request_id: str,
            api_key: str = "") -> dict:
    try:
        resp = requests.post(
            f"{_BASE}/execute",
            json={"signedTransaction": signed_tx_b64, "requestId": request_id},
            headers={**_headers(api_key), "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.exception("jupiter /execute")
        return {"ok": False, "error": str(e)}
    if data.get("status") != "Success":
        return {"ok": False, "error": data.get("error") or data}
    return {"ok": True, "result": data}
