"""Tests for QuestionnaireService CRUD operations."""

from decimal import Decimal

import pytest

from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    FreeTextQuestionUpdateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceOptionUpdateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
    QuestionnaireCreateSchema,
    SectionCreateSchema,
    SectionUpdateSchema,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


# --- Tests for create_questionnaire ---


def test_create_questionnaire_from_schema() -> None:
    """Test that a complete questionnaire can be created from a schema."""
    payload = QuestionnaireCreateSchema(
        name="Full Test Questionnaire",
        min_score=Decimal(80),
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.HYBRID,
        llm_guidelines="Be very nice.",
        multiplechoicequestion_questions=[
            MultipleChoiceQuestionCreateSchema(
                question="Top Level MCQ",
                order=1,
                options=[
                    MultipleChoiceOptionCreateSchema(option="Top Opt 1", is_correct=True),
                    MultipleChoiceOptionCreateSchema(option="Top Opt 2"),
                ],
            )
        ],
        freetextquestion_questions=[FreeTextQuestionCreateSchema(question="Top Level FTQ", order=2)],
        sections=[
            SectionCreateSchema(
                name="Section One",
                order=1,
                multiplechoicequestion_questions=[
                    MultipleChoiceQuestionCreateSchema(
                        question="Section 1 MCQ",
                        allow_multiple_answers=True,
                        options=[
                            MultipleChoiceOptionCreateSchema(option="S1 Opt 1", is_correct=True),
                            MultipleChoiceOptionCreateSchema(option="S1 Opt 2", is_correct=True),
                        ],
                    )
                ],
                freetextquestion_questions=[],
            ),
            SectionCreateSchema(
                name="Section Two",
                order=2,
                multiplechoicequestion_questions=[],
                freetextquestion_questions=[FreeTextQuestionCreateSchema(question="Section 2 FTQ")],
            ),
        ],
    )

    # Action
    questionnaire = QuestionnaireService.create_questionnaire(payload)

    # Assertions
    assert questionnaire.name == "Full Test Questionnaire"
    assert questionnaire.min_score == 80
    assert questionnaire.llm_guidelines == "Be very nice."
    assert questionnaire.evaluation_mode == "hybrid"

    # Check top-level questions
    assert questionnaire.multiplechoicequestion_questions.filter(section__isnull=True).count() == 1
    top_mcq = questionnaire.multiplechoicequestion_questions.filter(section__isnull=True).first()
    assert top_mcq is not None
    assert questionnaire.freetextquestion_questions.filter(section__isnull=True).count() == 1
    top_ftq = questionnaire.freetextquestion_questions.filter(section__isnull=True).first()
    assert top_ftq is not None

    # Check sections
    assert questionnaire.sections.count() == 2
    section1 = questionnaire.sections.get(name="Section One")
    section2 = questionnaire.sections.get(name="Section Two")
    assert section1.order == 1
    assert section2.order == 2

    # Check questions within sections
    assert section1.multiplechoicequestion_questions.count() == 1
    s1_mcq = section1.multiplechoicequestion_questions.first()
    assert s1_mcq is not None
    assert s1_mcq.question == "Section 1 MCQ"
    assert s1_mcq.allow_multiple_answers is True
    assert s1_mcq.options.count() == 2
    assert s1_mcq.options.filter(is_correct=True).count() == 2
    assert s1_mcq.questionnaire == questionnaire

    assert section2.freetextquestion_questions.count() == 1
    s2_ftq = section2.freetextquestion_questions.first()
    assert s2_ftq is not None
    assert s2_ftq.question == "Section 2 FTQ"
    assert s2_ftq.questionnaire == questionnaire


# --- Tests for section CRUD ---


def test_create_section(questionnaire: Questionnaire) -> None:
    """Test that a section can be created."""
    service = QuestionnaireService(questionnaire.id)
    payload = SectionCreateSchema(name="New Section", order=1)
    section = service.create_section(payload)
    assert section.name == "New Section"
    assert section.order == 1
    assert section.questionnaire == questionnaire
    assert QuestionnaireSection.objects.count() == 1


def test_update_section(questionnaire: Questionnaire, section: QuestionnaireSection) -> None:
    """Test that a section can be updated."""
    service = QuestionnaireService(questionnaire.id)
    payload = SectionUpdateSchema(name="Updated Section", order=2)
    section = QuestionnaireSection.objects.get(id=section.id)
    updated_section = service.update_section(section, payload)
    assert updated_section.name == "Updated Section"
    assert updated_section.order == 2


def test_update_section_with_questions(questionnaire: Questionnaire, section: QuestionnaireSection) -> None:
    """Test that a section can be updated with questions."""
    service = QuestionnaireService(questionnaire.id)
    payload = SectionUpdateSchema(
        name="Updated Section",
        order=2,
        multiplechoicequestion_questions=[
            MultipleChoiceQuestionCreateSchema(
                question="What is your favorite color?",
                options=[MultipleChoiceOptionCreateSchema(option="Blue", is_correct=True)],
            )
        ],
        freetextquestion_questions=[FreeTextQuestionCreateSchema(question="Why?")],
    )
    updated_section = service.update_section(section, payload)
    assert updated_section.name == "Updated Section"
    assert updated_section.order == 2
    assert updated_section.multiplechoicequestion_questions.count() == 1
    assert updated_section.freetextquestion_questions.count() == 1


# --- Tests for MC question CRUD ---


def test_create_mc_question(questionnaire: Questionnaire, section: QuestionnaireSection) -> None:
    """Test that a multiple choice question can be created."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        section_id=section.id,
        options=[MultipleChoiceOptionCreateSchema(option="Blue", is_correct=True)],
    )
    question = service.create_mc_question(payload)
    assert question.question == "What is your favorite color?"
    assert question.section == section
    assert question.options.count() == 1
    assert MultipleChoiceQuestion.objects.count() == 1


def test_update_mc_question(questionnaire: Questionnaire, single_answer_mc_question: MultipleChoiceQuestion) -> None:
    """Test that a multiple choice question can be updated."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionUpdateSchema(
        question="What is your favorite color?",
        options=[
            MultipleChoiceOptionCreateSchema(option="Red", is_correct=True),
            MultipleChoiceOptionCreateSchema(option="Blue", is_correct=False),
        ],
    )
    updated_question = service.update_mc_question(single_answer_mc_question, payload)
    assert updated_question.options.count() == 2
    correct_option = updated_question.options.filter(is_correct=True).first()
    assert correct_option is not None
    assert correct_option.option == "Red"


def test_update_mc_question_without_options(
    questionnaire: Questionnaire, single_answer_mc_question: MultipleChoiceQuestion
) -> None:
    """Test that a multiple choice question can be updated without options."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionUpdateSchema(question="What is your favorite color?", options=[])
    updated_question = service.update_mc_question(single_answer_mc_question, payload)
    assert updated_question.options.count() == 0


def test_update_mc_question_with_section_move(
    questionnaire: Questionnaire,
    single_answer_mc_question: MultipleChoiceQuestion,
    section: QuestionnaireSection,
) -> None:
    """Test that a multiple choice question can be moved to a different section."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceQuestionUpdateSchema(
        question="What is your favorite color?",
        section_id=section.id,
        options=[],
    )
    updated_question = service.update_mc_question(single_answer_mc_question, payload)
    assert updated_question.section == section


# --- Tests for FT question CRUD ---


def test_create_ft_question(questionnaire: Questionnaire, section: QuestionnaireSection) -> None:
    """Test that a free text question can be created."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionCreateSchema(question="Why?", section_id=section.id)
    question = service.create_ft_question(payload)
    assert question.question == "Why?"
    assert question.section == section
    assert FreeTextQuestion.objects.count() == 1


def test_update_ft_question(questionnaire: Questionnaire, free_text_question: FreeTextQuestion) -> None:
    """Test that a free text question can be updated."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionUpdateSchema(question="Why not?")
    updated_question = service.update_ft_question(free_text_question, payload)
    assert updated_question.question == "Why not?"


def test_update_ft_question_with_section(
    questionnaire: Questionnaire,
    free_text_question: FreeTextQuestion,
    section: QuestionnaireSection,
) -> None:
    """Test that a free text question can be updated with a section."""
    service = QuestionnaireService(questionnaire.id)
    payload = FreeTextQuestionUpdateSchema(question="Why not?", section_id=section.id)
    updated_question = service.update_ft_question(free_text_question, payload)
    assert updated_question.section == section


# --- Tests for MC option CRUD ---


def test_create_mc_option(questionnaire: Questionnaire, single_answer_mc_question: MultipleChoiceQuestion) -> None:
    """Test that a multiple choice option can be created."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceOptionCreateSchema(option="Green", is_correct=False)
    option = service.create_mc_option(single_answer_mc_question, payload)
    assert option.option == "Green"
    assert option.is_correct is False
    assert single_answer_mc_question.options.count() == 1


def test_update_mc_option(questionnaire: Questionnaire, correct_option: MultipleChoiceOption) -> None:
    """Test that a multiple choice option can be updated."""
    service = QuestionnaireService(questionnaire.id)
    payload = MultipleChoiceOptionUpdateSchema(option="Yellow", is_correct=True)
    updated_option = service.update_mc_option(correct_option, payload)
    assert updated_option.option == "Yellow"
    assert updated_option.is_correct is True
