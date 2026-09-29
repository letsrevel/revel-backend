"""DOT's oauthlib bridge, with the userinfo URI made absolute for the RFC 8707 audience check."""

import typing as t

from django.http import HttpRequest
from oauth2_provider.oauth2_backends import OAuthLibCore
from oauthlib.oauth2 import OAuth2Error


class RevelOAuthLibCore(OAuthLibCore):  # type: ignore[misc]
    """``OAuthLibCore`` whose userinfo response audience-checks the absolute request URI.

    Upstream bug (#1004; removal tracked in #1005): DOT 3.4.1 absolutizes the URI in
    ``verify_request`` because a resource-bound token is prefix-matched against it, but
    ``create_userinfo_response`` passes the relative path from ``_extract_params``. ``/o/userinfo``
    never matches ``https://api…``, so every client that sends ``resource`` (as the developer guide
    tells them to) was refused there. This applies upstream's own ``verify_request`` fix, and
    nothing else, so it can be deleted as soon as a release does the same.

    Behind a TLS-terminating proxy ``build_absolute_uri`` needs ``SECURE_PROXY_SSL_HEADER`` to see
    ``https``, exactly as upstream's ``verify_request`` does (``revel.settings.base``).
    """

    def create_userinfo_response(self, request: HttpRequest) -> tuple[t.Any, t.Any, t.Any, t.Any]:
        """DOT 3.4.1's implementation plus the ``build_absolute_uri`` that ``verify_request`` applies.

        Args:
            request: The Django request for ``/o/userinfo``.

        Returns:
            DOT's ``(uri, headers, body, status)`` tuple.
        """
        uri, http_method, body, headers = self._extract_params(request)
        uri = request.build_absolute_uri(uri)
        try:
            headers, body, status = self.server.create_userinfo_response(uri, http_method, body, headers)
            uri = headers.get("Location", None)
            return uri, headers, body, status
        except OAuth2Error as exc:
            return None, exc.headers, exc.json, exc.status_code
