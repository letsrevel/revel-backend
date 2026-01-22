"""Testing middleware for system testing mode."""

import typing as t

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from common.testing import get_and_clear_test_tokens, get_header_name


class TestTokenMiddleware:
    """Middleware that exposes tokens in response headers during system testing.

    When SYSTEM_TESTING=True, tokens generated during the request (verification,
    password reset, deletion) are added as X-Test-*-Token headers to the response.

    This allows automated tests to retrieve tokens directly from HTTP responses
    instead of relying on email delivery via Mailpit.
    """

    def __init__(self, get_response: t.Callable[[HttpRequest], HttpResponse]) -> None:
        """Initialize middleware.

        Args:
            get_response: Django middleware get_response callable
        """
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process request and add test token headers if applicable.

        Args:
            request: Django HttpRequest

        Returns:
            HttpResponse with test token headers if SYSTEM_TESTING is enabled
        """
        response = self.get_response(request)

        if getattr(settings, "SYSTEM_TESTING", False):
            tokens = get_and_clear_test_tokens()
            for token_type, token in tokens.items():
                header_name = get_header_name(token_type)
                response[header_name] = token

        return response
