"""Connected apps (spec §8.4): what a user has authorized, and how they cut it off.

Plain ``I18nJWTAuth()`` on purpose (R-73): an unverified user can grant an app, so they must be
able to see and revoke that grant. Email verification gates *publishing* an app, not disowning
one.
"""

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from oauth import schema
from oauth.models import OAuthApplication
from oauth.permissions import ProviderEnabled
from oauth.service import token_service


@api_controller(
    "/oauth/connections",
    auth=I18nJWTAuth(),
    tags=["OAuth - Connected Apps"],
    throttle=UserDefaultThrottle(),
    permissions=[ProviderEnabled()],
)
class OAuthConnectionController(UserAwareController):
    @route.get("/", url_name="oauth_connections_list", response=list[schema.ConnectionSchema])
    def list_connections(self) -> list[schema.ConnectionSchema]:
        """List the apps you have authorized, most recently used first."""
        return [
            schema.ConnectionSchema(
                application=schema.AuthorizeAppSchema.from_app(connection.application),
                scopes=sorted(connection.scopes),
                first_authorized_at=connection.first_authorized_at,
                last_used_at=connection.last_used_at,
            )
            for connection in token_service.connections_for(self.user())
        ]

    @route.delete("/{client_id}", url_name="oauth_connections_revoke", response={204: None}, throttle=WriteThrottle())
    def revoke(self, client_id: str) -> tuple[int, None]:
        """Disconnect an app: every token, ID token and pending code you granted it dies.

        Both halves matter. ``authorize_service.has_prior_grant`` auto-approves the next
        authorization request when *either* a live access token or an unrevoked refresh token
        survives, so a partial revoke would hand the app a fresh grant with no consent screen
        (R-78); ``token_service`` is what makes this complete.
        """
        app = get_object_or_404(OAuthApplication, client_id=client_id)
        token_service.revoke_user_tokens(self.user(), application=app)
        return 204, None
