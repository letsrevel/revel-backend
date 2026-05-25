"""Tests for the Unfold admin dashboard, focused on the traction leaderboard.

The "top organizations by traction" metric is computed by
``OrganizationQuerySet.top_by_traction`` and shaped for the template by
``revel.dashboard._get_top_organizations_by_traction``. These tests cover the
metric's edge cases (cross-source dedup, status filtering, the trailing-window
cutoff, ranking/limit) and the helper's output contract.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, Organization, Ticket, TicketTier
from revel.dashboard import _get_top_organizations_by_traction

pytestmark = pytest.mark.django_db


def _make_org(name: str, owner: RevelUser) -> Organization:
    return Organization.objects.create(name=name, slug=name.lower().replace(" ", "-"), owner=owner)


def _make_event(org: Organization) -> Event:
    return Event.objects.create(
        organization=org,
        name=f"{org.name} Event",
        slug="event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        max_attendees=100,
        status=Event.EventStatus.OPEN,
        start=timezone.now(),
        requires_ticket=True,
    )


def _rsvp(
    event: Event,
    user: RevelUser,
    status: EventRSVP.RsvpStatus,
    *,
    created_at: datetime | None = None,
) -> EventRSVP:
    rsvp = EventRSVP.objects.create(event=event, user=user, status=status)
    if created_at is not None:
        # created_at is auto_now_add; .update() bypasses it to backdate engagement.
        EventRSVP.objects.filter(pk=rsvp.pk).update(created_at=created_at)
    return rsvp


def _ticket(
    event: Event,
    user: RevelUser,
    status: Ticket.TicketStatus = Ticket.TicketStatus.ACTIVE,
    *,
    created_at: datetime | None = None,
) -> Ticket:
    # TicketTier has a unique (event, name) constraint, so each tier needs a distinct name.
    tier = TicketTier.objects.create(event=event, name=f"GA-{uuid.uuid4().hex[:8]}")
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, status=status, guest_name="Guest")
    if created_at is not None:
        Ticket.objects.filter(pk=ticket.pk).update(created_at=created_at)
    return ticket


def test_user_with_rsvp_and_ticket_for_same_org_counts_once(revel_user_factory: RevelUserFactory) -> None:
    """A user who both RSVPs and buys a ticket for the same org is a single distinct user."""
    owner = revel_user_factory()
    org = _make_org("Solo Org", owner)
    event = _make_event(org)
    user = revel_user_factory()
    _rsvp(event, user, EventRSVP.RsvpStatus.YES)
    _ticket(event, user, Ticket.TicketStatus.ACTIVE)

    rows = Organization.objects.top_by_traction(since=timezone.now() - relativedelta(months=12))

    assert len(rows) == 1
    assert rows[0].organization_id == org.id
    assert rows[0].distinct_users == 1


def test_status_filtering_counts_only_qualifying_engagement(revel_user_factory: RevelUserFactory) -> None:
    """no RSVPs and cancelled tickets are excluded; yes/maybe RSVPs and active/pending/checked_in tickets count."""
    owner = revel_user_factory()

    # An org whose only engagement is non-qualifying must be omitted entirely.
    excluded = _make_org("Excluded Org", owner)
    excluded_event = _make_event(excluded)
    _rsvp(excluded_event, revel_user_factory(), EventRSVP.RsvpStatus.NO)
    _ticket(excluded_event, revel_user_factory(), Ticket.TicketStatus.CANCELLED)

    # An org with one distinct user per qualifying status.
    included = _make_org("Included Org", owner)
    included_event = _make_event(included)
    _rsvp(included_event, revel_user_factory(), EventRSVP.RsvpStatus.YES)
    _rsvp(included_event, revel_user_factory(), EventRSVP.RsvpStatus.MAYBE)
    _ticket(included_event, revel_user_factory(), Ticket.TicketStatus.ACTIVE)
    _ticket(included_event, revel_user_factory(), Ticket.TicketStatus.PENDING)
    _ticket(included_event, revel_user_factory(), Ticket.TicketStatus.CHECKED_IN)

    rows = Organization.objects.top_by_traction(since=timezone.now() - relativedelta(months=12))

    by_name = {row.name: row.distinct_users for row in rows}
    assert "Excluded Org" not in by_name
    assert by_name == {"Included Org": 5}


def test_trailing_window_cutoff_is_inclusive_lower_bound(revel_user_factory: RevelUserFactory) -> None:
    """Engagement on/after ``since`` counts; engagement before ``since`` is excluded."""
    since = timezone.now() - relativedelta(months=12)
    owner = revel_user_factory()
    org = _make_org("Boundary Org", owner)
    event = _make_event(org)

    _rsvp(event, revel_user_factory(), EventRSVP.RsvpStatus.YES, created_at=since)  # exactly on the boundary: in
    _rsvp(event, revel_user_factory(), EventRSVP.RsvpStatus.YES, created_at=since + timedelta(days=1))  # inside: in
    _rsvp(event, revel_user_factory(), EventRSVP.RsvpStatus.YES, created_at=since - timedelta(days=1))  # outside: out

    rows = Organization.objects.top_by_traction(since=since)

    assert len(rows) == 1
    assert rows[0].distinct_users == 2


def test_ranks_descending_and_limits_to_top_n(revel_user_factory: RevelUserFactory) -> None:
    """Organizations are ranked by distinct users descending and capped at ``limit``."""
    owner = revel_user_factory()
    # Reuse a shared user pool so each org's distinct-user count is exactly the slice size.
    pool = [revel_user_factory() for _ in range(12)]

    # "Org 01" reaches 12 users, "Org 02" reaches 11, ... "Org 12" reaches 1.
    for index in range(1, 13):
        org = _make_org(f"Org {index:02d}", owner)
        event = _make_event(org)
        for user in pool[: 13 - index]:
            _rsvp(event, user, EventRSVP.RsvpStatus.YES)

    rows = Organization.objects.top_by_traction(since=timezone.now() - relativedelta(months=12), limit=10)

    assert [row.distinct_users for row in rows] == list(range(12, 2, -1))
    assert [row.name for row in rows] == [f"Org {index:02d}" for index in range(1, 11)]


def test_ties_broken_alphabetically_by_name(revel_user_factory: RevelUserFactory) -> None:
    """Organizations tied on distinct users are ordered alphabetically (case-insensitive)."""
    owner = revel_user_factory()
    shared = [revel_user_factory() for _ in range(2)]

    # Created in non-alphabetical order; both reach the same two users.
    for name in ("beta org", "Alpha Org"):
        event = _make_event(_make_org(name, owner))
        for user in shared:
            _rsvp(event, user, EventRSVP.RsvpStatus.YES)

    rows = Organization.objects.top_by_traction(since=timezone.now() - relativedelta(months=12))

    assert [row.name for row in rows] == ["Alpha Org", "beta org"]
    assert {row.distinct_users for row in rows} == {2}


def test_get_top_organizations_by_traction_output_shape(revel_user_factory: RevelUserFactory) -> None:
    """The dashboard helper returns chart labels/data and ranked table rows with admin links."""
    owner = revel_user_factory()
    org = _make_org("Shapely Org", owner)
    event = _make_event(org)
    _rsvp(event, revel_user_factory(), EventRSVP.RsvpStatus.YES)

    result = _get_top_organizations_by_traction(months=12, limit=10)

    assert result["labels"] == ["Shapely Org"]
    assert result["data"] == [1]
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["rank"] == 1
    assert row["name"] == "Shapely Org"
    assert row["distinct_users"] == 1
    assert str(org.id) in row["change_url"]


def test_get_top_organizations_by_traction_empty(revel_user_factory: RevelUserFactory) -> None:
    """With no qualifying engagement the helper returns empty chart series and no rows."""
    result = _get_top_organizations_by_traction(months=12, limit=10)

    assert result == {"labels": [], "data": [], "rows": []}
