"""Tests for the ExternalIdentity model and its Google backfill."""

import typing as t

import pytest
from django.core.exceptions import ValidationError

from accounts.models import ExternalIdentity, RevelUser

pytestmark = pytest.mark.django_db


def test_identity_links_to_user(user: RevelUser) -> None:
    identity = ExternalIdentity.objects.create(user=user, provider="google", subject="123", email=user.email)
    assert list(user.external_identities.all()) == [identity]
    assert str(identity) == f"google:123 → {user.email}"


def test_provider_subject_unique(user: RevelUser, revel_user_factory: t.Any) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="123")
    other = revel_user_factory.create_user()
    # TimeStampedModel.save runs full_clean, so the unique constraint surfaces as ValidationError.
    with pytest.raises(ValidationError):
        ExternalIdentity.objects.create(user=other, provider="google", subject="123")


def test_user_provider_unique(user: RevelUser) -> None:
    """One identity per provider per account (a rotated ``sub`` replaces the row in the service layer)."""
    ExternalIdentity.objects.create(user=user, provider="google", subject="123")
    with pytest.raises(ValidationError):
        ExternalIdentity.objects.create(user=user, provider="google", subject="456")


def test_same_subject_different_provider_allowed(user: RevelUser) -> None:
    ExternalIdentity.objects.create(user=user, provider="google", subject="123")
    ExternalIdentity.objects.create(user=user, provider="keycloak", subject="123")
    assert user.external_identities.count() == 2


def test_google_backfill_is_idempotent(user: RevelUser) -> None:
    """The data migration copies GoogleSSOUser rows once; re-running it is a no-op."""
    from importlib import import_module

    from django.apps import apps
    from django_google_sso.models import GoogleSSOUser

    migration = import_module("accounts.migrations.0033_backfill_external_identity_from_google_sso")
    GoogleSSOUser.objects.create(user=user, google_id="g-1")
    migration.forwards(apps, None)
    migration.forwards(apps, None)
    assert ExternalIdentity.objects.filter(provider="google", subject="g-1", user=user, email=user.email).count() == 1
