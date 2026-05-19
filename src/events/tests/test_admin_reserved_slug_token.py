"""ReservedSlugToken admin registration + admin bypass for organization names."""

import pytest
from django.contrib import admin

from accounts.models import RevelUser
from events.models import Organization, ReservedSlugToken

pytestmark = pytest.mark.django_db


def test_admin_is_registered() -> None:
    assert ReservedSlugToken in admin.site._registry


def test_admin_path_bypasses_reserved_token_guard() -> None:
    # Simulate admin save_model: direct ORM create, no service involvement.
    owner = RevelUser.objects.create_user(
        username="staffuser", email="staff@example.com", password="x", email_verified=True
    )
    org = Organization.objects.create(
        name="Test Internal Org",
        owner=owner,
        contact_email="staff@example.com",
        visibility=Organization.Visibility.STAFF_ONLY,
    )
    assert org.pk is not None
    assert org.name == "Test Internal Org"
