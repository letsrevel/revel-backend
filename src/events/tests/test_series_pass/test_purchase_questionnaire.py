"""Series pass purchase enforces the series' admission questionnaires (#1321)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.exceptions import SeriesPassNotPurchasableError
from events.models import (
    Event,
    EventInvitation,
    EventQuestionnaireSubmission,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)
from events.service.event_manager import NextStep, Reasons, UserIsIneligibleError
from events.service.series_pass_purchase import SeriesPassPurchaseService
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def _event(organization: Organization, series: EventSeries, slug: str, days: int) -> Event:
    return Event.objects.create(
        organization=organization,
        name=slug,
        slug=slug,
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        event_series=series,
        max_attendees=100,
        start=timezone.now() + timedelta(days=days),
        status=Event.EventStatus.OPEN,
        requires_ticket=True,
    )


@pytest.fixture
def covered_events(organization: Organization, event_series: EventSeries) -> list[Event]:
    """Two upcoming covered events; the earlier one is the gate's anchor."""
    return [_event(organization, event_series, "soon", 1), _event(organization, event_series, "later", 5)]


@pytest.fixture
def free_pass(organization: Organization, event_series: EventSeries, covered_events: list[Event]) -> SeriesPass:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Gated Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    for event in covered_events:
        tier = TicketTier.objects.create(
            event=event, name=f"Tier {event.slug}", price=Decimal("10.00"), currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )  # fmt: skip
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)
    return series_pass


@pytest.fixture
def series_questionnaire(
    organization: Organization, event_series: EventSeries, questionnaire: Questionnaire
) -> OrganizationQuestionnaire:
    """ADMISSION questionnaire linked to the series, evaluation not required."""
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization, questionnaire=questionnaire, requires_evaluation=False
    )
    oq.event_series.add(event_series)
    return oq


def _submit(user: RevelUser, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    return QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )


def _assert_blocked(
    series_pass: SeriesPass, user: RevelUser, questionnaire: Questionnaire, anchor: Event
) -> UserIsIneligibleError:
    with pytest.raises(UserIsIneligibleError) as exc:
        SeriesPassPurchaseService(series_pass, user).purchase()
    elig = exc.value.eligibility
    assert elig.reason_code == Reasons.QUESTIONNAIRE_MISSING.code
    assert elig.questionnaires_missing == [questionnaire.id]
    assert elig.event_id == anchor.id
    assert not HeldSeriesPass.objects.filter(user=user).exists()
    return exc.value


def test_blocked_without_submission(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """No submission: purchase raises, anchored on the earliest upcoming covered event."""
    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_allowed_with_submission_no_evaluation_required(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
) -> None:
    """A READY submission suffices when the questionnaire does not require evaluation."""
    _submit(revel_user, questionnaire)

    SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert HeldSeriesPass.objects.filter(user=revel_user, series_pass=free_pass).exists()


def test_active_member_exempt(
    free_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    """members_exempt lets an ACTIVE member skip the questionnaire."""
    series_questionnaire.members_exempt = True
    series_questionnaire.save()
    OrganizationMember.objects.create(organization=organization, user=revel_user)

    SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_paused_member_not_exempt(
    free_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """A PAUSED member does not get the members_exempt waiver."""
    series_questionnaire.members_exempt = True
    series_questionnaire.save()
    OrganizationMember.objects.create(
        organization=organization, user=revel_user, status=OrganizationMember.MembershipStatus.PAUSED
    )

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_owner_bypasses(
    free_pass: SeriesPass,
    organization_owner_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Organization owners skip the series questionnaire gate."""
    SeriesPassPurchaseService(free_pass, organization_owner_user).purchase()

    assert HeldSeriesPass.objects.filter(user=organization_owner_user).exists()


def test_staff_bypasses(
    free_pass: SeriesPass,
    staff_member: OrganizationStaff,
    organization_staff_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    """Organization staff skip the series questionnaire gate."""
    SeriesPassPurchaseService(free_pass, organization_staff_user).purchase()

    assert HeldSeriesPass.objects.filter(user=organization_staff_user).exists()


def test_event_only_questionnaire_ignored(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    organization: Organization,
    covered_events: list[Event],
) -> None:
    """A questionnaire linked only to a covered event (not the series) is not a pass-purchase gate."""
    other = Questionnaire.objects.create(name="Event only", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=other).events.add(
        covered_events[0]
    )

    SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_per_event_ignored_in_series_scope(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """per_event applies only to single tickets: a submission made for another covered event covers the pass."""
    series_questionnaire.per_event = True
    series_questionnaire.save()
    submission = _submit(revel_user, questionnaire)
    EventQuestionnaireSubmission.objects.create(
        user=revel_user,
        event=covered_events[1],
        questionnaire=questionnaire,
        submission=submission,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )

    SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_per_event_global_submission_without_event_link_blocks(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """A per_event series questionnaire needs a submission tied to an event of this series."""
    series_questionnaire.per_event = True
    series_questionnaire.save()
    _submit(revel_user, questionnaire)

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_expired_approval_blocks(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """An approval older than max_submission_age no longer counts."""
    series_questionnaire.requires_evaluation = True
    series_questionnaire.max_submission_age = timedelta(days=30)
    series_questionnaire.save()
    evaluation = QuestionnaireEvaluation.objects.create(
        submission=_submit(revel_user, questionnaire),
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
    )
    QuestionnaireEvaluation.objects.filter(pk=evaluation.pk).update(updated_at=timezone.now() - timedelta(days=31))

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_rejected_submission_blocks(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
) -> None:
    """A rejected evaluation (no retakes left) blocks with QUESTIONNAIRE_FAILED."""
    series_questionnaire.requires_evaluation = True
    series_questionnaire.save()
    questionnaire.max_attempts = 1
    questionnaire.save()
    QuestionnaireEvaluation.objects.create(
        submission=_submit(revel_user, questionnaire),
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    with pytest.raises(UserIsIneligibleError) as exc:
        SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert exc.value.eligibility.reason_code == Reasons.QUESTIONNAIRE_FAILED.code
    assert not HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_pass_purchase_pending_review_blocks(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
) -> None:
    """A READY submission still awaiting evaluation blocks with QUESTIONNAIRE_PENDING_REVIEW."""
    series_questionnaire.requires_evaluation = True
    series_questionnaire.save()
    _submit(revel_user, questionnaire)

    with pytest.raises(UserIsIneligibleError) as exc:
        SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert exc.value.eligibility.reason_code == Reasons.QUESTIONNAIRE_PENDING_REVIEW.code
    assert not HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_pass_purchase_event_invitation_does_not_waive_series_questionnaire(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """An event invitation waiving the questionnaire does not waive the series questionnaire for the pass."""
    EventInvitation.objects.create(event=covered_events[0], user=revel_user, waives_questionnaire=True)

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_anchor_skips_non_open_event(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """A cancelled first covered event is not the anchor; the next open one is."""
    Event.objects.filter(pk=covered_events[0].pk).update(status=Event.EventStatus.CANCELLED)

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[1])


def test_anchor_skips_event_past_apply_before(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """A first event whose application window has closed is not the anchor."""
    Event.objects.filter(pk=covered_events[0].pk).update(apply_before=timezone.now() - timedelta(hours=1))

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[1])


def _assert_dead_end(series_pass: SeriesPass, user: RevelUser) -> None:
    with pytest.raises(UserIsIneligibleError) as exc:
        SeriesPassPurchaseService(series_pass, user).purchase()
    elig = exc.value.eligibility
    assert elig.next_step is None
    assert "can no longer be submitted" in str(elig.reason)
    assert not HeldSeriesPass.objects.filter(user=user).exists()


def test_all_covered_events_past_apply_before_is_dead_end(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    covered_events: list[Event],
) -> None:
    """No covered event accepts submissions any more: blocked without a complete_questionnaire next step."""
    Event.objects.filter(pk__in=[e.pk for e in covered_events]).update(apply_before=timezone.now() - timedelta(hours=1))

    _assert_dead_end(free_pass, revel_user)


def test_only_cancelled_upcoming_event_is_dead_end(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    covered_events: list[Event],
) -> None:
    """Only cancelled covered events remain: the questionnaire cannot be submitted."""
    Event.objects.filter(pk__in=[e.pk for e in covered_events]).update(status=Event.EventStatus.CANCELLED)

    _assert_dead_end(free_pass, revel_user)


def test_apply_before_boundary_still_accepts(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    """An event whose deadline is exactly now+epsilon is still a valid anchor (open while now <= deadline)."""
    Event.objects.filter(pk=covered_events[0].pk).update(apply_before=timezone.now() + timedelta(seconds=30))

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def _per_event_submission(user: RevelUser, questionnaire: Questionnaire, event: Event, status: str) -> None:
    submission = _submit(user, questionnaire)
    QuestionnaireEvaluation.objects.create(submission=submission, status=status)
    EventQuestionnaireSubmission.objects.create(
        user=user,
        event=event,
        questionnaire=questionnaire,
        submission=submission,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )


@pytest.fixture
def other_series_event(organization: Organization) -> Event:
    other_series = EventSeries.objects.create(organization=organization, name="Other", slug="other")
    return _event(organization, other_series, "elsewhere", 3)


def test_per_event_submission_for_other_series_blocks(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
    other_series_event: Event,
) -> None:
    """A per_event submission made for an event of another series does not satisfy this series' pass."""
    series_questionnaire.per_event = True
    series_questionnaire.save()
    _per_event_submission(
        revel_user, questionnaire, other_series_event, QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_per_event_rejections_on_other_series_do_not_exhaust_attempts(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
    other_series_event: Event,
) -> None:
    """Rejected attempts at another series' event do not count toward this series' attempt cap."""
    series_questionnaire.per_event = True
    series_questionnaire.requires_evaluation = True
    series_questionnaire.save()
    questionnaire.max_attempts = 1
    questionnaire.save()
    _per_event_submission(
        revel_user, questionnaire, other_series_event, QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )

    _assert_blocked(free_pass, revel_user, questionnaire, covered_events[0])


def test_existing_holder_gets_already_hold_not_questionnaire(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
) -> None:
    """Re-buying while holding the pass reports the duplicate, not the questionnaire."""
    _submit(revel_user, questionnaire)
    SeriesPassPurchaseService(free_pass, revel_user).purchase()
    QuestionnaireSubmission.objects.filter(user=revel_user).delete()

    with pytest.raises(SeriesPassNotPurchasableError, match="already hold"):
        SeriesPassPurchaseService(free_pass, revel_user).purchase()


def test_feedback_and_draft_questionnaires_do_not_block(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    organization: Organization,
    event_series: EventSeries,
) -> None:
    """Only published ADMISSION questionnaires gate the pass."""
    feedback = Questionnaire.objects.create(name="Feedback", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    draft = Questionnaire.objects.create(name="Draft", status=Questionnaire.QuestionnaireStatus.DRAFT)
    OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=feedback,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    ).event_series.add(event_series)
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=draft).event_series.add(
        event_series
    )

    SeriesPassPurchaseService(free_pass, revel_user).purchase()

    assert HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_no_upcoming_covered_event_skips_gate(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    """With nothing upcoming the gate has no anchor; the quote's own purchasable check rejects instead."""
    Event.objects.filter(event_series=free_pass.event_series).update(start=timezone.now() - timedelta(days=1))

    with pytest.raises(SeriesPassNotPurchasableError):
        SeriesPassPurchaseService(free_pass, revel_user).purchase()


def test_endpoint_returns_400_eligibility(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    client = Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(revel_user).access_token}")  # type: ignore[attr-defined]

    response = client.post(
        reverse("api:checkout_series_pass", kwargs={"pass_id": free_pass.id}),
        data=b"",
        content_type="application/json",
    )

    body: dict[str, t.Any] = response.json()
    assert response.status_code == 400
    assert body["next_step"] == NextStep.COMPLETE_QUESTIONNAIRE
    assert body["questionnaires_missing"] == [str(questionnaire.id)]
    assert body["event_id"] == str(covered_events[0].id)
