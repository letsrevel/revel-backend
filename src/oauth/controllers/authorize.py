"""Headless consent endpoints consumed by the frontend ``/oauth/authorize`` page (spec §8.2)."""

from django.http import HttpRequest
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from oauth import schema
from oauth.service import authorize_service


def _render(
    result: authorize_service.AuthorizeDescription | authorize_service.AuthorizeRedirect,
) -> schema.AuthorizeResponse:
    """Map a service result onto the wire schema the frontend consumes.

    Args:
        result: Either a redirect the browser must follow or a consent description.

    Returns:
        The response body.
    """
    if isinstance(result, authorize_service.AuthorizeRedirect):
        return schema.AuthorizeRedirectResponse(redirect_to=result.redirect_to)
    return schema.AuthorizeDescribeResponse(
        application=schema.AuthorizeAppSchema.from_app(result.application),
        scopes=schema.AuthorizeScopeSchema.rows(result.scopes),
        redirect_uri=result.redirect_uri,
        state=result.state,
    )


@api_controller("/oauth", auth=I18nJWTAuth(), tags=["OAuth"], throttle=UserDefaultThrottle())
class OAuthAuthorizeController(UserAwareController):
    def _request(self) -> HttpRequest:
        """The Django request behind this call.

        ``ControllerBase`` types both ``context`` and ``context.request`` as optional because
        a controller can be instantiated outside a route; on a routed call ninja-extra always
        sets them. Narrowed here rather than silenced with an ignore (R-65), so an unrouted
        call fails with a message instead of an ``AttributeError`` deep inside oauthlib.

        Returns:
            The request.

        Raises:
            RuntimeError: The controller was invoked outside a request.
        """
        request = self.context.request if self.context is not None else None
        if request is None:  # pragma: no cover - ninja-extra always sets it on a routed call
            raise RuntimeError("OAuthAuthorizeController was invoked outside a request.")
        return request

    @route.get(
        "/authorize",
        url_name="oauth_authorize_describe",
        response=schema.AuthorizeResponse,
    )
    def describe(self) -> schema.AuthorizeResponse:
        """Validate the client's authorization request and say what happens next.

        Returns either the consent screen's contents or, when no interaction is needed, the
        URL the browser must be sent to.
        """
        return _render(authorize_service.describe(self._request(), self.user()))

    @route.post(
        "/authorize",
        url_name="oauth_authorize_decide",
        response=schema.AuthorizeRedirectResponse,
        throttle=WriteThrottle(),
    )
    def decide(self, payload: schema.AuthorizeDecisionPayload) -> schema.AuthorizeRedirectResponse:
        """Record the user's decision and return where the browser must go next.

        A decision always ends in a redirect — a refusal becomes the client's
        ``access_denied`` response — so this route never returns a consent description.
        """
        result = authorize_service.decide(self._request(), allow=payload.allow)
        return schema.AuthorizeRedirectResponse(redirect_to=result.redirect_to)
