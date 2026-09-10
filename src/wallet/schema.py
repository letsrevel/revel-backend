"""Schemas for wallet pass endpoints."""

import enum

from ninja import Schema


class GoogleWalletSaveUrlSchema(Schema):
    """JSON shape of the Google Wallet save link (``?format=json``)."""

    save_url: str


class WalletPassErrorCode(str, enum.Enum):
    """Stable discriminators for wallet 503s.

    Both rails answer 503 for two different situations: the deployment has no
    wallet credentials at all (a plain ``HttpError``, no ``code``), and the
    credentials are there but the pass could not be signed or built. The
    frontend renders "not configured" for the former, so the latter needs a
    machine-readable marker — renaming a value is a breaking change, rewording
    the message is always safe.
    """

    PASS_UNAVAILABLE = "wallet_pass_unavailable"


class WalletPassErrorSchema(Schema):
    """503 body for a wallet pass that could not be signed or generated.

    ``detail`` is translated and must never be matched on; ``code`` is the
    discriminator the frontend keys on to tell a transient generation failure
    apart from the un-``code``d "Wallet is not configured" 503.
    """

    detail: str
    code: WalletPassErrorCode
