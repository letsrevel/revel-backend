"""Tests for ``events.service.dashboard_service``.

Covers the query-composition helpers that the dashboard endpoints delegate
to: authorized ∩ relationship intersections for events/orgs/series, and the
invitation-exclusion chain.
"""

import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events import filters, models
from events.service import dashboard_service

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Local fixtures (mirrors test_controllers/conftest.py:dashboard_setup but
# self-contained so service tests don't depend on controller-test conftest).
# ---------------------------------------------------------------------------


@pytest.fixture
def dash_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    """The user whose dashboard we exercise."""
    return django_user_model.objects.create_user(username="dash-svc", email="dash-svc@example.com", password="p")


@pytest.fixture
def dash_setup(dash_user: RevelUser, django_user_model: t.Type[RevelUser]) -> dict[str, t.Any]:
    """Build the same relationship graph used by dashboard controller tests."""
    org_owner = models.Organization.objects.create(name="Owned Org", owner=dash_user)
    org_staff = models.Organization.objects.create(
        name="Staff Org", owner=django_user_model.objects.create_user("svc-anotherowner")
    )
    models.OrganizationStaff.objects.create(organization=org_staff, user=dash_user)
    org_member = models.Organization.objects.create(
        name="Member Org", owner=django_user_model.objects.create_user("svc-thirdowner")
    )
    models.OrganizationMember.objects.create(organization=org_member, user=dash_user)

    org_public_rsvp = models.Organization.objects.create(
        name="RSVP Org", owner=django_user_model.objects.create_user("svc-fourthowner"), visibility="public"
    )
    org_public_ticket = models.Organization.objects.create(
        name="Ticket Org", owner=django_user_model.objects.create_user("svc-fifthowner"), visibility="public"
    )
    org_private_unrelated = models.Organization.objects.create(
        name="Unrelated Private Org",
        owner=django_user_model.objects.create_user("svc-seventhowner"),
        visibility="private",
    )

    evt_owner = models.Event.objects.create(
        name="In Owned Org", organization=org_owner, status="open", start=timezone.now()
    )
    evt_staff = models.Event.objects.create(
        name="In Staff Org", organization=org_staff, status="open", start=timezone.now()
    )
    evt_member = models.Event.objects.create(
        name="In Member Org",
        organization=org_member,
        status="open",
        visibility=models.Event.Visibility.MEMBERS_ONLY,
        start=timezone.now(),
    )
    evt_rsvp = models.Event.objects.create(
        name="RSVP'd Event", organization=org_public_rsvp, status="open", start=timezone.now()
    )
    models.EventRSVP.objects.create(event=evt_rsvp, user=dash_user, status="yes")
    evt_ticket = models.Event.objects.create(
        name="Ticketed Event", organization=org_public_ticket, status="open", start=timezone.now()
    )
    tier = evt_ticket.ticket_tiers.first()
    assert tier is not None
    models.Ticket.objects.create(guest_name="Test Guest", event=evt_ticket, user=dash_user, tier=tier)
    evt_invite = models.Event.objects.create(
        name="Invited Event", organization=org_public_ticket, status="open", start=timezone.now()
    )
    models.EventInvitation.objects.create(event=evt_invite, user=dash_user)
    models.Event.objects.create(
        name="Unrelated Private Event", organization=org_private_unrelated, status="open", start=timezone.now()
    )

    return {
        "user": dash_user,
        "orgs": {
            "owner": org_owner,
            "staff": org_staff,
            "member": org_member,
            "rsvp": org_public_rsvp,
            "ticket": org_public_ticket,
            "private": org_private_unrelated,
        },
        "events": {
            "owner": evt_owner,
            "staff": evt_staff,
            "member": evt_member,
            "rsvp": evt_rsvp,
            "ticket": evt_ticket,
            "invite": evt_invite,
        },
    }


# ---------------------------------------------------------------------------
# get_user_related_organizations
# ---------------------------------------------------------------------------


def test_get_user_related_organizations_returns_intersection(
    dash_user: RevelUser, dash_setup: dict[str, t.Any]
) -> None:
    """Default filters return owned, staff and member orgs (authorized ∩ related)."""
    params = filters.DashboardOrganizationsFiltersSchema()
    qs = dashboard_service.get_user_related_organizations(dash_user, params)
    assert {o.name for o in qs} == {"Owned Org", "Staff Org", "Member Org"}


def test_get_user_related_organizations_respects_relationship_filters(
    dash_user: RevelUser, dash_setup: dict[str, t.Any]
) -> None:
    """Disabling staff/member yields only the owned org."""
    params = filters.DashboardOrganizationsFiltersSchema(owner=True, staff=False, member=False)
    qs = dashboard_service.get_user_related_organizations(dash_user, params)
    assert {o.name for o in qs} == {"Owned Org"}


def test_get_user_related_organizations_excludes_unauthorized(
    dash_user: RevelUser, dash_setup: dict[str, t.Any]
) -> None:
    """The unrelated private org never appears, even with all filters on."""
    params = filters.DashboardOrganizationsFiltersSchema()
    qs = dashboard_service.get_user_related_organizations(dash_user, params)
    assert not qs.filter(name="Unrelated Private Org").exists()


# ---------------------------------------------------------------------------
# get_user_related_events
# ---------------------------------------------------------------------------


def test_get_user_related_events_returns_full_intersection(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Default filters return all six related events."""
    params = filters.DashboardEventsFiltersSchema()
    qs = dashboard_service.get_user_related_events(dash_user, params)
    assert {e.name for e in qs} == {
        "In Owned Org",
        "In Staff Org",
        "In Member Org",
        "RSVP'd Event",
        "Ticketed Event",
        "Invited Event",
    }


def test_get_user_related_events_requires_ticket_filter(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Passing ``requires_ticket=False`` further narrows the intersection."""
    models.Event.objects.create(
        name="RSVP-Only Event",
        organization=dash_setup["orgs"]["owner"],
        status="open",
        start=timezone.now(),
        requires_ticket=False,
    )
    params = filters.DashboardEventsFiltersSchema(requires_ticket=False)
    qs = dashboard_service.get_user_related_events(dash_user, params)
    assert {e.name for e in qs} == {"RSVP-Only Event"}


def test_get_user_related_events_excludes_unrelated_event(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Events in private orgs the user has no relation to must not appear."""
    params = filters.DashboardEventsFiltersSchema()
    qs = dashboard_service.get_user_related_events(dash_user, params)
    assert not qs.filter(name="Unrelated Private Event").exists()


# ---------------------------------------------------------------------------
# get_user_related_event_series
# ---------------------------------------------------------------------------


def test_get_user_related_event_series_intersection(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Returns series the user has visibility on AND a matching relationship."""
    org_owner = dash_setup["orgs"]["owner"]
    org_unrelated = dash_setup["orgs"]["private"]
    owned_series = models.EventSeries.objects.create(name="Owned Series", organization=org_owner)
    models.EventSeries.objects.create(name="Unrelated Series", organization=org_unrelated)

    params = filters.DashboardEventSeriesFiltersSchema()
    qs = dashboard_service.get_user_related_event_series(dash_user, params)
    names = {s.name for s in qs}
    assert owned_series.name in names
    assert "Unrelated Series" not in names


def test_get_user_related_event_series_relationship_filters(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Turning every relationship off yields the empty set."""
    models.EventSeries.objects.create(name="Owned Series", organization=dash_setup["orgs"]["owner"])
    params = filters.DashboardEventSeriesFiltersSchema(owner=False, staff=False, member=False)
    qs = dashboard_service.get_user_related_event_series(dash_user, params)
    assert list(qs) == []


# ---------------------------------------------------------------------------
# get_user_invitations
# ---------------------------------------------------------------------------


def test_get_user_invitations_hides_past_by_default(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Past-ended events are hidden unless ``include_past`` is True."""
    past_event = dash_setup["events"]["invite"]
    past_event.start = timezone.now() - timedelta(days=2)
    past_event.end = timezone.now() - timedelta(days=1)
    past_event.save(update_fields=["start", "end"])

    future_event = models.Event.objects.create(
        name="Future Invite Event",
        organization=dash_setup["orgs"]["owner"],
        status="open",
        start=timezone.now() + timedelta(days=5),
        end=timezone.now() + timedelta(days=6),
    )
    models.EventInvitation.objects.create(user=dash_user, event=future_event)

    qs = dashboard_service.get_user_invitations(dash_user)
    assert {inv.event.name for inv in qs} == {"Future Invite Event"}


def test_get_user_invitations_include_past_shows_all(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """``include_past=True`` returns invitations regardless of event end."""
    past_event = dash_setup["events"]["invite"]
    past_event.start = timezone.now() - timedelta(days=2)
    past_event.end = timezone.now() - timedelta(days=1)
    past_event.save(update_fields=["start", "end"])

    qs = dashboard_service.get_user_invitations(dash_user, include_past=True)
    assert past_event.name in {inv.event.name for inv in qs}


def test_get_user_invitations_filter_by_event_id(dash_user: RevelUser, dash_setup: dict[str, t.Any]) -> None:
    """Filtering by ``event_id`` restricts the result set to that event."""
    invite_evt = dash_setup["events"]["invite"]
    invite_evt.end = timezone.now() + timedelta(days=1)
    invite_evt.save(update_fields=["end"])

    other_evt = models.Event.objects.create(
        name="Other Event",
        organization=dash_setup["orgs"]["owner"],
        status="open",
        start=timezone.now() + timedelta(days=2),
        end=timezone.now() + timedelta(days=3),
    )
    models.EventInvitation.objects.create(user=dash_user, event=other_evt)

    qs = dashboard_service.get_user_invitations(dash_user, event_id=invite_evt.id)
    assert [inv.event_id for inv in qs] == [invite_evt.id]


def test_get_user_invitations_exclude_accepted_hides_ticket_holders(
    dash_user: RevelUser, dash_setup: dict[str, t.Any]
) -> None:
    """Invitations are hidden by default when the user already holds a ticket."""
    invite_evt = dash_setup["events"]["invite"]
    invite_evt.end = timezone.now() + timedelta(days=1)
    invite_evt.save(update_fields=["end"])

    tier = invite_evt.ticket_tiers.first()
    assert tier is not None
    models.Ticket.objects.create(
        guest_name="Self",
        event=invite_evt,
        user=dash_user,
        tier=tier,
        status=models.Ticket.TicketStatus.ACTIVE,
    )

    hidden_qs = dashboard_service.get_user_invitations(dash_user)
    assert invite_evt.id not in {inv.event_id for inv in hidden_qs}

    shown_qs = dashboard_service.get_user_invitations(dash_user, exclude_accepted=False)
    assert invite_evt.id in {inv.event_id for inv in shown_qs}


def test_get_user_invitations_exclude_accepted_hides_yes_rsvp(
    dash_user: RevelUser, dash_setup: dict[str, t.Any]
) -> None:
    """Invitations are hidden by default when the user has a YES RSVP."""
    invite_evt = dash_setup["events"]["invite"]
    invite_evt.end = timezone.now() + timedelta(days=1)
    invite_evt.save(update_fields=["end"])

    models.EventRSVP.objects.create(event=invite_evt, user=dash_user, status=models.EventRSVP.RsvpStatus.YES)

    hidden_qs = dashboard_service.get_user_invitations(dash_user)
    assert invite_evt.id not in {inv.event_id for inv in hidden_qs}

    shown_qs = dashboard_service.get_user_invitations(dash_user, exclude_accepted=False)
    assert invite_evt.id in {inv.event_id for inv in shown_qs}
