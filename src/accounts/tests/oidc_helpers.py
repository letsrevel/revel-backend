"""Shared helpers for OIDC tests: an RSA key pair and an ID-token factory."""

import time
import typing as t

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
PUBLIC_KEY = _PRIVATE.public_key()


def make_id_token(
    *,
    issuer: str = "https://accounts.google.com",
    audience: str = "cid",
    nonce: str = "nonce",
    sub: str = "sub-1",
    email: str | None = "alice@example.com",
    email_verified: bool = True,
    alg: str = "RS256",
    key: t.Any = None,
    **extra: t.Any,
) -> str:
    """Sign an ID token with the module RSA key (or ``key``) for the given claims."""
    now = int(time.time())
    claims: dict[str, t.Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": sub,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email_verified": email_verified,
        "given_name": "Alice",
        "family_name": "Doe",
        "locale": "de-AT",
        **extra,
    }
    if email is not None:
        claims["email"] = email
    return pyjwt.encode(claims, key or PRIVATE_PEM, algorithm=alg, headers={"kid": "test-kid"})
