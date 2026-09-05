"""Tests for pulling the IdP ``picture`` claim into ``RevelUser.profile_picture`` on account creation."""

import typing as t
import uuid
from unittest.mock import patch

import httpx
import pytest
from django.core.files.base import ContentFile

from accounts.models import ExternalIdentity, RevelUser
from accounts.service import oidc
from accounts.service.oidc import OIDCClaims
from accounts.tasks import fetch_oidc_profile_picture
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db

GOOGLE = OIDCProviderConfig(
    key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
)
PICTURE_URL = "https://lh3.googleusercontent.com/a/ACg8ocK=s96-c"


def claims(**overrides: t.Any) -> OIDCClaims:
    base: dict[str, t.Any] = {
        "sub": "sub-1",
        "email": "alice@example.com",
        "email_verified": True,
        "picture": PICTURE_URL,
    }
    return OIDCClaims(**{**base, **overrides})


@pytest.fixture
def image_server(monkeypatch: pytest.MonkeyPatch, png_bytes: bytes) -> list[httpx.Request]:
    """Serve ``png_bytes`` for any https URL; records the requests made."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=png_bytes, headers={"Content-Type": "image/png"})

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def _serve(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


# --- dispatch from _resolve_user ---


def test_new_account_schedules_picture_fetch(django_capture_on_commit_callbacks: t.Any) -> None:
    with (
        patch("accounts.tasks.profile_picture.fetch_oidc_profile_picture.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        user = oidc._resolve_user(GOOGLE, claims())
    delay.assert_called_once_with(user_id=str(user.id), url=PICTURE_URL)


def test_new_account_without_picture_claim_does_not_fetch(django_capture_on_commit_callbacks: t.Any) -> None:
    with (
        patch("accounts.tasks.profile_picture.fetch_oidc_profile_picture.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        oidc._resolve_user(GOOGLE, claims(picture=None))
    delay.assert_not_called()


def test_linking_existing_account_does_not_fetch(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """Only a freshly created account gets the IdP picture; an existing one keeps whatever it has (or hasn't)."""
    with (
        patch("accounts.tasks.profile_picture.fetch_oidc_profile_picture.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        oidc._resolve_user(GOOGLE, claims(email=user.email))
    delay.assert_not_called()


def test_returning_identity_does_not_fetch(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="sub-1")
    with (
        patch("accounts.tasks.profile_picture.fetch_oidc_profile_picture.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        oidc._resolve_user(GOOGLE, claims())
    delay.assert_not_called()


# --- fetch_profile_picture (service) ---


def test_fetch_saves_image_as_profile_picture(user: RevelUser, image_server: list[httpx.Request]) -> None:
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert user.profile_picture
    assert user.profile_picture.name.endswith(".png")


def test_fetch_requests_google_picture_at_preview_size(user: RevelUser, image_server: list[httpx.Request]) -> None:
    """Google's claim URL is a 96px square; ask for 400px so the preview thumbnail isn't upscaled."""
    oidc.fetch_profile_picture(user, PICTURE_URL)
    assert [str(r.url) for r in image_server] == ["https://lh3.googleusercontent.com/a/ACg8ocK=s400-c"]


def test_fetch_leaves_non_google_url_untouched(user: RevelUser, image_server: list[httpx.Request]) -> None:
    oidc.fetch_profile_picture(user, "https://idp.example.test/avatars/alice.png?size=96")
    assert [str(r.url) for r in image_server] == ["https://idp.example.test/avatars/alice.png?size=96"]


def test_fetch_refuses_non_https_url(user: RevelUser, image_server: list[httpx.Request]) -> None:
    oidc.fetch_profile_picture(user, "http://idp.example.test/avatars/alice.png")
    assert image_server == []
    user.refresh_from_db()
    assert not user.profile_picture


def test_fetch_skips_when_user_already_has_picture(
    user: RevelUser, png_bytes: bytes, image_server: list[httpx.Request]
) -> None:
    """The user uploaded a picture between account creation and the task running: never overwrite it."""
    user.profile_picture.save("mine.png", ContentFile(png_bytes), save=True)
    before = user.profile_picture.name
    oidc.fetch_profile_picture(user, PICTURE_URL)
    assert image_server == []
    user.refresh_from_db()
    assert user.profile_picture.name == before


def test_fetch_skips_non_2xx(user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, httpx.Response(404))
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert not user.profile_picture


def test_fetch_skips_non_image_content_type(user: RevelUser, monkeypatch: pytest.MonkeyPatch, png_bytes: bytes) -> None:
    _serve(monkeypatch, httpx.Response(200, content=png_bytes, headers={"Content-Type": "text/html"}))
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert not user.profile_picture


def test_fetch_skips_oversized_body(user: RevelUser, monkeypatch: pytest.MonkeyPatch, png_bytes: bytes) -> None:
    monkeypatch.setattr(oidc, "PICTURE_MAX_BYTES", len(png_bytes) - 1)
    _serve(monkeypatch, httpx.Response(200, content=png_bytes, headers={"Content-Type": "image/png"}))
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert not user.profile_picture


def test_fetch_skips_bytes_that_are_not_an_image(user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lying Content-Type must not get garbage into an ImageField."""
    _serve(monkeypatch, httpx.Response(200, content=b"not a png", headers={"Content-Type": "image/png"}))
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert not user.profile_picture


def test_fetch_transport_error_is_swallowed(user: RevelUser, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(oidc, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    oidc.fetch_profile_picture(user, PICTURE_URL)
    user.refresh_from_db()
    assert not user.profile_picture


# --- task ---


def test_task_fetches_for_user(user: RevelUser, image_server: list[httpx.Request]) -> None:
    fetch_oidc_profile_picture.delay(user_id=str(user.id), url=PICTURE_URL)
    user.refresh_from_db()
    assert user.profile_picture


def test_task_unknown_user_is_noop(image_server: list[httpx.Request]) -> None:
    fetch_oidc_profile_picture.delay(user_id=str(uuid.uuid4()), url=PICTURE_URL)
    assert image_server == []
