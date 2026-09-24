"""DOT's oauthlib bridge, with request URIs rooted at ``OAUTH_ISSUER`` (RFC 8707 audience check)."""

from django.conf import settings
from django.http import HttpRequest
from oauth2_provider.oauth2_backends import OAuthLibCore


class RevelOAuthLibCore(OAuthLibCore):  # type: ignore[misc]
    """``OAuthLibCore`` that hands oauthlib ``OAUTH_ISSUER + path`` instead of a Django-built URI.

    A resource-bound token is prefix-matched against the request URI. DOT builds that URI two
    ways, both wrong for us:

    * ``create_userinfo_response`` passes the *relative* path (DOT 3.4.1), so ``/o/userinfo``
      never matched ``https://api…`` and every well-behaved client got 401 there;
    * ``verify_request`` uses ``request.build_absolute_uri``, whose scheme depends on
      ``SECURE_PROXY_SSL_HEADER``. Beta and demo terminate TLS at Caddy without it, so Django saw
      ``http://`` and refused every ``https://``-bound token on the whole API.

    ``OAUTH_ISSUER`` is this API's public origin and the protected-resource identifier, so it is
    the right root whatever the proxy reports. ``build_absolute_uri`` leaves an absolute URI
    untouched, which is what makes this one override enough for both paths.
    """

    def _get_escaped_full_path(self, request: HttpRequest) -> str:
        """DOT's escaped path, prefixed with the issuer when one is configured.

        Args:
            request: The Django request being handed to oauthlib.

        Returns:
            ``OAUTH_ISSUER`` + the escaped path and query, or DOT's relative value without an issuer.
        """
        path: str = super()._get_escaped_full_path(request)
        issuer: str = settings.OAUTH_ISSUER
        return f"{issuer}{path}" if issuer else path
