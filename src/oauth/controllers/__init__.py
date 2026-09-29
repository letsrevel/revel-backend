from .apps import OAuthAppController
from .authorize import OAuthAuthorizeController
from .connections import OAuthConnectionController
from .scopes import OAuthScopeController

OAUTH_CONTROLLERS: list[type] = [
    OAuthAuthorizeController,
    OAuthAppController,
    OAuthConnectionController,
    OAuthScopeController,
]

__all__ = [
    "OAUTH_CONTROLLERS",
    "OAuthAppController",
    "OAuthAuthorizeController",
    "OAuthConnectionController",
    "OAuthScopeController",
]
