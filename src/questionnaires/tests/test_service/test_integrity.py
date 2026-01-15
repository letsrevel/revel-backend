"""Tests for QuestionnaireService integrity error handling."""

import uuid

import pytest

from questionnaires.exceptions import (
    QuestionIntegrityError,
    SectionIntegrityError,
)
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
    SectionCreateSchema,
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


# Tests for depends_on_option_id resolution


def test_create_mc_question_with_depends_on_option_id(
    questionnaire: Questionnaire, correct_option: MultipleChoiceOption
) -> None:
    """Test creating a conditional MC question using depends_on_option_id in payload."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionCreateSchema(
        question="Follow-up question",
        depends_on_option_id=correct_option.id,
        options=[MultipleChoiceOptionCreateSchema(option="Yes", is_correct=True)],
    )
    question = service.create_mc_question(payload)
    assert question.depends_on_option_id == correct_option.id
    assert question.depends_on_option == correct_option


def test_create_ft_question_with_depends_on_option_id(
    questionnaire: Questionnaire, correct_option: MultipleChoiceOption
) -> None:
    """Test creating a conditional FT question using depends_on_option_id in payload."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionCreateSchema(
        question="Please explain",
        depends_on_option_id=correct_option.id,
    )
    question = service.create_ft_question(payload)
    assert question.depends_on_option_id == correct_option.id
    assert question.depends_on_option == correct_option


def test_create_mc_question_with_invalid_depends_on_option_id_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating an MC question with depends_on_option_id from another questionnaire."""
    # Create an option in another questionnaire
    other_question = MultipleChoiceQuestion.objects.create(
        questionnaire=another_questionnaire, question="Other Question"
    )
    other_option = MultipleChoiceOption.objects.create(question=other_question, option="Other Option", is_correct=True)

    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionCreateSchema(
        question="Conditional question",
        depends_on_option_id=other_option.id,
        options=[MultipleChoiceOptionCreateSchema(option="Yes", is_correct=True)],
    )
    with pytest.raises(QuestionIntegrityError):
        service.create_mc_question(payload)


def test_create_ft_question_with_invalid_depends_on_option_id_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating an FT question with depends_on_option_id from another questionnaire."""
    # Create an option in another questionnaire
    other_question = MultipleChoiceQuestion.objects.create(
        questionnaire=another_questionnaire, question="Other Question"
    )
    other_option = MultipleChoiceOption.objects.create(question=other_question, option="Other Option", is_correct=True)

    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionCreateSchema(
        question="Conditional question",
        depends_on_option_id=other_option.id,
    )
    with pytest.raises(QuestionIntegrityError):
        service.create_ft_question(payload)


def test_create_mc_question_with_nonexistent_depends_on_option_id_raises_error(
    questionnaire: Questionnaire,
) -> None:
    """Test creating an MC question with a nonexistent depends_on_option_id."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionCreateSchema(
        question="Conditional question",
        depends_on_option_id=uuid.uuid4(),  # Non-existent UUID
        options=[MultipleChoiceOptionCreateSchema(option="Yes", is_correct=True)],
    )
    with pytest.raises(QuestionIntegrityError):
        service.create_mc_question(payload)


def test_create_ft_question_with_nonexistent_depends_on_option_id_raises_error(
    questionnaire: Questionnaire,
) -> None:
    """Test creating an FT question with a nonexistent depends_on_option_id."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionCreateSchema(
        question="Conditional question",
        depends_on_option_id=uuid.uuid4(),  # Non-existent UUID
    )
    with pytest.raises(QuestionIntegrityError):
        service.create_ft_question(payload)


# Tests for create_section depends_on_option_id resolution


def test_create_section_with_depends_on_option_id(
    questionnaire: Questionnaire, correct_option: MultipleChoiceOption
) -> None:
    """Test creating a conditional section using depends_on_option_id in payload."""
    service = QuestionnaireService(questionnaire.id)
    payload = SectionCreateSchema(
        name="Conditional Section",
        depends_on_option_id=correct_option.id,
    )
    section = service.create_section(payload)
    assert section.depends_on_option_id == correct_option.id
    assert section.depends_on_option == correct_option


def test_create_section_with_invalid_depends_on_option_id_raises_error(
    questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test creating a section with depends_on_option_id from another questionnaire."""
    # Create an option in another questionnaire
    other_question = MultipleChoiceQuestion.objects.create(
        questionnaire=another_questionnaire, question="Other Question"
    )
    other_option = MultipleChoiceOption.objects.create(question=other_question, option="Other Option", is_correct=True)

    service = QuestionnaireService(questionnaire.id)
    payload = SectionCreateSchema(
        name="Conditional Section",
        depends_on_option_id=other_option.id,
    )
    with pytest.raises(SectionIntegrityError):
        service.create_section(payload)


def test_create_section_with_nonexistent_depends_on_option_id_raises_error(
    questionnaire: Questionnaire,
) -> None:
    """Test creating a section with a nonexistent depends_on_option_id."""
    service = QuestionnaireService(questionnaire.id)
    payload = SectionCreateSchema(
        name="Conditional Section",
        depends_on_option_id=uuid.uuid4(),  # Non-existent UUID
    )
    with pytest.raises(SectionIntegrityError):
        service.create_section(payload)
