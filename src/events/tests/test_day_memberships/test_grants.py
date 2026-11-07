"""Day-membership grants on every ticket issuance path (#1320)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import stripe
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    EventSeries,
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationQuestionnaire,
    Payment,
    Ticket,
    TicketSaleSource,
    TicketTier,
    VenueSeat,
)
from events.schema import TicketPurchaseItem
from events.service import check_in_service, ticket_service
from events.service.batch_ticket_service import BatchTicketService
from events.service.seating import box_office
from events.service.stripe_webhooks import StripeEventHandler
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

from .conftest import records_for

pytestmark = pytest.mark.django_db

Status = Ticket.TicketStatus


def _assert_day_record(
    ticket: Ticket, event: Event, tier: MembershipTier, user: RevelUser, source: str = MembershipRecord.Source.TICKET
) -> MembershipRecord:
    (record,) = records_for(ticket)
    assert record.kind == MembershipRecord.Kind.TEMPORARY
    assert record.source == source
    assert record.organization_id == event.organization_id
    assert record.user_id == user.pk
    assert record.full_name == "Dana Day"
    assert record.email == user.email
    assert record.tier_id == tier.pk
    assert record.tier_name == tier.name
    assert record.valid_from == event.start
    assert record.valid_until == event.end
    assert record.ended_reason is None
    assert record.event_id == event.pk
    return record


def test_active_ticket_grants_day_record(
    day_event: Event,
    day_tier: TicketTier,
    day_member_tier: MembershipTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    ticket = make_ticket(day_tier)
    record = _assert_day_record(ticket, day_event, day_member_tier, holder)
    assert record.questionnaire_submission is None
    assert record.accepted_at is None
    assert record.questionnaire_name == ""
    assert record.ticket_id == ticket.pk


def test_non_granting_tier_creates_no_record(
    day_tier_factory: t.Callable[..., TicketTier], make_ticket: t.Callable[..., Ticket]
) -> None:
    ticket = make_ticket(day_tier_factory(grants=False))
    assert records_for(ticket) == []


def test_pending_ticket_grants_nothing_until_active(day_tier: TicketTier, make_ticket: t.Callable[..., Ticket]) -> None:
    ticket = make_ticket(day_tier, Status.PENDING)
    assert records_for(ticket) == []
    ticket.status = Status.CANCELLED
    ticket.save(update_fields=["status"])
    assert records_for(ticket) == []


def test_free_checkout_grants(
    day_event: Event,
    day_tier: TicketTier,
    day_member_tier: MembershipTier,
    holder: RevelUser,
) -> None:
    (ticket,) = BatchTicketService(day_event, day_tier, holder).create_batch(
        [TicketPurchaseItem(guest_name="Dana Day")]
    )  # type: ignore[misc]
    _assert_day_record(ticket, day_event, day_member_tier, holder)


def test_offline_pending_check_in_grants(
    day_event: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    day_member_tier: MembershipTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
    organization_owner_user: RevelUser,
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    ticket = make_ticket(tier, Status.PENDING)
    assert records_for(ticket) == []
    Event.objects.filter(pk=day_event.pk).update(
        start=timezone.now() - timedelta(minutes=5), end=timezone.now() + timedelta(hours=5)
    )
    day_event.refresh_from_db()

    checked_in = check_in_service.check_in_ticket(day_event, ticket.id, organization_owner_user)

    assert checked_in.status == Status.CHECKED_IN
    (record,) = records_for(ticket)
    assert record.valid_from == day_event.start
    assert checked_in.membership is not None  # type: ignore[attr-defined]
    assert checked_in.membership.kind == MembershipRecord.Kind.TEMPORARY  # type: ignore[attr-defined]


def test_offline_confirm_grants(
    day_event: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    day_member_tier: MembershipTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.OFFLINE, price=Decimal("10.00"))
    ticket = make_ticket(tier, Status.PENDING)
    ticket_service.confirm_ticket_payment(ticket)
    _assert_day_record(ticket, day_event, day_member_tier, holder)


def test_stripe_webhook_activation_grants(
    day_event: Event,
    day_tier_factory: t.Callable[..., TicketTier],
    day_member_tier: MembershipTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    tier = day_tier_factory(payment_method=TicketTier.PaymentMethod.ONLINE, price=Decimal("10.00"))
    ticket = make_ticket(tier, Status.PENDING)
    Payment.objects.create(
        ticket=ticket,
        user=holder,
        stripe_session_id="cs_day",
        amount=Decimal("10.00"),
        platform_fee=Decimal("0.00"),
        currency="EUR",
        status=Payment.PaymentStatus.PENDING,
        raw_response={},
    )
    session = {"id": "cs_day", "payment_status": "paid", "payment_intent": "pi_day"}
    data = {"type": "checkout.session.completed", "data": {"object": session}}
    event = MagicMock(spec=stripe.Event)
    event.__iter__.return_value = iter(data.items())
    event.type = data["type"]
    event.data = MagicMock()
    event.data.object = session

    StripeEventHandler(event).handle_checkout_session_completed(event)

    ticket.refresh_from_db()
    assert ticket.status == Status.ACTIVE
    _assert_day_record(ticket, day_event, day_member_tier, holder)


@pytest.mark.parametrize(
    ("method", "source"),
    [
        (TicketTier.PaymentMethod.AT_THE_DOOR, TicketSaleSource.BOX_OFFICE_SALE),
        (TicketTier.PaymentMethod.FREE, TicketSaleSource.BOX_OFFICE_COMP),
    ],
)
def test_box_office_sale_and_comp_grant(
    seated_event: tuple[Event, list[VenueSeat]],
    day_member_tier: MembershipTier,
    holder: RevelUser,
    method: TicketTier.PaymentMethod,
    source: TicketSaleSource,
) -> None:
    event, seats = seated_event
    tier = TicketTier.objects.create(
        event=event,
        name="Door",
        price=Decimal("5.00"),
        currency="EUR",
        payment_method=method,
        grants_membership_tier=day_member_tier,
        max_tickets_per_user=1,
    )
    ticket = box_office.sell(event, tier, seat_id=seats[0].id, payment_method=method, recipient=holder)
    assert ticket.sale_source == source
    (record,) = records_for(ticket)
    assert record.tier_id == day_member_tier.pk
    assert record.valid_from == event.start


# ---- evidence ----


def _admission_submission(
    holder: RevelUser,
    organization: Organization,
    *,
    events: list[Event] | None = None,
    series: EventSeries | None = None,
    evaluation: str | None = None,
    name: str = "Waiver",
    per_event: bool = False,
    requires_evaluation: bool = False,
    scoped_to: Event | None = None,
    published: str = Questionnaire.QuestionnaireStatus.PUBLISHED,
) -> QuestionnaireSubmission:
    questionnaire = Questionnaire.objects.create(name=name, status=published)
    org_q = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        per_event=per_event,
        requires_evaluation=requires_evaluation,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )
    if events:
        org_q.events.set(events)
    if series:
        org_q.event_series.set([series])
    submission = QuestionnaireSubmission.objects.create(
        questionnaire=questionnaire,
        user=holder,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    if evaluation:
        QuestionnaireEvaluation.objects.create(submission=submission, status=evaluation)
    if scoped_to is not None:
        EventQuestionnaireSubmission.objects.create(
            user=holder,
            event=scoped_to,
            questionnaire=questionnaire,
            submission=submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
    return submission


def test_evidence_from_event_admission_questionnaire(
    organization: Organization,
    day_event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    submission = _admission_submission(holder, organization, events=[day_event])
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission_id == submission.pk
    assert record.questionnaire_name == "Waiver"
    assert record.accepted_at == submission.submitted_at


def test_evidence_from_series_admission_questionnaire(
    organization: Organization,
    event_series: EventSeries,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    submission = _admission_submission(holder, organization, series=event_series, name="Series waiver")
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission_id == submission.pk
    assert record.questionnaire_name == "Series waiver"


def test_rejected_or_unrelated_submissions_are_not_evidence(
    organization: Organization,
    day_event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
    event: Event,
) -> None:
    _admission_submission(
        holder,
        organization,
        events=[day_event],
        evaluation=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )
    _admission_submission(holder, organization, events=[event], name="Other event")  # not linked to day_event
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission is None
    assert record.questionnaire_name == ""
    assert record.accepted_at is None


def test_per_event_submission_for_another_event_is_not_evidence(
    organization: Organization,
    day_event: Event,
    event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    _admission_submission(holder, organization, events=[day_event], per_event=True, scoped_to=event)
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission is None


def test_per_event_submission_for_this_event_is_evidence(
    organization: Organization,
    day_event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    submission = _admission_submission(holder, organization, events=[day_event], per_event=True, scoped_to=day_event)
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission_id == submission.pk


def test_pending_review_and_unpublished_are_not_evidence(
    organization: Organization,
    day_event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    status = QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    _admission_submission(
        holder,
        organization,
        events=[day_event],
        requires_evaluation=True,
        evaluation=status.PENDING_REVIEW,
        name="Pending",
    )
    _admission_submission(
        holder, organization, events=[day_event], published=Questionnaire.QuestionnaireStatus.DRAFT, name="Draft"
    )
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission is None


def test_approved_evaluation_is_evidence(
    organization: Organization,
    day_event: Event,
    day_tier: TicketTier,
    holder: RevelUser,
    make_ticket: t.Callable[..., Ticket],
) -> None:
    status = QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    submission = _admission_submission(
        holder, organization, events=[day_event], requires_evaluation=True, evaluation=status.APPROVED
    )
    (record,) = records_for(make_ticket(day_tier))
    assert record.questionnaire_submission_id == submission.pk


def test_two_real_saves_grant_once(day_tier: TicketTier, make_ticket: t.Callable[..., Ticket]) -> None:
    ticket = make_ticket(day_tier)
    ticket.guest_name = "Renamed"
    ticket.save()
    ticket.status = Status.PENDING
    ticket.save(update_fields=["status"])
    ticket.status = Status.ACTIVE
    ticket.save(update_fields=["status"])
    ticket.save()
    assert len(records_for(ticket)) == 1
