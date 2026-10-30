"""Admin-created rows are attributed to the admin when created_by is left empty."""

import typing as t

import pytest
from django.contrib.admin.sites import site

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


def test_new_blacklist_entry_defaults_created_by_to_the_admin(
    rf: t.Any, superuser: RevelUser, organization: models.Organization
) -> None:
    request = rf.post("/")
    request.user = superuser
    entry = models.Blacklist(organization=organization, email="blocked@example.com")

    site._registry[models.Blacklist].save_model(request, entry, form=None, change=False)

    entry.refresh_from_db()
    assert entry.created_by == superuser


def test_editing_keeps_an_empty_created_by(rf: t.Any, superuser: RevelUser, organization: models.Organization) -> None:
    entry = models.Blacklist.objects.create(organization=organization, email="blocked@example.com")
    request = rf.post("/")
    request.user = superuser

    site._registry[models.Blacklist].save_model(request, entry, form=None, change=True)

    entry.refresh_from_db()
    assert entry.created_by is None  # a creator deleted later stays unattributed on edits
