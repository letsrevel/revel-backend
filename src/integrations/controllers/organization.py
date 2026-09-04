"""Owner-only connection management, mounted under the organization-admin prefix."""

from django.conf import settings
from django.http import HttpResponse
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.schema import ResponseOk
from common.throttling import UserDefaultThrottle, WriteThrottle
from events.controllers.organization_admin.base import OrganizationAdminBaseController
from events.controllers.permissions import IsOrganizationOwner
from integrations import schema
from integrations.models import PlatformConnection
from integrations.service import connection_service
from integrations.service.state import CONNECT_STATE_COOKIE, CONNECT_STATE_COOKIE_PATH


def _to_schema(conn: PlatformConnection) -> schema.ConnectionSchema:
    rows = connection_service.list_connections(conn.organization)
    return next(r for r in rows if r.provider == conn.provider)


@api_controller(
    "/organization-admin/{slug}/integrations",
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=UserDefaultThrottle(),
    permissions=[IsOrganizationOwner()],
)
class OrganizationIntegrationsController(OrganizationAdminBaseController):
    """Connect, inspect, and disconnect external listing platforms (spec §6, §11)."""

    @route.get("", url_name="list_integrations", response=list[schema.ConnectionSchema])
    def list_integrations(self, slug: str) -> list[schema.ConnectionSchema]:
        """One row per enabled provider with its connection state."""
        return connection_service.list_connections(self.get_one(slug))

    @route.post(
        "/{provider}/connect",
        url_name="integration_connect",
        response=schema.ConnectStartSchema,
        throttle=WriteThrottle(),
    )
    def connect(self, slug: str, provider: str) -> HttpResponse:
        """Start the OAuth flow: returns the provider's authorize URL and binds the state to the browser."""
        organization = self.get_one(slug)
        start = connection_service.begin_connect(organization, self.user(), provider)
        response = self.create_response(schema.ConnectStartSchema(authorize_url=start.authorize_url), status_code=200)
        response.set_cookie(
            CONNECT_STATE_COOKIE,
            start.state,
            max_age=int(settings.INTEGRATIONS_CONNECT_STATE_TTL.total_seconds()),
            httponly=True,
            secure=not settings.DEBUG,
            samesite="Lax",
            path=CONNECT_STATE_COOKIE_PATH,
        )
        return response

    @route.get("/{provider}/accounts", url_name="integration_accounts", response=list[schema.RemoteAccountSchema])
    def accounts(self, slug: str, provider: str) -> list[schema.RemoteAccountSchema]:
        """Accounts available to a pending connection (multi-organization Eventbrite users)."""
        accounts = connection_service.list_pending_accounts(self.get_one(slug), provider)
        return [schema.RemoteAccountSchema(remote_id=a.remote_id, name=a.name) for a in accounts]

    @route.post(
        "/{provider}/select-account",
        url_name="integration_select_account",
        response=schema.ConnectionSchema,
        throttle=WriteThrottle(),
    )
    def select_account(self, slug: str, provider: str, payload: schema.SelectAccountSchema) -> schema.ConnectionSchema:
        """Bind one of the pending accounts and activate the connection."""
        return _to_schema(connection_service.select_account(self.get_one(slug), provider, payload.remote_id))

    @route.patch(
        "/{provider}", url_name="integration_update", response=schema.ConnectionSchema, throttle=WriteThrottle()
    )
    def update(self, slug: str, provider: str, payload: schema.ConnectionUpdateSchema) -> schema.ConnectionSchema:
        """Org-wide auto-sync default."""
        return _to_schema(connection_service.set_auto_sync(self.get_one(slug), provider, payload.auto_sync))

    @route.delete("/{provider}", url_name="integration_disconnect", response=ResponseOk, throttle=WriteThrottle())
    def disconnect(self, slug: str, provider: str) -> ResponseOk:
        """Unregister the webhook, revoke where the provider allows it, delete the connection."""
        connection_service.disconnect(self.get_one(slug), provider)
        return ResponseOk()
