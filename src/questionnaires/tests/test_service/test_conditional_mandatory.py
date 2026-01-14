"""Tests for conditional mandatory questions in QuestionnaireService.submit()."""

import pytest

from accounts.models import RevelUser
from questionnaires.exceptions import MissingMandatoryAnswerError
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    MultipleChoiceSubmissionSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


def test_submit_conditional_mc_question_not_required_when_option_not_selected(
    user: RevelUser, questionnaire: Questionnaire
) -> None:
    """Test that a conditional mandatory MC question doesn't require an answer when its condition is not met."""
    # Q1: "Do you have allergies?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you have allergies?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)
    q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=False, order=2)

    # Q2: Conditional mandatory question that depends on Q1=Yes
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Are any life-threatening?",
        order=2,
        is_mandatory=True,
        depends_on_option=q1_yes,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Yes", is_correct=True, order=1)

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=No, so Q2 should NOT be required
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_no.id]),
        ],
    )

    # Should NOT raise MissingMandatoryAnswerError because Q2 is not applicable
    submission = service.submit(user, submission_schema)
    assert submission.pk is not None
    assert submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY


def test_submit_conditional_mc_question_required_when_option_selected(
    user: RevelUser, questionnaire: Questionnaire
) -> None:
    """Test that a conditional mandatory MC question requires an answer when its condition IS met."""
    # Q1: "Do you have allergies?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you have allergies?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)

    # Q2: Conditional mandatory question that depends on Q1=Yes
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        question="Are any life-threatening?",
        order=2,
        is_mandatory=True,
        depends_on_option=q1_yes,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Yes", is_correct=True, order=1)

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=Yes, so Q2 SHOULD be required
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_yes.id]),
        ],
    )

    # Should raise MissingMandatoryAnswerError because Q2 is applicable but not answered
    with pytest.raises(MissingMandatoryAnswerError):
        service.submit(user, submission_schema)

    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_conditional_ft_question_not_required_when_option_not_selected(
    user: RevelUser, questionnaire: Questionnaire
) -> None:
    """Test that a conditional mandatory free text question doesn't require an answer when its condition is not met."""
    # Q1: "Do you have allergies?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you have allergies?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)
    q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=False, order=2)

    # Q2: Conditional mandatory free text question that depends on Q1=Yes
    FreeTextQuestion.objects.create(
        questionnaire=questionnaire,
        question="Please describe your allergies",
        order=2,
        is_mandatory=True,
        depends_on_option=q1_yes,
    )

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=No, so Q2 should NOT be required
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_no.id]),
        ],
    )

    # Should NOT raise MissingMandatoryAnswerError because Q2 is not applicable
    submission = service.submit(user, submission_schema)
    assert submission.pk is not None


def test_submit_conditional_section_questions_not_required_when_section_not_applicable(
    user: RevelUser, questionnaire: Questionnaire
) -> None:
    """Test that mandatory questions in a conditional section don't require answers when section is not applicable."""
    # Q1: "Do you want details?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you want details?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)
    q1_no = MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=False, order=2)

    # Conditional section that depends on Q1=Yes
    section = QuestionnaireSection.objects.create(
        questionnaire=questionnaire, name="Details Section", order=1, depends_on_option=q1_yes
    )

    # Q2: Mandatory question inside the conditional section
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Please provide details",
        order=2,
        is_mandatory=True,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Detail A", is_correct=True, order=1)

    # Q3: Mandatory free text question inside the conditional section
    FreeTextQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Explain in detail",
        order=3,
        is_mandatory=True,
    )

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=No, so the section and Q2/Q3 should NOT be required
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_no.id]),
        ],
    )

    # Should NOT raise MissingMandatoryAnswerError because Q2 and Q3 are in a non-applicable section
    submission = service.submit(user, submission_schema)
    assert submission.pk is not None


def test_submit_conditional_section_questions_required_when_section_applicable(
    user: RevelUser, questionnaire: Questionnaire
) -> None:
    """Test that mandatory questions in a conditional section require answers when section IS applicable."""
    # Q1: "Do you want details?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you want details?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)

    # Conditional section that depends on Q1=Yes
    section = QuestionnaireSection.objects.create(
        questionnaire=questionnaire, name="Details Section", order=1, depends_on_option=q1_yes
    )

    # Q2: Mandatory question inside the conditional section
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Please provide details",
        order=2,
        is_mandatory=True,
    )
    MultipleChoiceOption.objects.create(question=q2, option="Detail A", is_correct=True, order=1)

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=Yes, so the section and Q2 SHOULD be required
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_yes.id]),
        ],
    )

    # Should raise MissingMandatoryAnswerError because Q2 is in an applicable section but not answered
    with pytest.raises(MissingMandatoryAnswerError):
        service.submit(user, submission_schema)

    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_conditional_question_inside_conditional_section(user: RevelUser, questionnaire: Questionnaire) -> None:
    """Test that a conditional question inside a conditional section is properly handled."""
    # Q1: "Do you want details?" with Yes/No options
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you want details?", order=1, is_mandatory=True
    )
    q1_yes = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)

    # Conditional section that depends on Q1=Yes
    section = QuestionnaireSection.objects.create(
        questionnaire=questionnaire, name="Details Section", order=1, depends_on_option=q1_yes
    )

    # Q2: Question inside the conditional section
    q2 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Please choose a type",
        order=2,
        is_mandatory=True,
    )
    q2_a = MultipleChoiceOption.objects.create(question=q2, option="Type A", is_correct=True, order=1)
    q2_b = MultipleChoiceOption.objects.create(question=q2, option="Type B", is_correct=False, order=2)

    # Q3: Conditional question inside the section that depends on Q2=A
    q3 = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Describe Type A",
        order=3,
        is_mandatory=True,
        depends_on_option=q2_a,
    )
    q3_opt = MultipleChoiceOption.objects.create(question=q3, option="A detail", is_correct=True, order=1)

    service = QuestionnaireService(questionnaire.id)

    # User answers Q1=Yes and Q2=B, so Q3 should NOT be required (Q2_a not selected)
    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_yes.id]),
            MultipleChoiceSubmissionSchema(question_id=q2.id, options_id=[q2_b.id]),
        ],
    )

    # Should NOT raise because Q3's depends_on_option (q2_a) was not selected
    submission = service.submit(user, submission_schema)
    assert submission.pk is not None

    # Clean up for second test
    submission.delete()

    # Now test: User answers Q1=Yes and Q2=A, so Q3 SHOULD be required
    submission_schema_2 = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_yes.id]),
            MultipleChoiceSubmissionSchema(question_id=q2.id, options_id=[q2_a.id]),
        ],
    )

    # Should raise because Q3 is applicable but not answered
    with pytest.raises(MissingMandatoryAnswerError):
        service.submit(user, submission_schema_2)

    # Now provide answer for Q3
    submission_schema_3 = QuestionnaireSubmissionSchema(
        questionnaire_id=questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=q1.id, options_id=[q1_yes.id]),
            MultipleChoiceSubmissionSchema(question_id=q2.id, options_id=[q2_a.id]),
            MultipleChoiceSubmissionSchema(question_id=q3.id, options_id=[q3_opt.id]),
        ],
    )

    # Should succeed
    submission = service.submit(user, submission_schema_3)
    assert submission.pk is not None
