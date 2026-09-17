from .authorize import OAuthAuthorizeController

OAUTH_CONTROLLERS: list[type] = [OAuthAuthorizeController]

__all__ = ["OAUTH_CONTROLLERS", "OAuthAuthorizeController"]
