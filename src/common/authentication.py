import logging
import typing as t

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from ninja_jwt.authentication import JWTAuth

logger = logging.getLogger(__name__)


class OptionalAuth(JWTAuth):
    def __call__(self, request: HttpRequest) -> t.Any | None:
        """Overrides JWTAuth __call__ to provide optional auth."""
        headers = request.headers
        auth_value = headers.get(self.header)
        if not auth_value:
            request.user = AnonymousUser()
            return request.user
        parts = auth_value.split(" ")

        if parts[0].lower() != self.openapi_scheme:
            if settings.DEBUG:
                logger.error(f"Unexpected auth - '{auth_value}'")
            return None
        token = " ".join(parts[1:])
        return self.authenticate(request, token)
