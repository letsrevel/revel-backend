"""The series pass quote exposes the series admission questionnaire requirement (#1323)."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationStaff,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

# Pinned, measured query count of an authenticated quote request: SAVEPOINT + RELEASE (test transaction),
# user, pass, banned-membership / telegram / blacklist lookups (auth + visibility), series visibility,
# is_owner_or_staff (staff check, twice: visibility + questionnaire gate), tier links (quote + anchor),
# series questionnaires, submissions. Nothing scales with org members or events.
QUOTE_QUERIES = 14


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
    """Two upcoming covered events; the earlier one is the anchor."""
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


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def _quote(series_pass: SeriesPass, user: RevelUser | None = None) -> dict[str, t.Any]:
    client = _client(user) if user else Client()
    response = client.get(reverse("api:get_series_pass_quote", kwargs={"pass_id": series_pass.id}))
    assert response.status_code == 200, response.content
    body: dict[str, t.Any] = response.json()
    return body


def _assert_clear(body: dict[str, t.Any]) -> None:
    assert body["questionnaires_missing"] == []
    assert body["questionnaire_event_id"] is None


def test_anonymous_sees_nothing(free_pass: SeriesPass, series_questionnaire: OrganizationQuestionnaire) -> None:
    _assert_clear(_quote(free_pass))


def test_missing_questionnaire_listed(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    body = _quote(free_pass, revel_user)

    assert body["questionnaires_missing"] == [str(questionnaire.id)]
    assert body["questionnaire_event_id"] == str(covered_events[0].id)


def test_satisfied_is_clear(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
) -> None:
    _submit(revel_user, questionnaire)

    _assert_clear(_quote(free_pass, revel_user))


def test_owner_is_clear(
    free_pass: SeriesPass, organization_owner_user: RevelUser, series_questionnaire: OrganizationQuestionnaire
) -> None:
    _assert_clear(_quote(free_pass, organization_owner_user))


def test_staff_is_clear(
    free_pass: SeriesPass,
    staff_member: OrganizationStaff,
    organization_staff_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    _assert_clear(_quote(free_pass, organization_staff_user))


def test_active_member_exempt_is_clear(
    free_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
) -> None:
    series_questionnaire.members_exempt = True
    series_questionnaire.save()
    OrganizationMember.objects.create(organization=organization, user=revel_user)

    _assert_clear(_quote(free_pass, revel_user))


def test_paused_member_listed(
    free_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    series_questionnaire.members_exempt = True
    series_questionnaire.save()
    OrganizationMember.objects.create(
        organization=organization, user=revel_user, status=OrganizationMember.MembershipStatus.PAUSED
    )

    body = _quote(free_pass, revel_user)

    assert body["questionnaires_missing"] == [str(questionnaire.id)]
    assert body["questionnaire_event_id"] == str(covered_events[0].id)


@pytest.mark.parametrize("with_evaluation", [False, True])
def test_pending_review_is_clear(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    with_evaluation: bool,
) -> None:
    series_questionnaire.requires_evaluation = True
    series_questionnaire.save()
    submission = _submit(revel_user, questionnaire)
    if with_evaluation:
        QuestionnaireEvaluation.objects.create(
            submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
        )

    _assert_clear(_quote(free_pass, revel_user))


def test_dead_end_is_clear(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    covered_events: list[Event],
) -> None:
    Event.objects.filter(pk__in=[e.pk for e in covered_events]).update(apply_before=timezone.now() - timedelta(hours=1))

    _assert_clear(_quote(free_pass, revel_user))


def test_anchor_skips_non_open_event(
    free_pass: SeriesPass,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    questionnaire: Questionnaire,
    covered_events: list[Event],
) -> None:
    Event.objects.filter(pk=covered_events[0].pk).update(status=Event.EventStatus.CANCELLED)

    body = _quote(free_pass, revel_user)

    assert body["questionnaires_missing"] == [str(questionnaire.id)]
    assert body["questionnaire_event_id"] == str(covered_events[1].id)


def test_query_count_is_size_independent(
    django_assert_num_queries: t.Any,
    free_pass: SeriesPass,
    organization: Organization,
    revel_user: RevelUser,
    series_questionnaire: OrganizationQuestionnaire,
    covered_events: list[Event],
) -> None:
    """The quote costs a fixed number of queries however many members/events the org has."""
    _quote(free_pass, revel_user)  # warm caches (content types etc.)
    client = _client(revel_user)
    url = reverse("api:get_series_pass_quote", kwargs={"pass_id": free_pass.id})

    with django_assert_num_queries(QUOTE_QUERIES):
        assert client.get(url).status_code == 200

    for i in range(20):
        other = RevelUser.objects.create_user(username=f"bulk{i}@example.com", email=f"bulk{i}@example.com")
        OrganizationMember.objects.create(organization=organization, user=other)
        _event(organization, free_pass.event_series, f"extra{i}", 10 + i)

    with django_assert_num_queries(QUOTE_QUERIES):
        assert client.get(url).status_code == 200
