"""Signing/verification keys, so SECRET_KEY can be rotated without invalidating live tokens.

Sign with :func:`signing_key`; verify against :func:`verification_keys` (the current key
followed by ``SECRET_KEY_FALLBACKS``). Unsubscribe links get their own optional key list
(``UNSUBSCRIBE_SIGNING_KEYS``) because they must outlive any fallback window.

Reads settings only, so it is safe to import from anywhere.
"""

import typing as t

import jwt
from django.conf import settings

__all__ = [
    "decode_jwt",
    "signing_key",
    "unsubscribe_signing_key",
    "unsubscribe_verification_keys",
    "verification_keys",
]


def signing_key() -> str:
    """The key new tokens and signatures are made with."""
    return str(settings.SECRET_KEY)


def verification_keys() -> list[str]:
    """Every key a token/signature may have been made with, newest first."""
    return [signing_key(), *settings.SECRET_KEY_FALLBACKS]


def unsubscribe_signing_key() -> str:
    """The key unsubscribe tokens are made with (the main key unless dedicated keys are set)."""
    return str(settings.UNSUBSCRIBE_SIGNING_KEYS[0]) if settings.UNSUBSCRIBE_SIGNING_KEYS else signing_key()


def unsubscribe_verification_keys() -> list[str]:
    """Every key an unsubscribe token may have been made with."""
    return list(settings.UNSUBSCRIBE_SIGNING_KEYS) or verification_keys()


def decode_jwt(token: str, keys: list[str], **kwargs: t.Any) -> dict[str, t.Any]:
    """``jwt.decode`` trying each key in turn.

    Only a signature mismatch moves on to the next key; any other failure (expired, wrong
    audience, malformed) is raised at once, so a rotation never masks a real error.

    Args:
        token: The encoded JWT.
        keys: Candidate keys, newest first.
        **kwargs: Passed to ``jwt.decode`` (``algorithms``, ``audience``, ...).

    Returns:
        The decoded claims.

    Raises:
        jwt.InvalidSignatureError: No key verified the signature (the last failure).
    """
    last: jwt.InvalidSignatureError | None = None
    for key in keys:
        try:
            return jwt.decode(token, key, **kwargs)
        except jwt.InvalidSignatureError as e:
            last = e
    raise last or jwt.InvalidSignatureError("Signature verification failed")
