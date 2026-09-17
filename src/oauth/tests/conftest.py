"""OAuth-specific fixtures.

Only fixtures the OAuth provider needs live here; everything else (``user``,
``organization``, the user factories) is inherited from the root ``src/conftest.py``
and must never be shadowed.
"""

import typing as t
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken
from pytest_django.fixtures import Settings

from accounts.models import RevelUser

if t.TYPE_CHECKING:
    from oauth.models import OAuthApplication


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    """One RSA-2048 key per test session (generation is ~100 ms)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def oauth_provider(settings: Settings, tmp_path: Path, rsa_private_key_pem: str) -> None:
    """Switch the provider on for every test in this package.

    DOT reloads its settings on Django's ``setting_changed`` signal, so assigning
    ``settings.OAUTH2_PROVIDER`` is enough to take effect.
    """
    key_path = tmp_path / "oidc.pem"
    key_path.write_text(rsa_private_key_pem)
    settings.OIDC_SIGNING_KEY_PATH = str(key_path)
    settings.OAUTH_ISSUER = "http://testserver"
    settings.FRONTEND_BASE_URL = "http://frontend.test"
    settings.OAUTH2_PROVIDER = {
        **settings.OAUTH2_PROVIDER,
        "OIDC_ENABLED": True,
        "DCR_ENABLED": True,
        "OIDC_RSA_PRIVATE_KEY": rsa_private_key_pem,
        "OIDC_ISS_ENDPOINT": "http://testserver",
        "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": "http://testserver",
        "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": ["http://testserver"],
    }


@pytest.fixture
def oauth_app(user: RevelUser) -> "OAuthApplication":
    """A manually registered confidential app owned by ``user`` with every scope allowed."""
    from oauth.models import OAuthApplication
    from oauth.scopes import SCOPES

    app = OAuthApplication(
        user=user,
        name="Test App",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        redirect_uris="https://app.example/cb",
        allowed_scopes=sorted(SCOPES),
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    app.full_clean()
    app.save()
    return app


@pytest.fixture
def public_oauth_app(user: RevelUser) -> "OAuthApplication":
    """A manually registered public (PKCE-only) app."""
    from oauth.models import OAuthApplication
    from oauth.scopes import SCOPES

    app = OAuthApplication(
        user=user,
        name="Public App",
        client_type=OAuthApplication.CLIENT_PUBLIC,
        client_secret="",
        redirect_uris="https://app.example/cb http://127.0.0.1/cb",
        allowed_scopes=sorted(SCOPES),
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    app.full_clean()
    app.save()
    return app


@pytest.fixture
def session_client(user: RevelUser) -> Client:
    """A test client authenticated with a normal session JWT for ``user``.

    The root ``revel_user_factory`` leaves ``email_verified`` False; the OAuth
    controllers require a verified email, so verify it here. It stays the same
    ``user`` object the app fixtures are owned by.
    """
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def oauth_event(organization: t.Any) -> t.Any:
    """A minimal event owned by the root ``organization`` (whose owner is ``user``).

    The ``events`` app's own ``event`` fixture is not visible from this package and
    hangs off a differently shaped ``organization``; see R-47. Keep this minimal.
    """
    from django.utils import timezone

    from events.models import Event

    return Event.objects.create(
        organization=organization,
        name="OAuth Event",
        slug="oauth-event",
        start=timezone.now(),
    )
