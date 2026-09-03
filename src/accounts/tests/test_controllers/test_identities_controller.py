"""Tests for GET/DELETE /api/account/identities."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import ExternalIdentity, RevelUser
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _settings(settings: t.Any) -> None:
    settings.OIDC_PROVIDERS = (
        OIDCProviderConfig(
            key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
        ),
    )


def test_list_identities(auth_client: Client, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1", email=user.email)
    ExternalIdentity.objects.create(user=user, provider="old-idp", subject="2", email="")
    data = auth_client.get(reverse("api:identities_list")).json()
    assert [(d["provider"], d["provider_name"], d["email"]) for d in data] == [
        ("google", "Google", user.email),
        ("old-idp", "Old-Idp", ""),
    ]
    assert "created_at" in data[0]


def test_list_requires_auth(client: Client) -> None:
    assert client.get(reverse("api:identities_list")).status_code == 401


def test_unlink(auth_client: Client, user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    response = auth_client.delete(reverse("api:identities_unlink", kwargs={"provider": "google"}))
    assert response.status_code == 204
    assert not user.external_identities.exists()


def test_unlink_stranded_400(auth_client: Client, user: RevelUser) -> None:
    user.set_unusable_password()
    user.save(update_fields=["password"])
    ExternalIdentity.objects.create(user=user, provider="google", subject="1")
    response = auth_client.delete(reverse("api:identities_unlink", kwargs={"provider": "google"}))
    assert response.status_code == 400
    assert set(response.json()) == {"detail"}


def test_unlink_unknown_404(auth_client: Client) -> None:
    response = auth_client.delete(reverse("api:identities_unlink", kwargs={"provider": "google"}))
    assert response.status_code == 404
    assert set(response.json()) == {"detail"}


def test_unlink_provider_key_is_bounded(auth_client: Client) -> None:
    assert auth_client.delete(reverse("api:identities_unlink", kwargs={"provider": "Google"})).status_code == 422


def test_cannot_unlink_other_users_identity(auth_client: Client, revel_user_factory: t.Any) -> None:
    other = revel_user_factory.create_user()
    ExternalIdentity.objects.create(user=other, provider="google", subject="1")
    assert auth_client.delete(reverse("api:identities_unlink", kwargs={"provider": "google"})).status_code == 404
    assert other.external_identities.exists()
