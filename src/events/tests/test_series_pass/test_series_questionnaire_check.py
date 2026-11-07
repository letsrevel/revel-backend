"""Direct tests for ``check_series_questionnaires`` (#1327): behaviour and a pinned query budget."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    EventRSVP,
    EventSeries,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service.event_manager import NextStep, Reasons
from events.service.series_pass_questionnaires import check_series_questionnaires
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

Fixture = t.Callable[..., t.Any]


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
    return [_event(organization, event_series, "soon", 1), _event(organization, event_series, "later", 5)]


@pytest.fixture
def series_pass(organization: Organization, event_series: EventSeries, covered_events: list[Event]) -> SeriesPass:
    sp = SeriesPass.objects.create(
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
        SeriesPassTierLink.objects.create(series_pass=sp, event=event, tier=tier)
    return sp


@pytest.fixture
def series_questionnaire(
    organization: Organization, event_series: EventSeries, questionnaire: Questionnaire
) -> OrganizationQuestionnaire:
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


# Query budget, non-privileged user with one non-exempt, non-per_event series questionnaire:
#   1. owner/staff check
#   2. anchor tier link (+ event) lookup
#   3. series questionnaires (+ questionnaire)
#   4. the user's ready submissions for the questionnaire ids
EXPECTED_QUERIES = 4


def test_query_count_missing_submission(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    django_assert_num_queries: t.Any,
) -> None:
    """No submission: COMPLETE_QUESTIONNAIRE for a fixed number of queries."""
    with django_assert_num_queries(EXPECTED_QUERIES):
        result = check_series_questionnaires(series_pass, revel_user)

    assert result is not None
    assert result.next_step == NextStep.COMPLETE_QUESTIONNAIRE


def test_query_count_independent_of_org_and_event_size(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    organization: Organization,
    covered_events: list[Event],
    series_questionnaire: OrganizationQuestionnaire,
    revel_user_factory: t.Any,
    django_assert_num_queries: t.Any,
) -> None:
    """Tickets, RSVPs, members and staff in the org and anchor event do not change the query count."""
    tier = covered_events[0].ticket_tiers.get(name="Tier soon")
    for i in range(30):
        other = revel_user_factory(username=f"extra{i}@example.com", email=f"extra{i}@example.com")
        OrganizationMember.objects.create(organization=organization, user=other)
        OrganizationStaff.objects.create(organization=organization, user=other)
        EventRSVP.objects.create(event=covered_events[0], user=other, status=EventRSVP.RsvpStatus.YES)
        Ticket.objects.create(event=covered_events[0], tier=tier, user=other, status=Ticket.TicketStatus.ACTIVE)

    with django_assert_num_queries(EXPECTED_QUERIES):
        result = check_series_questionnaires(series_pass, revel_user)

    assert result is not None
    assert result.reason_code == Reasons.QUESTIONNAIRE_MISSING.code


def test_owner_or_staff_true_issues_no_queries(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    django_assert_num_queries: t.Any,
) -> None:
    """A caller-supplied owner_or_staff=True short-circuits before any query."""
    with django_assert_num_queries(0):
        assert check_series_questionnaires(series_pass, revel_user, owner_or_staff=True) is None


def test_owner_or_staff_false_skips_staff_queries(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    django_assert_num_queries: t.Any,
) -> None:
    """A caller-supplied owner_or_staff=False skips the owner/staff query."""
    with django_assert_num_queries(EXPECTED_QUERIES - 1):
        assert check_series_questionnaires(series_pass, revel_user, owner_or_staff=False) is not None


def test_no_questionnaires_stops_early(
    series_pass: SeriesPass, revel_user: RevelUser, django_assert_num_queries: t.Any
) -> None:
    """Without series questionnaires: owner/staff + anchor + questionnaires, no submission queries."""
    with django_assert_num_queries(3):
        assert check_series_questionnaires(series_pass, revel_user) is None


def test_members_exempt_active_member_clear(
    series_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    django_assert_num_queries: t.Any,
) -> None:
    """An ACTIVE member is exempt: one membership query, no submission query."""
    series_questionnaire.members_exempt = True
    series_questionnaire.save()
    OrganizationMember.objects.create(organization=organization, user=revel_user)

    with django_assert_num_queries(EXPECTED_QUERIES):  # the membership check replaces the submissions query
        assert check_series_questionnaires(series_pass, revel_user) is None


def test_per_event_path_extra_query_accepts_other_covered_event(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
    django_assert_num_queries: t.Any,
) -> None:
    """per_event: one event-submission query (no global one); a submission for another covered event counts."""
    series_questionnaire.per_event = True
    series_questionnaire.save()
    EventQuestionnaireSubmission.objects.create(
        user=revel_user,
        event=covered_events[1],
        questionnaire=questionnaire,
        submission=_submit(revel_user, questionnaire),
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )

    with django_assert_num_queries(EXPECTED_QUERIES):
        assert check_series_questionnaires(series_pass, revel_user) is None


def test_rejected_evaluation_issues_no_lazy_queries(
    series_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    django_assert_num_queries: t.Any,
) -> None:
    """The rejected path reads oq.questionnaire and the evaluation without lazy loads."""
    series_questionnaire.requires_evaluation = True
    series_questionnaire.save()
    questionnaire.max_attempts = 1
    questionnaire.save()
    QuestionnaireEvaluation.objects.create(
        submission=_submit(revel_user, questionnaire),
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
    )

    with django_assert_num_queries(EXPECTED_QUERIES):
        result = check_series_questionnaires(series_pass, revel_user)

    assert result is not None
    assert result.reason_code == Reasons.QUESTIONNAIRE_FAILED.code
