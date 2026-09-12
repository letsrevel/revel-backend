"""Tests for the two Stripe-oriented quick stats on the admin dashboard.

``connected_stripe`` sits under the Total Organizations card and
``events_with_online_payment`` under Total Events, mirroring how
``connected_telegram`` hangs off Total Users.
"""

import typing as t

import pytest
from django.test import RequestFactory
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, TicketTier
from revel.dashboard import dashboard_callback

pytestmark = pytest.mark.django_db


def _quick_stats(user: RevelUser) -> dict[str, t.Any]:
    request = RequestFactory().get("/admin/")
    request.user = user
    context = dashboard_callback(request, {})
    return t.cast(dict[str, t.Any], context["dashboard"]["quick_stats"])


def _org(owner: RevelUser, name: str, **kwargs: t.Any) -> Organization:
    return Organization.objects.create(name=name, slug=name.lower().replace(" ", "-"), owner=owner, **kwargs)


def _event(org: Organization, slug: str) -> Event:
    return Event.objects.create(
        organization=org,
        name=slug,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        status=Event.EventStatus.OPEN,
        start=timezone.now(),
        requires_ticket=True,
    )


def test_connected_stripe_requires_full_onboarding(superuser: RevelUser, revel_user_factory: RevelUserFactory) -> None:
    """A half-onboarded org (details submitted, charges still off) is not counted."""
    owner = revel_user_factory()
    _org(
        owner,
        "Connected",
        stripe_account_id="acct_connected",
        stripe_charges_enabled=True,
        stripe_details_submitted=True,
    )
    _org(
        owner,
        "Half",
        stripe_account_id="acct_half",
        stripe_charges_enabled=False,
        stripe_details_submitted=True,
    )
    _org(owner, "None")

    stats = _quick_stats(superuser)

    assert stats["total_organizations"] == 3
    assert stats["connected_stripe"] == 1


def test_event_with_several_online_tiers_counts_once(
    superuser: RevelUser, revel_user_factory: RevelUserFactory
) -> None:
    """The metric counts events, not tiers — two online tiers on one event is still one event."""
    org = _org(revel_user_factory(), "Org")
    event = _event(org, "multi-tier")
    TicketTier.objects.create(event=event, name="Early", payment_method=TicketTier.PaymentMethod.ONLINE)
    TicketTier.objects.create(event=event, name="Late", payment_method=TicketTier.PaymentMethod.ONLINE)

    assert _quick_stats(superuser)["events_with_online_payment"] == 1


def test_non_online_payment_methods_are_excluded(superuser: RevelUser, revel_user_factory: RevelUserFactory) -> None:
    """Only ONLINE counts; offline, at-the-door and free tiers take no money through us."""
    org = _org(revel_user_factory(), "Org")
    online = _event(org, "online")
    TicketTier.objects.create(event=online, name="GA", payment_method=TicketTier.PaymentMethod.ONLINE)
    for slug, method in (
        ("offline", TicketTier.PaymentMethod.OFFLINE),
        ("door", TicketTier.PaymentMethod.AT_THE_DOOR),
        ("free", TicketTier.PaymentMethod.FREE),
    ):
        TicketTier.objects.create(event=_event(org, slug), name="GA", payment_method=method)

    stats = _quick_stats(superuser)

    assert stats["total_events"] == 4
    assert stats["events_with_online_payment"] == 1


def test_event_without_tiers_is_not_counted(superuser: RevelUser, revel_user_factory: RevelUserFactory) -> None:
    """An RSVP-only event has no online tier and must not appear in the count."""
    org = _org(revel_user_factory(), "Org")
    event = _event(org, "rsvp-only")
    event.ticket_tiers.all().delete()  # a signal auto-creates a tier for requires_ticket events

    stats = _quick_stats(superuser)

    assert stats["total_events"] == 1
    assert stats["events_with_online_payment"] == 0


def test_empty_database_reports_zeroes(superuser: RevelUser) -> None:
    """Both stats are plain counts, so an empty instance renders 0 rather than blowing up."""
    stats = _quick_stats(superuser)

    assert stats["connected_stripe"] == 0
    assert stats["events_with_online_payment"] == 0
