"""``requires_ticket`` is create-only in the Event admin (#1295)."""

import typing as t

import pytest
from django.contrib.admin.sites import site

from accounts.models import RevelUser
from events import models
from events.admin.base import TicketTierInline

pytestmark = pytest.mark.django_db


def test_requires_ticket_is_read_only_when_editing(rf: t.Any, superuser: RevelUser, public_event: models.Event) -> None:
    request = rf.get("/")
    request.user = superuser
    model_admin = site._registry[models.Event]

    assert "requires_ticket" in model_admin.get_readonly_fields(request, public_event)


def test_requires_ticket_is_editable_when_adding(rf: t.Any, superuser: RevelUser) -> None:
    request = rf.get("/")
    request.user = superuser
    model_admin = site._registry[models.Event]

    assert "requires_ticket" not in model_admin.get_readonly_fields(request, None)


def test_ticket_tier_inline_cannot_add_tiers_to_rsvp_only_event(
    rf: t.Any, superuser: RevelUser, public_event: models.Event
) -> None:
    request = rf.get("/")
    request.user = superuser
    public_event.requires_ticket = False
    inline = TicketTierInline(models.Event, site)

    assert inline.has_add_permission(request, public_event) is False


def test_ticket_tier_inline_can_add_tiers_to_ticketed_event(
    rf: t.Any, superuser: RevelUser, public_event: models.Event
) -> None:
    request = rf.get("/")
    request.user = superuser
    inline = TicketTierInline(models.Event, site)

    assert inline.has_add_permission(request, public_event) is True
