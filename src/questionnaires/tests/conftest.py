"""conftest.py: Fixtures for the questionnaires app."""

import typing as t

import pytest
from django.contrib.auth import get_user_model

from events.models import Organization, OrganizationQuestionnaire
from questionnaires.llms.llm_backends import MockEvaluator
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

User = get_user_model()


@pytest.fixture
def user() -> t.Any:
    """Provides a standard user instance."""
    return User.objects.create_user(username="testuser", password="password")


@pytest.fixture
def organization(user: t.Any) -> t.Any:
    """Provides an Organization instance for questionnaire tests."""

    return Organization.objects.create(name="Test Organization", slug="test-org", owner=user)


@pytest.fixture
def org_questionnaire(organization: Organization, questionnaire: Questionnaire) -> OrganizationQuestionnaire:
    """Link the questionnaire to the organization.

    This is required for the notification system which needs to determine
    which organization a questionnaire belongs to when sending evaluation notifications.
    """

    return OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)


@pytest.fixture
def another_questionnaire() -> Questionnaire:
    """Provides a second, distinct Questionnaire instance."""
    return Questionnaire.objects.create(name="Another Questionnaire")


@pytest.fixture
def draft_submission(user: t.Any, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    """Provides a draft submission for the standard user and questionnaire."""
    return QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)


@pytest.fixture
def submitted_submission(draft_submission: QuestionnaireSubmission) -> QuestionnaireSubmission:
    """Provides a submitted submission, ready for evaluation."""
    draft_submission.status = QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    draft_submission.save()
    return draft_submission


@pytest.fixture
def section(questionnaire: Questionnaire) -> QuestionnaireSection:
    """Provides a section linked to the main questionnaire."""
    return QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Section 1", order=1)


@pytest.fixture
def single_answer_mc_question(questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """Provides a MultipleChoiceQuestion that allows only one answer."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="What is your favorite color?", allow_multiple_answers=False, order=1
    )


@pytest.fixture
def multi_answer_mc_question(questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """Provides a MultipleChoiceQuestion that allows multiple answers."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Which colors do you like?", allow_multiple_answers=True, order=2
    )


@pytest.fixture
def correct_option(single_answer_mc_question: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """Provides a correct option for the single-answer question."""
    return MultipleChoiceOption.objects.create(question=single_answer_mc_question, option="Blue", is_correct=True)


@pytest.fixture
def incorrect_option(single_answer_mc_question: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """Provides an incorrect option for the single-answer question."""
    return MultipleChoiceOption.objects.create(question=single_answer_mc_question, option="Red", is_correct=False)


@pytest.fixture
def free_text_question(questionnaire: Questionnaire) -> FreeTextQuestion:
    """Provides a FreeTextQuestion instance."""
    return FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Explain your reasoning.", order=3, llm_guidelines="Be concise."
    )


@pytest.fixture
def mock_evaluator() -> MockEvaluator:
    """Provides an instance of the MockBatchEvaluator."""
    return MockEvaluator()
