"""Per-app exception → HTTP mapping, installed from ``OauthConfig.ready``."""

import typing as t

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.responses import Response

from common.authentication import InvalidBearerToken
from common.exception_handlers import ExceptionHandler, make_static_handler, register_handlers
from oauth.exceptions import (
    AppLimitReachedError,
    AuthorizationRequestError,
    InsufficientScopeError,
    OAuthProviderDisabledError,
)


def _authorization_request_error(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render the RFC 6749 error code alongside the human-readable description."""
    err = t.cast(AuthorizationRequestError, exc)
    return Response(status=400, data={"detail": err.description, "error": err.error})


def _invalid_bearer_token(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render the RFC 6750 §3.1 / RFC 9728 challenge for a refused app token.

    ninja-extra would already render the 401 body, but not the header, which is what
    lets an MCP client discover where to get a token.
    """
    err = t.cast(InvalidBearerToken, exc)
    response = Response(status=401, data={"detail": str(err.detail)})
    response["WWW-Authenticate"] = err.www_authenticate
    return response


def _insufficient_scope(request: HttpRequest, exc: Exception | t.Type[Exception]) -> Response:
    """Render the RFC 6750 §3.1 challenge, naming the missing scope when there is one."""
    err = t.cast(InsufficientScopeError, exc)
    response = Response(status=403, data={"detail": str(_("This app was not granted the required scope."))})
    challenge = 'Bearer error="insufficient_scope"'
    if err.scope:
        challenge += f', scope="{err.scope}"'
    response["WWW-Authenticate"] = challenge
    return response


HANDLERS: dict[type[Exception], ExceptionHandler] = {
    OAuthProviderDisabledError: make_static_handler(404, _("Not found.")),
    AuthorizationRequestError: _authorization_request_error,
    InvalidBearerToken: _invalid_bearer_token,
    InsufficientScopeError: _insufficient_scope,
    AppLimitReachedError: make_static_handler(409, _("You have reached the maximum number of apps.")),
}


def register() -> None:
    """Install the oauth handlers on the global Ninja API."""
    from api.api import api

    register_handlers(api, HANDLERS)
