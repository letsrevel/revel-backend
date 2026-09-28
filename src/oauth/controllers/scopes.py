"""The scope vocabulary with its consent labels, for the developer portal and Connected Apps.

Plain ``I18nJWTAuth()`` (R-73), like Connected Apps: an unverified user can hold grants and must
be able to read what they mean. Served from ``SCOPES`` so clients never keep a second copy of
the labels that could drift from the consent screen.
"""

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle
from oauth import schema
from oauth.permissions import ProviderEnabled
from oauth.scopes import SCOPES


@api_controller(
    "/oauth/scopes",
    auth=I18nJWTAuth(),
    tags=["OAuth - Scopes"],
    throttle=UserDefaultThrottle(),
    permissions=[ProviderEnabled()],
)
class OAuthScopeController(UserAwareController):
    @route.get("/", url_name="oauth_scopes_list", response=list[schema.AuthorizeScopeSchema])
    def list_scopes(self) -> list[schema.AuthorizeScopeSchema]:
        """List every scope with its translated consent label and group, in registry order."""
        return schema.AuthorizeScopeSchema.rows(list(SCOPES))
