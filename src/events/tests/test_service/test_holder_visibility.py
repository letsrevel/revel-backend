"""Known-holder predicate: only the account holder's own acts make them known to an organization."""

import typing as t
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.exceptions import InvitationRequiredError
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventRSVP,
    EventSeries,
    EventWaitList,
    HeldSeriesPass,
    MembershipRecord,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    OrganizationStaff,
    SeriesPass,
    Ticket,
    TicketSaleSource,
    TicketTier,
)
from events.service.holder_visibility import annotate_holder_known, is_known_holder, require_known_holder
from events.service.organization_service import has_relationship
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

Act = t.Callable[[Event, RevelUser], object]


def _ticket(event: Event, user: RevelUser, source: str | None, status: str = Ticket.TicketStatus.ACTIVE) -> Ticket:
    tier = TicketTier.objects.create(event=event, name=f"T{Ticket.objects.count()}", price=Decimal("0"))
    ticket = Ticket.objects.create(event=event, user=user, tier=tier, status=status, guest_name="X")
    Ticket.objects.filter(pk=ticket.pk).update(sale_source=source)  # editable=False
    ticket.refresh_from_db()
    return ticket


def _record(event: Event, user: RevelUser, source: str, ticket: Ticket | None = None) -> MembershipRecord:
    return MembershipRecord.objects.create(
        organization=event.organization,
        user=user,
        full_name="X",
        valid_from=timezone.now(),
        source=source,
        ticket=ticket,
        kind=MembershipRecord.Kind.TEMPORARY if ticket else MembershipRecord.Kind.STANDARD,
    )


def _held_pass(event: Event, user: RevelUser) -> HeldSeriesPass:
    series = EventSeries.objects.create(organization=event.organization, name="S", slug="s")
    series_pass = SeriesPass.objects.create(
        event_series=series,
        name="Season",
        price=Decimal("10.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
    return HeldSeriesPass.objects.create(series_pass=series_pass, user=user, price_paid=Decimal("10.00"))


def _submission(event: Event, user: RevelUser) -> QuestionnaireSubmission:
    questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    OrganizationQuestionnaire.objects.create(organization=event.organization, questionnaire=questionnaire)
    return QuestionnaireSubmission.objects.create(questionnaire=questionnaire, user=user)


KNOWN: dict[str, Act] = {
    "staff": lambda e, u: OrganizationStaff.objects.create(organization=e.organization, user=u),
    "member": lambda e, u: OrganizationMember.objects.create(organization=e.organization, user=u),
    "banned member": lambda e, u: OrganizationMember.objects.create(
        organization=e.organization, user=u, status=OrganizationMember.MembershipStatus.BANNED
    ),
    "membership request": lambda e, u: OrganizationMembershipRequest.objects.create(
        organization=e.organization, user=u
    ),
    "register record": lambda e, u: _record(e, u, MembershipRecord.Source.IMPORT),
    "checkout ticket": lambda e, u: _ticket(e, u, TicketSaleSource.CHECKOUT),
    "checked-in checkout ticket": lambda e, u: _ticket(e, u, TicketSaleSource.CHECKOUT, Ticket.TicketStatus.CHECKED_IN),
    "series-pass ticket": lambda e, u: _ticket(e, u, TicketSaleSource.SERIES_PASS),
    "legacy ticket (no source)": lambda e, u: _ticket(e, u, None),
    "day membership from checkout": lambda e, u: _record(
        e, u, MembershipRecord.Source.TICKET, _ticket(e, u, TicketSaleSource.CHECKOUT)
    ),
    "own RSVP": lambda e, u: EventRSVP.objects.create(event=e, user=u, status=EventRSVP.RsvpStatus.NO),
    "series pass": _held_pass,
    "waitlist entry": lambda e, u: EventWaitList.objects.create(event=e, user=u),
    "invitation request": lambda e, u: EventInvitationRequest.objects.create(event=e, user=u),
    "questionnaire submission": _submission,
}

NOT_KNOWN: dict[str, Act] = {
    "nothing": lambda e, u: None,
    "pending checkout ticket": lambda e, u: _ticket(e, u, TicketSaleSource.CHECKOUT, Ticket.TicketStatus.PENDING),
    "cancelled checkout ticket": lambda e, u: _ticket(e, u, TicketSaleSource.CHECKOUT, Ticket.TicketStatus.CANCELLED),
    "box-office sale": lambda e, u: _ticket(e, u, TicketSaleSource.BOX_OFFICE_SALE),
    "box-office comp": lambda e, u: _ticket(e, u, TicketSaleSource.BOX_OFFICE_COMP),
    "cancelled box-office comp": lambda e, u: _ticket(
        e, u, TicketSaleSource.BOX_OFFICE_COMP, Ticket.TicketStatus.CANCELLED
    ),
    "day membership from box office": lambda e, u: _record(
        e, u, MembershipRecord.Source.TICKET, _ticket(e, u, TicketSaleSource.BOX_OFFICE_SALE)
    ),
    "day membership from cancelled checkout ticket": lambda e, u: _record(
        e, u, MembershipRecord.Source.TICKET, _ticket(e, u, TicketSaleSource.CHECKOUT, Ticket.TicketStatus.CANCELLED)
    ),
    "merge-linked register record": lambda e, u: MembershipRecord.objects.create(
        organization=e.organization,
        user=u,
        full_name="X",
        valid_from=timezone.now(),
        source=MembershipRecord.Source.MANUAL,
        linked_via=MembershipRecord.LinkMethod.MERGE,
    ),
    "staff RSVP": lambda e, u: EventRSVP.objects.create(
        event=e, user=u, status=EventRSVP.RsvpStatus.YES, source=EventRSVP.Source.STAFF
    ),
    "event invitation": lambda e, u: EventInvitation.objects.create(event=e, user=u),
}


@pytest.mark.parametrize("act", KNOWN.values(), ids=KNOWN.keys())
def test_own_acts_make_the_holder_known(event: Event, nonmember_user: RevelUser, act: Act) -> None:
    act(event, nonmember_user)
    assert is_known_holder(event.organization, nonmember_user)


@pytest.mark.parametrize("act", NOT_KNOWN.values(), ids=NOT_KNOWN.keys())
def test_staff_originated_rows_confer_nothing(event: Event, nonmember_user: RevelUser, act: Act) -> None:
    act(event, nonmember_user)
    assert not is_known_holder(event.organization, nonmember_user)
    with pytest.raises(InvitationRequiredError):
        require_known_holder(event.organization, nonmember_user)


def test_owner_is_known(organization: Organization, organization_owner_user: RevelUser) -> None:
    assert is_known_holder(organization, organization_owner_user)


@pytest.mark.parametrize("act", KNOWN.values(), ids=KNOWN.keys())
def test_acts_toward_another_organization_do_not_count(
    event: Event, nonmember_user: RevelUser, revel_user_factory: RevelUserFactory, act: Act
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other-org", owner=revel_user_factory())
    act(Event.objects.create(organization=other_org, name="Elsewhere", start=event.start), nonmember_user)
    assert not is_known_holder(event.organization, nonmember_user)


def test_annotation_judges_each_row_in_one_query(
    event: Event, nonmember_user: RevelUser, revel_user_factory: RevelUserFactory
) -> None:
    stranger_ticket = _ticket(event, nonmember_user, TicketSaleSource.BOX_OFFICE_COMP)
    buyer = revel_user_factory()
    buyer_ticket = _ticket(event, buyer, TicketSaleSource.CHECKOUT)

    with CaptureQueriesContext(connection) as ctx:
        rows: list[t.Any] = list(annotate_holder_known(Ticket.objects.filter(event=event), event.organization_id))
    known = {row.pk: row.holder_known for row in rows}

    assert len(ctx.captured_queries) == 1
    assert known == {stranger_ticket.pk: False, buyer_ticket.pk: True}


@pytest.mark.parametrize(
    ("source", "expected"), [(TicketSaleSource.BOX_OFFICE_COMP, False), (TicketSaleSource.CHECKOUT, True)]
)
def test_default_relationship_follows_day_membership_origin(
    event: Event, nonmember_user: RevelUser, source: str, expected: bool
) -> None:
    """Staff add and subscription create: a box-office day membership is no standing either."""
    _record(event, nonmember_user, MembershipRecord.Source.TICKET, _ticket(event, nonmember_user, source))
    assert has_relationship(event.organization, nonmember_user) is expected


def test_default_relationship_ignores_merge_linked_record(event: Event, nonmember_user: RevelUser) -> None:
    NOT_KNOWN["merge-linked register record"](event, nonmember_user)
    assert not has_relationship(event.organization, nonmember_user)


def test_online_day_membership_stops_counting_when_its_ticket_is_cancelled(
    event: Event, nonmember_user: RevelUser
) -> None:
    """A day-membership record counts only while its ticket is attending; a plain register entry always does."""
    ticket = _ticket(event, nonmember_user, TicketSaleSource.CHECKOUT)
    _record(event, nonmember_user, MembershipRecord.Source.TICKET, ticket)
    assert is_known_holder(event.organization, nonmember_user)
    Ticket.objects.filter(pk=ticket.pk).update(status=Ticket.TicketStatus.CANCELLED)
    assert not is_known_holder(event.organization, nonmember_user)
    _record(event, nonmember_user, MembershipRecord.Source.IMPORT)
    assert is_known_holder(event.organization, nonmember_user)
