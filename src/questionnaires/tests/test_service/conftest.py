"""Shared fixtures for QuestionnaireService tests."""

import pytest

from accounts.models import RevelUser
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)


@pytest.fixture
def complex_questionnaire(questionnaire: Questionnaire) -> Questionnaire:
    """Provides a more complex questionnaire with sections and various questions."""
    # Top-level questions (one mandatory, one not)
    mcq_top = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Top level MCQ", order=1, is_mandatory=True
    )
    MultipleChoiceOption.objects.create(question=mcq_top, option="Top Opt 1", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mcq_top, option="Top Opt 2", is_correct=False, order=2)

    FreeTextQuestion.objects.create(questionnaire=questionnaire, question="Top level FTQ", order=2, is_mandatory=False)

    # Section 1 with one mandatory question
    section1 = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Section 1", order=1)
    mcq_s1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, section=section1, question="Section 1 MCQ", order=1, is_mandatory=True
    )
    MultipleChoiceOption.objects.create(question=mcq_s1, option="S1 Opt 1", is_correct=True, order=1)

    # Section 2 with one mandatory question
    section2 = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Section 2", order=2)
    FreeTextQuestion.objects.create(
        questionnaire=questionnaire, section=section2, question="Section 2 FTQ", order=1, is_mandatory=True
    )

    questionnaire.refresh_from_db()
    return questionnaire


@pytest.fixture
def evaluator() -> RevelUser:
    """Create an evaluator user for testing."""
    return RevelUser.objects.create(username="evaluator", password="password")
