"""Smoke tests for the read-only OrganizationNudge admin."""

import typing as t

import pytest
from django.test import override_settings
from django.urls import reverse

from events.models import Organization, OrganizationNudge

pytestmark = pytest.mark.django_db


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
def test_changelist_renders_and_is_linked_from_the_sidebar(admin_client: t.Any, organization: Organization) -> None:
    OrganizationNudge.objects.create(organization=organization, trigger=OrganizationNudge.Trigger.NO_EVENTS, sequence=1)
    url = reverse("admin:events_organizationnudge_changelist")

    resp = admin_client.get(url)

    assert resp.status_code == 200
    assert url in resp.content.decode()  # curated Unfold sidebar entry
    assert reverse("admin:events_organizationnudge_add") not in resp.content.decode()
