"""Shared fixtures for event service tests."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization
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
