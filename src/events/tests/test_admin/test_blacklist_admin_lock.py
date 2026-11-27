"""A blacklist entry saved in the Django admin takes the organization lock first."""

import typing as t

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Blacklist, Organization

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings: t.Any) -> None:
    """Admin pages render static URLs; the hashed manifest storage has no manifest in tests."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(superuser: RevelUser) -> Client:
    client = Client()
    client.force_login(superuser)
    return client


def test_admin_save_locks_the_organization_before_the_insert(admin_client: Client, organization: Organization) -> None:
    data = {
        "organization": str(organization.pk),
        "email": "admin-added@example.com",
        "reason": "admin",
    }

    with CaptureQueriesContext(connection) as ctx:
        response = admin_client.post(reverse("admin:events_blacklist_add"), data)

    assert response.status_code == 302, response.content[:500]
    assert Blacklist.objects.filter(organization=organization, email="admin-added@example.com").exists()
    sql = [q["sql"] for q in ctx.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "FOR NO KEY UPDATE" in q and '"events_organization"' in q)
    insert = next(i for i, q in enumerate(sql) if q.startswith('INSERT INTO "events_blacklist"'))
    assert lock < insert
