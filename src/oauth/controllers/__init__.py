from .apps import OAuthAppController
from .authorize import OAuthAuthorizeController
from .connections import OAuthConnectionController

OAUTH_CONTROLLERS: list[type] = [OAuthAuthorizeController, OAuthAppController, OAuthConnectionController]

__all__ = [
    "OAUTH_CONTROLLERS",
    "OAuthAppController",
    "OAuthAuthorizeController",
    "OAuthConnectionController",
]
