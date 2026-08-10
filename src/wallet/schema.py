"""Schemas for wallet pass endpoints."""

from ninja import Schema


class GoogleWalletSaveUrlSchema(Schema):
    """JSON shape of the Google Wallet save link (``?format=json``)."""

    save_url: str
