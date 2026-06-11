"""Tests for StripeWebhookEventAdmin."""

import typing as t

import pytest
from django.contrib.admin.sites import site

from events.models import StripeWebhookEvent

pytestmark = pytest.mark.django_db


def test_registered_and_read_only(rf: t.Any) -> None:
    """The event log admin is registered and rejects add/delete."""
    admin_instance = site._registry[StripeWebhookEvent]
    request = rf.get("/")
    assert admin_instance.has_add_permission(request) is False
    assert admin_instance.has_delete_permission(request) is False
