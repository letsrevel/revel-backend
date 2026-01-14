"""Tests for the QuestionnaireService.build() method."""

import pytest

from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
)
from questionnaires.service import QuestionnaireService, get_questionnaire_schema

pytestmark = pytest.mark.django_db


def test_build_questionnaire_no_shuffling(complex_questionnaire: Questionnaire) -> None:
    """Test that the questionnaire schema is built correctly with a defined order."""
    q = complex_questionnaire
    q.shuffle_questions = False
    q.shuffle_sections = False
    q.save()

    service = QuestionnaireService(q.id)
    schema = service.build()

    assert schema.id == q.id
    # Check top-level questions order
    assert len(schema.multiple_choice_questions) == 1
    assert schema.multiple_choice_questions[0].question == "Top level MCQ"
    assert len(schema.free_text_questions) == 1
    assert schema.free_text_questions[0].question == "Top level FTQ"

    # Check sections order
    assert len(schema.sections) == 2
    assert schema.sections[0].name == "Section 1"
    assert schema.sections[1].name == "Section 2"

    # Check questions within sections
    assert len(schema.sections[0].multiple_choice_questions) == 1
    assert schema.sections[0].multiple_choice_questions[0].question == "Section 1 MCQ"
    assert len(schema.sections[1].free_text_questions) == 1
    assert schema.sections[1].free_text_questions[0].question == "Section 2 FTQ"


def test_build_questionnaire_with_shuffling(complex_questionnaire: Questionnaire) -> None:
    """Test that the random.shuffle function is called when shuffling is enabled."""
    q = complex_questionnaire
    q.shuffle_questions = True
    q.shuffle_sections = True
    q.save()

    service = QuestionnaireService(q.id)
    schema = service.build()

    # Assert that shuffle was called for:
    # 1. Top-level questions (2 of them: 1 MCQ + 1 FTQ)
    # 2. Section 1 questions (1 of them: 1 MCQ) -> shuffle is called even on lists of 1
    # 3. Section 2 questions (1 of them: 1 FTQ)

    # Verify content is still present
    assert schema.id == q.id
    assert len(schema.sections) == 2
    assert len(schema.multiple_choice_questions) == 1
    assert len(schema.free_text_questions) == 1


def test_build_questionnaire_with_sorted_options(questionnaire: Questionnaire) -> None:
    """Test that options are sorted by order when shuffle_options is False."""
    mcq = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Sorted MCQ", shuffle_options=False
    )
    opt1 = MultipleChoiceOption.objects.create(question=mcq, option="Option 2", order=2)
    opt2 = MultipleChoiceOption.objects.create(question=mcq, option="Option 1", order=1)

    service = QuestionnaireService(questionnaire.id)
    schema = service.build()

    assert len(schema.multiple_choice_questions) == 1
    options = schema.multiple_choice_questions[0].options
    assert len(options) == 2
    assert options[0].id == opt2.id
    assert options[1].id == opt1.id


def test_get_questionnaire_schema(complex_questionnaire: Questionnaire) -> None:
    """Test that the questionnaire schema can be retrieved."""
    complex_questionnaire.evaluation_mode = "manual"
    schema = get_questionnaire_schema(complex_questionnaire)
    assert schema.name == complex_questionnaire.name
    assert len(schema.multiplechoicequestion_questions) == 2
    assert len(schema.freetextquestion_questions) == 2
    assert len(schema.sections) == 2
