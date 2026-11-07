"""Series pass purchase also requires the covered events' own admission questionnaires (#1321, owner ruling)."""

import typing as t
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    EventSeries,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.service.event_manager import NextStep, Reasons
from events.service.series_pass_questionnaires import check_series_questionnaires
from events.tasks.series_pass import materialize_series_pass_holders
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)

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


def _cover(series_pass: SeriesPass, event: Event) -> None:
    tier = TicketTier.objects.create(
        event=event, name=f"Tier {event.slug}", price=Decimal("10.00"), currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )  # fmt: skip
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)


@pytest.fixture
def covered(organization: Organization, event_series: EventSeries) -> list[Event]:
    """Three upcoming covered events, earliest first."""
    return [_event(organization, event_series, f"ev{i}", 1 + 4 * i) for i in range(3)]


@pytest.fixture
def free_pass(organization: Organization, event_series: EventSeries, covered: list[Event]) -> SeriesPass:
    organization.visibility = Organization.Visibility.PUBLIC
    organization.save(update_fields=["visibility"])
    series_pass = SeriesPass.objects.create(
        event_series=event_series,
        name="Pass",
        price=Decimal("20.00"),
        pro_rata_discount=Decimal("0.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=SeriesPass.Visibility.PUBLIC,
    )
    for event in covered:
        _cover(series_pass, event)
    return series_pass


def _oq(
    organization: Organization,
    name: str,
    *,
    events: list[Event] | None = None,
    series: EventSeries | None = None,
    per_event: bool = False,
    members_exempt: bool = False,
    requires_evaluation: bool = False,
) -> OrganizationQuestionnaire:
    questionnaire = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    oq = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        per_event=per_event,
        members_exempt=members_exempt,
        requires_evaluation=requires_evaluation,
    )
    if events:
        oq.events.set(events)
    if series:
        oq.event_series.add(series)
    return oq


def _submit(user: RevelUser, oq: OrganizationQuestionnaire, event: Event | None = None) -> QuestionnaireSubmission:
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=oq.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        submitted_at=timezone.now(),
    )
    if event is not None:
        EventQuestionnaireSubmission.objects.create(
            user=user,
            event=event,
            questionnaire=oq.questionnaire,
            submission=submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
    return submission


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def test_event_only_questionnaire_blocks_purchase_until_submitted(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    oq = _oq(organization, "Event only", events=[covered[1]])
    url = reverse("api:checkout_series_pass", kwargs={"pass_id": free_pass.id})

    response = _client(revel_user).post(url, data=b"", content_type="application/json")
    body: dict[str, t.Any] = response.json()
    assert response.status_code == 400
    assert body["next_step"] == NextStep.COMPLETE_QUESTIONNAIRE
    assert body["questionnaires_missing"] == [str(oq.questionnaire_id)]
    assert body["event_id"] == str(covered[1].id)
    assert not HeldSeriesPass.objects.filter(user=revel_user).exists()

    _submit(revel_user, oq)
    response = _client(revel_user).post(url, data=b"", content_type="application/json")
    assert response.status_code == 200, response.content
    assert HeldSeriesPass.objects.filter(user=revel_user).exists()


def test_per_event_questionnaire_needs_a_submission_for_each_covered_event(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    oq = _oq(organization, "Per event", events=[covered[2], covered[1]], per_event=True)

    verdict = check_series_questionnaires(free_pass, revel_user)
    assert verdict is not None
    assert verdict.event_id == covered[1].id  # the earlier of the two
    assert verdict.questionnaires_missing == [oq.questionnaire_id]  # de-duplicated

    _submit(revel_user, oq, covered[1])
    verdict = check_series_questionnaires(free_pass, revel_user)
    assert verdict is not None
    assert verdict.event_id == covered[2].id

    _submit(revel_user, oq, covered[0])  # a submission for an event that does not carry it counts for nothing
    assert check_series_questionnaires(free_pass, revel_user) is not None

    _submit(revel_user, oq, covered[2])
    assert check_series_questionnaires(free_pass, revel_user) is None


def test_mixed_series_and_event_missing_gives_one_verdict_against_the_anchor(
    free_pass: SeriesPass,
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
    covered: list[Event],
) -> None:
    only_event = _oq(organization, "Event only", events=[covered[1]])
    series_oq = _oq(organization, "Series", series=event_series)

    verdict = check_series_questionnaires(free_pass, revel_user)

    assert verdict is not None
    assert verdict.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert verdict.event_id == covered[0].id  # series group first, reported against the anchor
    assert verdict.questionnaires_missing == [series_oq.questionnaire_id, only_event.questionnaire_id]


def test_members_exempt_event_questionnaire_skipped_for_member(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    _oq(organization, "Exempt", events=[covered[0]], members_exempt=True)
    assert check_series_questionnaires(free_pass, revel_user) is not None
    OrganizationMember.objects.create(organization=organization, user=revel_user)
    assert check_series_questionnaires(free_pass, revel_user) is None


def test_pending_review_event_questionnaire_gives_pending_verdict(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    oq = _oq(organization, "Needs review", events=[covered[0]], requires_evaluation=True)
    submission = _submit(revel_user, oq)
    QuestionnaireEvaluation.objects.create(
        submission=submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
    )

    verdict = check_series_questionnaires(free_pass, revel_user)

    assert verdict is not None
    assert verdict.reason_code == Reasons.QUESTIONNAIRE_PENDING_REVIEW.code


def test_past_covered_event_questionnaire_is_ignored(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    _oq(organization, "Past", events=[covered[0]])
    Event.objects.filter(pk=covered[0].pk).update(start=timezone.now() - timedelta(days=1))
    assert check_series_questionnaires(free_pass, revel_user) is None


def test_questionnaire_on_series_and_event_is_required_once(
    free_pass: SeriesPass,
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
    covered: list[Event],
) -> None:
    oq = _oq(organization, "Both", series=event_series, events=[covered[1], covered[2]])
    verdict = check_series_questionnaires(free_pass, revel_user)
    assert verdict is not None
    assert verdict.questionnaires_missing == [oq.questionnaire_id]
    assert verdict.event_id == covered[0].id
    _submit(revel_user, oq)
    assert check_series_questionnaires(free_pass, revel_user) is None


def test_quote_reflects_event_only_questionnaires(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    oq = _oq(organization, "Event only", events=[covered[2]])
    response = _client(revel_user).get(reverse("api:get_series_pass_quote", kwargs={"pass_id": free_pass.id}))
    body: dict[str, t.Any] = response.json()
    assert response.status_code == 200
    assert body["questionnaires_missing"] == [str(oq.questionnaire_id)]
    assert body["questionnaire_event_id"] == str(covered[2].id)


def test_query_count_independent_of_covered_event_count(
    free_pass: SeriesPass,
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
    covered: list[Event],
) -> None:
    """Owner/staff, links, questionnaires, covered-events prefetch, global and per_event submissions."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def grow(n: int) -> None:
        for i in range(n):
            event = _event(organization, event_series, f"grow-{n}-{i}", 20 + i)
            _cover(free_pass, event)
            _oq(organization, f"G{n}-{i}", events=[event])
            _oq(organization, f"P{n}-{i}", events=[event], per_event=True)

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            assert check_series_questionnaires(free_pass, revel_user) is not None
        return len(ctx)

    grow(1)
    one = count()
    grow(4)  # five grown events now
    assert count() == one
    assert one == 6


def _with_question(oq: OrganizationQuestionnaire) -> tuple[MultipleChoiceQuestion, MultipleChoiceOption]:
    question = MultipleChoiceQuestion.objects.create(
        questionnaire=oq.questionnaire, question=f"Q {oq.questionnaire.name}", is_mandatory=True
    )
    return question, MultipleChoiceOption.objects.create(question=question, option="Yes", is_correct=True)


def _submit_via_http(user: RevelUser, event_id: t.Any, oq_questionnaire_id: t.Any, mcq: t.Any, option: t.Any) -> int:
    url = reverse("api:submit_questionnaire", kwargs={"event_id": event_id, "questionnaire_id": oq_questionnaire_id})
    payload = {
        "questionnaire_id": str(oq_questionnaire_id),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],
    }
    return _client(user).post(url, data=orjson.dumps(payload), content_type="application/json").status_code


def test_quote_routes_each_missing_questionnaire_to_an_event_that_accepts_it(
    free_pass: SeriesPass,
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
    covered: list[Event],
) -> None:
    """A merged verdict spans groups; each questionnaire must be submitted through its own event."""
    series_oq = _oq(organization, "Series", series=event_series)
    event_oq = _oq(organization, "Event only", events=[covered[1]])
    questions = {oq.questionnaire_id: _with_question(oq) for oq in (series_oq, event_oq)}
    quote_url = reverse("api:get_series_pass_quote", kwargs={"pass_id": free_pass.id})

    body: dict[str, t.Any] = _client(revel_user).get(quote_url).json()
    assert body["questionnaires_missing"] == [str(series_oq.questionnaire_id), str(event_oq.questionnaire_id)]
    assert body["questionnaire_events"] == [
        {"questionnaire_id": str(series_oq.questionnaire_id), "event_id": str(covered[0].id)},
        {"questionnaire_id": str(event_oq.questionnaire_id), "event_id": str(covered[1].id)},
    ]
    # The anchor event does not carry the event-only questionnaire: submitting there would 404.
    mcq, option = questions[event_oq.questionnaire_id]
    assert _submit_via_http(revel_user, covered[0].id, event_oq.questionnaire_id, mcq, option) == 404

    for entry in body["questionnaire_events"]:
        mcq, option = questions[uuid.UUID(entry["questionnaire_id"])]
        assert _submit_via_http(revel_user, entry["event_id"], entry["questionnaire_id"], mcq, option) == 200

    body = _client(revel_user).get(quote_url).json()
    assert body["questionnaires_missing"] == []
    assert body["questionnaire_events"] == []
    checkout = _client(revel_user).post(
        reverse("api:checkout_series_pass", kwargs={"pass_id": free_pass.id}), data=b"", content_type="application/json"
    )
    assert checkout.status_code == 200, checkout.content


def test_per_event_event_only_questionnaire_maps_once_per_event(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    oq = _oq(organization, "Per event", events=[covered[1], covered[2]], per_event=True)
    body: dict[str, t.Any] = (
        _client(revel_user).get(reverse("api:get_series_pass_quote", kwargs={"pass_id": free_pass.id})).json()
    )
    assert body["questionnaires_missing"] == [str(oq.questionnaire_id)]
    assert body["questionnaire_events"] == [
        {"questionnaire_id": str(oq.questionnaire_id), "event_id": str(covered[1].id)},
        {"questionnaire_id": str(oq.questionnaire_id), "event_id": str(covered[2].id)},
    ]


def test_shared_event_only_questionnaire_is_reported_against_earliest_submittable_event(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    """B (earlier) is past its apply deadline, C is open: the questionnaire is reported against C."""
    oq = _oq(organization, "Shared", events=[covered[1], covered[2]])
    Event.objects.filter(pk=covered[1].pk).update(apply_before=timezone.now() - timedelta(hours=1))

    verdict = check_series_questionnaires(free_pass, revel_user)

    assert verdict is not None
    assert verdict.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert verdict.event_id == covered[2].id
    assert verdict.questionnaires_missing == [oq.questionnaire_id]


def test_shared_event_only_questionnaire_falls_back_to_earliest_when_none_submittable(
    free_pass: SeriesPass, organization: Organization, revel_user: RevelUser, covered: list[Event]
) -> None:
    _oq(organization, "Shared", events=[covered[1], covered[2]])
    Event.objects.filter(pk__in=[covered[1].pk, covered[2].pk]).update(apply_before=timezone.now() - timedelta(hours=1))

    verdict = check_series_questionnaires(free_pass, revel_user)

    assert verdict is not None
    assert verdict.next_step is None  # downgraded: nothing can take a submission
    assert verdict.reason_code == Reasons.APPLICATION_DEADLINE_PASSED.code


def test_extension_still_issues_tickets_to_holders_without_the_new_questionnaire(
    free_pass: SeriesPass,
    organization: Organization,
    event_series: EventSeries,
    revel_user: RevelUser,
) -> None:
    held = HeldSeriesPass.objects.create(
        series_pass=free_pass,
        user=revel_user,
        price_paid=Decimal("0.00"),
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
    )
    new_event = _event(organization, event_series, "late", 30)
    _cover(free_pass, new_event)
    _oq(organization, "New event questionnaire", events=[new_event])

    with patch("notifications.signals.series_pass.send_series_pass_extended"):
        materialize_series_pass_holders(str(free_pass.id), [str(new_event.id)])

    ticket = Ticket.objects.get(held_pass=held, event=new_event)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
