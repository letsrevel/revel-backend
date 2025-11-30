"""Apple Wallet Pass Configuration.

See: https://developer.apple.com/documentation/walletpasses
"""

from decouple import config

APPLE_WALLET_PASS_TYPE_ID: str = config("APPLE_WALLET_PASS_TYPE_ID", default="")
APPLE_WALLET_TEAM_ID: str = config("APPLE_WALLET_TEAM_ID", default="")
APPLE_WALLET_CERT_PATH: str = config("APPLE_WALLET_CERT_PATH", default="")
APPLE_WALLET_KEY_PATH: str = config("APPLE_WALLET_KEY_PATH", default="")
APPLE_WALLET_KEY_PASSWORD: str = config("APPLE_WALLET_KEY_PASSWORD", default="")
APPLE_WALLET_WWDR_CERT_PATH: str = config("APPLE_WALLET_WWDR_CERT_PATH", default="")
