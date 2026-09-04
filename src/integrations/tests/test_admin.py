"""Admin permission tests: the integrations admin is read-only end to end."""

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.models import RevelUser
from integrations.admin import PlatformConnectionAdmin
from integrations.models import PlatformConnection

pytestmark = pytest.mark.django_db


def test_platform_connection_admin_is_read_only(superuser: RevelUser) -> None:
    """Even a superuser cannot add, change, or delete a ``PlatformConnection`` in the admin."""
    admin_instance = PlatformConnectionAdmin(PlatformConnection, AdminSite())
    request = RequestFactory().get("/")
    request.user = superuser
    assert admin_instance.has_add_permission(request) is False
    assert admin_instance.has_change_permission(request) is False
    assert admin_instance.has_delete_permission(request) is False
