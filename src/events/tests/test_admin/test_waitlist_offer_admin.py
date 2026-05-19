"""Smoke tests for WaitlistOfferAdmin registration."""

import typing as t

import pytest
from django.contrib import admin
from django.test import override_settings
from django.urls import reverse

from events.models import WaitlistOffer

pytestmark = pytest.mark.django_db


def test_waitlist_offer_registered_in_admin() -> None:
    assert admin.site.is_registered(WaitlistOffer)


def test_waitlist_offer_admin_changelist_url_resolves() -> None:
    url = reverse("admin:events_waitlistoffer_changelist")
    assert url


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
def test_waitlist_offer_admin_changelist_renders(
    admin_client: t.Any,  # pytest-django built-in fixture
) -> None:
    url = reverse("admin:events_waitlistoffer_changelist")
    resp = admin_client.get(url)
    assert resp.status_code == 200
