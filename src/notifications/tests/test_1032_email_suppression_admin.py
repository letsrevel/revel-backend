"""Admin for the email suppression list (#1032): per-org complaint triage and clearing."""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization
from notifications.models import EmailSuppression
from notifications.service.email_policy import suppress

pytestmark = pytest.mark.django_db

Reason = EmailSuppression.Reason
Source = EmailSuppression.Source


@pytest.fixture(autouse=True)
def _use_simple_staticfiles(settings: t.Any) -> None:
    """Use plain StaticFilesStorage so admin templates don't need a manifest."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(django_user_model: type[RevelUser]) -> Client:
    superuser = django_user_model.objects.create_superuser(
        username="admin@example.com", email="admin@example.com", password="admin-password"
    )
    client = Client()
    client.force_login(superuser)
    return client


def test_per_org_complaint_filter(admin_client: Client, organization: Organization) -> None:
    suppress("c1@example.com", Reason.COMPLAINT, Source.PROVIDER, organization_id=organization.id)
    suppress("c2@example.com", Reason.COMPLAINT, Source.PROVIDER, organization_id=organization.id)
    suppress("b1@example.com", Reason.HARD_BOUNCE, Source.PROVIDER, organization_id=organization.id)
    suppress("c3@example.com", Reason.COMPLAINT, Source.PROVIDER)

    url = reverse("admin:notifications_emailsuppression_changelist")
    response = admin_client.get(
        url, {"reason__exact": Reason.COMPLAINT, "organization__id__exact": str(organization.id)}
    )

    assert response.status_code == 200
    assert response.context["cl"].result_count == 2


def test_search_by_email(admin_client: Client) -> None:
    suppress("findme@example.com", Reason.HARD_BOUNCE, Source.PROVIDER)
    suppress("other@example.com", Reason.HARD_BOUNCE, Source.PROVIDER)

    url = reverse("admin:notifications_emailsuppression_changelist")
    response = admin_client.get(url, {"q": "findme"})

    assert response.status_code == 200
    assert response.context["cl"].result_count == 1


def test_delete_clears_suppression(admin_client: Client) -> None:
    row = suppress("cleared@example.com", Reason.HARD_BOUNCE, Source.PROVIDER)

    url = reverse("admin:notifications_emailsuppression_delete", args=[row.pk])
    response = admin_client.post(url, {"post": "yes"})

    assert response.status_code == 302
    assert not EmailSuppression.objects.exists()
