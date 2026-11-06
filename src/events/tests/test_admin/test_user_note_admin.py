"""Admin for private organizer notes (#1314): non-identifying repr, author recorded server-side."""

import typing as t

import pytest
from django.contrib.admin.models import LogEntry
from django.test import override_settings
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization, OrganizationUserNote

pytestmark = pytest.mark.django_db


def test_str_does_not_identify_subject(organization: Organization, member_user: RevelUser) -> None:
    note = OrganizationUserNote.objects.create(organization=organization, user=member_user, body="x")
    text = str(note)
    assert str(note.pk) in text
    assert str(member_user.id) not in text
    assert str(organization.id) not in text


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
def test_admin_sets_updated_by_and_logs_non_identifying_repr(
    admin_client: t.Any, admin_user: RevelUser, organization: Organization, member_user: RevelUser
) -> None:
    response = admin_client.post(
        reverse("admin:events_organizationusernote_add"),
        {"organization": organization.pk, "user": member_user.pk, "body": "hello", "updated_by": member_user.pk},
    )
    assert response.status_code == 302, response.content
    note = OrganizationUserNote.objects.get(organization=organization, user=member_user)
    assert note.updated_by == admin_user  # forged form value ignored (read-only), set from the request
    entry = LogEntry.objects.get(object_id=str(note.pk))
    assert str(member_user.id) not in entry.object_repr
    assert member_user.email not in entry.object_repr
