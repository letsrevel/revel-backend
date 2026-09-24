"""DOT's oauthlib bridge, with the userinfo URI made absolute (RFC 8707 audience check)."""

import typing as t

from django.http import HttpRequest
from oauth2_provider.oauth2_backends import OAuthLibCore
from oauthlib.oauth2 import OAuth2Error


class RevelOAuthLibCore(OAuthLibCore):  # type: ignore[misc]
    """``OAuthLibCore`` whose userinfo response audience-checks the absolute request URI.

    DOT 3.4.1 absolutizes the URI in ``verify_request`` because a resource-bound token is
    prefix-matched against it, but ``create_userinfo_response`` still passes the relative path
    from ``_extract_params``. ``/o/userinfo`` never starts with ``https://api…``, so every token
    requested with ``resource`` (as the developer guide tells clients to) was refused there.
    """

    def create_userinfo_response(self, request: HttpRequest) -> tuple[t.Any, t.Any, t.Any, t.Any]:
        """DOT's implementation with ``build_absolute_uri`` applied, exactly as ``verify_request`` does.

        Args:
            request: The Django request for ``/o/userinfo``.

        Returns:
            DOT's ``(uri, headers, body, status)`` tuple.
        """
        # ponytail: a copy of DOT 3.4.1's body plus one line. Delete this class when an upstream
        # release absolutizes the URI here too (the userinfo test in test_flow.py will say so).
        uri, http_method, body, headers = self._extract_params(request)
        uri = request.build_absolute_uri(uri)
        try:
            headers, body, status = self.server.create_userinfo_response(uri, http_method, body, headers)
            uri = headers.get("Location", None)
            return uri, headers, body, status
        except OAuth2Error as exc:
            return None, exc.headers, exc.json, exc.status_code
