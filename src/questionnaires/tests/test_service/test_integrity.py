"""Tests for QuestionnaireService integrity error handling."""

import uuid

import pytest

from questionnaires.exceptions import (
    QuestionIntegrityError,
    SectionIntegrityError,
)
from questionnaires.models import (
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


def test_create_mc_question_with_invalid_section_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating a multiple choice question with a section from another questionnaire."""
    service = QuestionnaireService(questionnaire.id)
    other_section = QuestionnaireSection.objects.create(questionnaire=another_questionnaire, name="Other Section")
    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        section_id=other_section.id,
        options=[MultipleChoiceOptionCreateSchema(option="Blue", is_correct=True)],
    )
    with pytest.raises(SectionIntegrityError):
        service.create_mc_question(payload)


def test_create_ft_question_with_invalid_section_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating a free text question with a section from another questionnaire."""
    service = QuestionnaireService(questionnaire.id)
    other_section = QuestionnaireSection.objects.create(questionnaire=another_questionnaire, name="Other Section")
    payload = FreeTextQuestionCreateSchema(question="Why?", section_id=other_section.id)
    with pytest.raises(SectionIntegrityError):
        service.create_ft_question(payload)


def test_create_mc_option_with_invalid_question_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating a multiple choice option with a question from another questionnaire."""
    service = QuestionnaireService(questionnaire.id)
    other_question = MultipleChoiceQuestion.objects.create(
        questionnaire=another_questionnaire, question="Other Question"
    )
    payload = MultipleChoiceOptionCreateSchema(option="Green", is_correct=False)
    with pytest.raises(QuestionIntegrityError):
        service.create_mc_option(other_question, payload)


def test_create_mc_question_with_mismatched_section_id_raises_error(
    questionnaire: Questionnaire, section: QuestionnaireSection
) -> None:
    """Test creating a multiple choice question with a mismatched section ID."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        section_id=section.id,
        options=[MultipleChoiceOptionCreateSchema(option="Blue", is_correct=True)],
    )
    with pytest.raises(SectionIntegrityError):
        service.create_mc_question(payload, section=QuestionnaireSection())  # Pass a different section object


def test_update_mc_question_with_nonexistent_section_raises_error(
    questionnaire: Questionnaire,
    single_answer_mc_question: MultipleChoiceQuestion,
) -> None:
    """Test updating a multiple choice question with a nonexistent section."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionUpdateSchema(
        question="What is your favorite color?",
        section_id=uuid.uuid4(),
        options=[],
    )
    with pytest.raises(SectionIntegrityError):
        service.update_mc_question(single_answer_mc_question, payload)


def test_create_ft_question_with_mismatched_section_id_raises_error(
    questionnaire: Questionnaire, section: QuestionnaireSection
) -> None:
    """Test creating a free text question with a mismatched section ID."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionCreateSchema(
        question="Why?",
        section_id=section.id,
    )
    with pytest.raises(SectionIntegrityError):
        service.create_ft_question(payload, section=QuestionnaireSection())  # Pass a different section object
