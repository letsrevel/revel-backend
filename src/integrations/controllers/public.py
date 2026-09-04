"""Unauthenticated endpoints the *provider* calls: OAuth callback and webhook receiver."""

import typing as t

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect
from ninja import Query
from ninja_extra import ControllerBase, api_controller, route

from common.throttling import AnonDefaultThrottle
from integrations.exceptions import IntegrationError
from integrations.schema import IntegrationErrorCode
from integrations.service import connection_service, webhook_service
from integrations.service.state import (
    CONNECT_STATE_COOKIE,
    CONNECT_STATE_COOKIE_PATH,
    state_matches_cookie,
    validate_state,
)

logger = structlog.get_logger(__name__)


def _settings_url(slug: str | None, **params: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = (
        f"{settings.FRONTEND_BASE_URL}/org/{slug}/settings/integrations"
        if slug
        else f"{settings.FRONTEND_BASE_URL}/org"
    )
    return f"{base}?{query}"


@api_controller("/integrations", auth=None, tags=["Integrations"], throttle=AnonDefaultThrottle())
class IntegrationsPublicController(ControllerBase):
    """Browser mid-navigation on the callback → always redirect; webhook → always JSON."""

    @route.get("/{provider}/callback", url_name="integration_callback", include_in_schema=False, response=None)
    def callback(
        self,
        request: HttpRequest,
        provider: str,
        code: t.Annotated[str | None, Query(max_length=2048)] = None,
        state: t.Annotated[str | None, Query(max_length=2048)] = None,
        error: t.Annotated[str | None, Query(max_length=128)] = None,
    ) -> HttpResponseRedirect:
        """Provider redirect target. Every failure becomes ``?error=<code>`` on the settings page.

        The ``try/except`` below is a deliberate, approved exemption from the project's "no
        try/except in controllers" rule: the browser is already mid-navigation on this GET, so
        every failure path must still resolve to a redirect rather than a JSON error page — and
        the redirect target needs the organization slug, which only this view can resolve (from
        the signed ``state``) before choosing where to send the browser. That is also why the
        exemption lives here instead of in a shared exception handler, unlike ``oidc_callback``
        (``accounts.controllers.auth``), whose handler doesn't need any per-request context to
        pick its redirect target.

        This view runs under ``ATOMIC_REQUESTS``, so ``connection_service.complete_connect``'s
        token exchange and webhook registration execute inside the request transaction — the
        same trade-off the Stripe webhook endpoints make. A failure here rolls back the
        ``PlatformConnection`` row along with it, which is the outcome we want: a half-connected
        row must never survive a failed callback.
        """
        slug: str | None = None
        try:
            if not state:
                raise IntegrationError(IntegrationErrorCode.STATE_INVALID, "missing state")
            payload = validate_state(state)
            from events.models import Organization

            slug = Organization.objects.filter(id=payload.organization_id).values_list("slug", flat=True).first()
            if error:
                logger.info("integration_connect_denied", provider=provider, error=error)
                raise IntegrationError(IntegrationErrorCode.PROVIDER_REJECTED, error)
            if not code or not state_matches_cookie(request.COOKIES.get(CONNECT_STATE_COOKIE), state):
                logger.warning("integration_state_cookie_mismatch", provider=provider)
                raise IntegrationError(IntegrationErrorCode.STATE_INVALID, "state/cookie mismatch")
            conn = connection_service.complete_connect(state, code)
            flag = "connected" if conn.status == conn.Status.ACTIVE else "select"
            response = HttpResponseRedirect(_settings_url(slug, **{flag: provider}))
        except IntegrationError as e:
            response = HttpResponseRedirect(_settings_url(slug, error=e.code.value))
        response.delete_cookie(CONNECT_STATE_COOKIE, path=CONNECT_STATE_COOKIE_PATH)
        return response

    @route.post(
        "/{provider}/webhook/{secret}",
        url_name="integration_webhook",
        include_in_schema=False,
        response={200: dict[str, t.Any]},
    )
    def webhook(self, request: HttpRequest, provider: str, secret: str) -> dict[str, t.Any]:
        """Record the delivery and answer fast. Body is untrusted; nothing is fetched here (spec §8)."""
        webhook_service.record_delivery(provider, secret, request)
        return {}
