"""Подпись Jupiter-транзакции. solders только здесь, опциональный extra.

Ключ: base58 secret (Phantom export), как в
https://developers.jup.ag/docs/swap/order-and-execute (BS58_PRIVATE_KEY).
"""

from __future__ import annotations

import base64
import logging

log = logging.getLogger("solana_bot.wallet")


def available() -> bool:
    try:
        from solders.keypair import Keypair  # noqa: F401
        from solders.transaction import VersionedTransaction  # noqa: F401
        return True
    except ImportError:
        return False


def pubkey(secret_b58: str) -> str | None:
    try:
        from solders.keypair import Keypair
        return str(Keypair.from_base58_string(secret_b58).pubkey())
    except Exception:
        log.exception("keypair")
        return None


def sign_order_tx(secret_b58: str, tx_b64: str) -> str | None:
    """partial sign: MM JupiterZ допишет вторую подпись на /execute."""
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction

        kp = Keypair.from_base58_string(secret_b58)
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(tx.message, [kp])
        return base64.b64encode(bytes(signed)).decode()
    except Exception:
        log.exception("sign")
        return None
