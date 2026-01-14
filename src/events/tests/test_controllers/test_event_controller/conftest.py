"""Shared fixtures for event controller tests."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from events.models import (
    Event,
    Organization,
    OrganizationQuestionnaire,
    TicketTier,
)
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
)


@pytest.fixture
def next_week() -> datetime:
    return timezone.now() + timedelta(days=7)


@pytest.fixture
def rsvp_only_public_event(organization: Organization) -> Event:
    """A public event that only requires an RSVP, not a ticket."""
    return Event.objects.create(
        organization=organization,
        name="RSVP-Only Public Event",
        slug="rsvp-only-public-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        requires_ticket=False,  # Key difference
        start=timezone.now(),
    )


@pytest.fixture
def event_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire linked to the public_event."""
    q = Questionnaire.objects.create(name="Event Questionnaire", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    # Add one mandatory MCQ and one optional FTQ
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Mandatory MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="Correct", is_correct=True)
    FreeTextQuestion.objects.create(questionnaire=q, question="Optional FTQ", is_mandatory=False)
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def auto_eval_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with only MCQs, suitable for auto-evaluation."""
    q = Questionnaire.objects.create(
        name="Auto-Eval Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,  # Ensure auto mode
    )
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Auto-Eval MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="OK", is_correct=True)
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    """Create a free ticket tier for convenience."""
    return TicketTier.objects.create(
        event=public_event,
        name="Free Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
    )
