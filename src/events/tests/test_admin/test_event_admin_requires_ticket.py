"""``requires_ticket`` is create-only in the Event admin (#1295)."""

import typing as t

import pytest
from django.contrib.admin.sites import site
from django.test import RequestFactory

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


RSVP_ONLY_MESSAGE = "This event is RSVP-only and does not sell tickets."


def _tier_formset(superuser: RevelUser, requires_ticket: bool) -> t.Any:
    """Build the tier formset for an unsaved add-page Event with one new, otherwise valid tier row."""
    request = RequestFactory().get("/")
    request.user = superuser
    formset_class = TicketTierInline(models.Event, site).get_formset(request)
    prefix = formset_class.get_default_prefix()
    data = {
        f"{prefix}-TOTAL_FORMS": "1",
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
        f"{prefix}-0-name": "General",
        f"{prefix}-0-visibility": "public",
        f"{prefix}-0-payment_method": "offline",
        f"{prefix}-0-purchasable_by": "public",
        f"{prefix}-0-price": "0",
        f"{prefix}-0-price_type": "fixed",
        f"{prefix}-0-pwyc_min": "1",
        f"{prefix}-0-currency": "EUR",
        f"{prefix}-0-quantity_sold": "0",
        f"{prefix}-0-seat_assignment_mode": "none",
        f"{prefix}-0-display_order": "0",
    }
    event = models.Event(requires_ticket=requires_ticket)
    return formset_class(data=data, instance=event, prefix=prefix)


def test_ticket_tier_formset_rejects_new_tier_on_rsvp_only_add(superuser: RevelUser) -> None:
    formset = _tier_formset(superuser, requires_ticket=False)

    assert formset.is_valid() is False
    assert [str(e) for e in formset.non_form_errors()] == [RSVP_ONLY_MESSAGE]


def test_ticket_tier_formset_allows_new_tier_on_ticketed_add(superuser: RevelUser) -> None:
    formset = _tier_formset(superuser, requires_ticket=True)

    assert formset.is_valid(), formset.errors
