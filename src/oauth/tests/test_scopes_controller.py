"""Tests for the scope vocabulary at ``/api/oauth/scopes``."""

import typing as t

import pytest
from django.test.client import Client
from django.utils import translation

from accounts.models import RevelUser
from oauth.scopes import SCOPES

pytestmark = pytest.mark.django_db


def test_lists_every_scope_in_registry_order(session_client: Client) -> None:
    response = session_client.get("/api/oauth/scopes/")
    assert response.status_code == 200, response.content
    rows = response.json()
    assert [row["name"] for row in rows] == list(SCOPES)
    assert len(rows) == 14
    for row in rows:
        assert isinstance(row["label"], str) and row["label"]
        assert row["group"] == SCOPES[row["name"]].group


def test_labels_follow_the_user_language(session_client: Client, user: RevelUser) -> None:
    """Same gettext source as the consent screen, so the labels translate the same way."""
    user.language = "de"
    user.save(update_fields=["language"])
    rows = session_client.get("/api/oauth/scopes/").json()
    with translation.override("de"):
        expected = {name: str(scope.label) for name, scope in SCOPES.items()}
    assert {row["name"]: row["label"] for row in rows} == expected
    assert expected["email"] != "See your email address"


def test_anonymous_is_401(client: Client) -> None:
    assert client.get("/api/oauth/scopes/").status_code == 401


def test_disabled_provider_is_404(settings: t.Any, session_client: Client) -> None:
    settings.OIDC_SIGNING_KEY_PATH = ""
    assert session_client.get("/api/oauth/scopes/").status_code == 404
