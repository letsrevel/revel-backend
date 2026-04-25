"""Shared fixtures for event service tests."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, RecurrenceRule, TicketTier
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
)
from questionnaires.service.questionnaire_service import QuestionnaireService


@pytest.fixture
def eq_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User for event questionnaire testing."""
    return django_user_model.objects.create_user(
        username="eq_test_user",
        email="eqtest@example.com",
        password="pass",
    )


@pytest.fixture
def eq_org(django_user_model: type[RevelUser]) -> Organization:
    """Organization for event questionnaire testing."""
    owner = django_user_model.objects.create_user(
        username="eq_org_owner",
        email="eqorgowner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="EQ Test Org",
        slug="eq-test-org",
        owner=owner,
    )


@pytest.fixture
def eq_event(eq_org: Organization) -> Event:
    """An event for questionnaire testing."""
    return Event.objects.create(
        organization=eq_org,
        name="Test Event For Questionnaire",
        slug="test-event-questionnaire",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def eq_questionnaire() -> Questionnaire:
    """A questionnaire for testing."""
    return Questionnaire.objects.create(
        name="EQ Test Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )


@pytest.fixture
def eq_mcq(eq_questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """A multiple choice question with options."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=eq_questionnaire,
        question="Test question?",
        is_mandatory=True,
        order=1,
    )


@pytest.fixture
def eq_option(eq_mcq: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """An option for the MCQ."""
    return MultipleChoiceOption.objects.create(
        question=eq_mcq,
        option="Test Option",
        is_correct=True,
    )


@pytest.fixture
def eq_questionnaire_service(
    eq_questionnaire: Questionnaire,
    eq_mcq: MultipleChoiceQuestion,
) -> QuestionnaireService:
    """QuestionnaireService instance for the test questionnaire."""
    return QuestionnaireService(eq_questionnaire.id)


# ---------------------------------------------------------------------------
# Recurrence service fixtures (shared across the split test_recurrence_*.py
# files: materialize, lifecycle, propagation).
# ---------------------------------------------------------------------------


@pytest.fixture
def weekly_rule(organization: Organization) -> RecurrenceRule:
    """Create a weekly recurrence rule on Mondays, starting from a known date."""
    dtstart = timezone.make_aware(datetime(2026, 4, 6, 10, 0))  # Monday
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],  # Monday
        dtstart=dtstart,
    )
    return rule


@pytest.fixture
def template_event(
    organization: Organization,
    event_series: EventSeries,
    weekly_rule: RecurrenceRule,
) -> Event:
    """Create a template event for the series.

    Anchors ``start`` to ``weekly_rule.dtstart`` so the rule and its template
    describe the exact same schedule. Without this, changing one literal in
    ``weekly_rule`` would leave the template pointing at the wrong date.
    """
    start = weekly_rule.dtstart
    event = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name="Weekly Meetup",
        start=start,
        end=start + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
        requires_ticket=True,
    )
    return event


@pytest.fixture
def template_event_with_tier(template_event: Event) -> Event:
    """Create a template event that has a ticket tier.

    The template_event has requires_ticket=True, so a default "General Admission"
    tier is auto-created by the signal. We update it with the desired price/quantity
    instead of creating a duplicate.
    """
    tier = TicketTier.objects.get(event=template_event, name="General Admission")
    tier.price = 15.00
    tier.total_quantity = 50
    tier.save(update_fields=["price", "total_quantity"])
    return template_event


@pytest.fixture
def active_series(
    event_series: EventSeries,
    weekly_rule: RecurrenceRule,
    template_event: Event,
) -> EventSeries:
    """An active series with a recurrence rule and template event."""
    event_series.recurrence_rule = weekly_rule
    event_series.template_event = template_event
    event_series.is_active = True
    event_series.auto_publish = False
    event_series.generation_window_weeks = 4
    event_series.save()
    return event_series


@pytest.fixture
def active_series_with_tier(
    event_series: EventSeries,
    weekly_rule: RecurrenceRule,
    template_event_with_tier: Event,
) -> EventSeries:
    """An active series whose template has a ticket tier."""
    event_series.recurrence_rule = weekly_rule
    event_series.template_event = template_event_with_tier
    event_series.is_active = True
    event_series.auto_publish = False
    event_series.generation_window_weeks = 4
    event_series.save()
    return event_series


# ---------------------------------------------------------------------------
# Cancellation service fixtures
# ---------------------------------------------------------------------------
# tier_online_with_cancellation_enabled and tier_online_with_cancellation_disabled
# are defined in src/events/tests/conftest.py for DRY access across both subtrees.
