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

# Google Wallet — fat-JWT save links, signed with a GCP service-account key.
# See: https://developers.google.com/wallet/tickets/events
GOOGLE_WALLET_ISSUER_ID: str = config("GOOGLE_WALLET_ISSUER_ID", default="")
GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH: str = config("GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH", default="")
# Namespaces class/object IDs under the shared issuer (env/version discriminator).
GOOGLE_WALLET_CLASS_PREFIX: str = config("GOOGLE_WALLET_CLASS_PREFIX", default="revel")
