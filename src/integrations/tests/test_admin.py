"""Admin permission tests: the integrations admin is read-only end to end."""

import typing as t

import pytest
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.urls import reverse

from accounts.models import RevelUser
from integrations.admin import PlatformConnectionAdmin
from integrations.models import EventLink, ImportJob, PlatformConnection, TierLink, WebhookDelivery

pytestmark = pytest.mark.django_db


def test_platform_connection_admin_is_read_only(superuser: RevelUser) -> None:
    """Even a superuser cannot add, change, or delete a ``PlatformConnection`` in the admin."""
    admin_instance = PlatformConnectionAdmin(PlatformConnection, AdminSite())
    request = RequestFactory().get("/")
    request.user = superuser
    assert admin_instance.has_add_permission(request) is False
    assert admin_instance.has_change_permission(request) is False
    assert admin_instance.has_delete_permission(request) is False


def test_integrations_models_are_in_unfold_sidebar() -> None:
    """Every integrations changelist is linked from the curated Unfold sidebar.

    ``show_all_applications`` is off, so an admin page without a sidebar entry is effectively hidden.
    """
    sidebar = t.cast(dict[str, t.Any], settings.UNFOLD["SIDEBAR"])
    navigation: list[dict[str, t.Any]] = sidebar["navigation"]
    links = {str(item["link"]) for group in navigation for item in group["items"] if "link" in item}
    for model in (PlatformConnection, EventLink, TierLink, WebhookDelivery, ImportJob):
        assert reverse(f"admin:integrations_{model._meta.model_name}_changelist") in links
