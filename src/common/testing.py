"""Testing utilities for system testing mode.

When SYSTEM_TESTING=True, tokens that would normally be sent via email
are exposed in response headers for easier automated testing.
"""

import typing as t
from contextvars import ContextVar

from django.conf import settings

# Context variable to store tokens during a request lifecycle
_test_tokens: ContextVar[dict[str, str]] = ContextVar("test_tokens", default={})


def store_test_token(token_type: str, token: str) -> None:
    """Store a token for inclusion in response headers.

    Only stores tokens when SYSTEM_TESTING is enabled.

    Args:
        token_type: Type of token (e.g., "verification", "password_reset", "deletion")
        token: The JWT token value
    """
    if not getattr(settings, "SYSTEM_TESTING", False):
        return

    tokens = _test_tokens.get()
    # Create a new dict to avoid mutating the default
    new_tokens = {**tokens, token_type: token}
    _test_tokens.set(new_tokens)


def get_and_clear_test_tokens() -> dict[str, str]:
    """Retrieve all stored tokens and clear the storage.

    Returns:
        Dictionary mapping token types to token values
    """
    tokens = _test_tokens.get()
    _test_tokens.set({})
    return tokens


# Token type constants for consistency
TOKEN_TYPE_VERIFICATION = "verification"
TOKEN_TYPE_PASSWORD_RESET = "password-reset"
TOKEN_TYPE_DELETION = "deletion"


def get_header_name(token_type: str) -> str:
    """Get the HTTP header name for a token type.

    Args:
        token_type: The token type constant

    Returns:
        HTTP header name (e.g., "X-Test-Verification-Token")
    """
    # Convert token_type to title case for header
    formatted = "-".join(word.title() for word in token_type.split("-"))
    return f"X-Test-{formatted}-Token"


__all__: t.Sequence[str] = [
    "store_test_token",
    "get_and_clear_test_tokens",
    "get_header_name",
    "TOKEN_TYPE_VERIFICATION",
    "TOKEN_TYPE_PASSWORD_RESET",
    "TOKEN_TYPE_DELETION",
]
