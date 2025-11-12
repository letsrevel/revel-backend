"""test_service.py: Unit tests for the QuestionnaireService."""

import uuid
from decimal import Decimal

import pytest

from accounts.models import RevelUser
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    FreeTextQuestionUpdateSchema,
    FreeTextSubmissionSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceOptionUpdateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
    MultipleChoiceSubmissionSchema,
    QuestionnaireCreateSchema,
    QuestionnaireSubmissionSchema,
    SectionCreateSchema,
    SectionUpdateSchema,
)
from questionnaires.service import QuestionnaireService, get_questionnaire_schema

pytestmark = pytest.mark.django_db


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


# --- Tests for build() ---


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


# --- Tests for submit() ---


def test_submit_success_final(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test a successful, final submission of a questionnaire."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)

    # Get question and option IDs for all mandatory questions
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True, is_mandatory=True)
    mcq_s1 = q.multiplechoicequestion_questions.get(section__name="Section 1", is_mandatory=True)
    ftq_s2 = q.freetextquestion_questions.get(section__name="Section 2", is_mandatory=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)
    mcq_s1_opt = mcq_s1.options.get(is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
            MultipleChoiceSubmissionSchema(question_id=mcq_s1.id, options_id=[mcq_s1_opt.id]),
        ],
        free_text_answers=[FreeTextSubmissionSchema(question_id=ftq_s2.id, answer="This is a mandatory answer.")],
    )

    submission = service.submit(user, submission_schema)

    assert submission.pk is not None
    assert submission.user == user
    assert submission.questionnaire == q
    assert submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    assert submission.submitted_at is not None
    assert submission.multiplechoiceanswer_answers.count() == 2
    assert submission.freetextanswer_answers.count() == 1


def test_submit_draft_and_update(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test creating a draft and then updating it by adding more answers."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)

    # First submission as draft
    draft_schema_1 = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
        ],
    )
    submission1 = service.submit(user, draft_schema_1)

    assert submission1.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT
    assert QuestionnaireSubmission.objects.count() == 1
    assert submission1.multiplechoiceanswer_answers.count() == 1

    # Second submission as draft. The current implementation APPENDS answers.
    ftq_s2 = q.freetextquestion_questions.get(section__name="Section 2")
    draft_schema_2 = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
        free_text_answers=[FreeTextSubmissionSchema(question_id=ftq_s2.id, answer="An appended answer.")],
    )
    submission2 = service.submit(user, draft_schema_2)

    assert submission2.id == submission1.id
    assert QuestionnaireSubmission.objects.count() == 1
    # Check that answers were replaced.
    assert submission2.multiplechoiceanswer_answers.count() == 0
    assert submission2.freetextanswer_answers.count() == 1


def test_submit_raises_missing_mandatory_error(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test that submitting without all mandatory answers raises a MissingMandatoryAnswerError."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)

    # Only answer one of the three mandatory questions
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True, is_mandatory=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
        ],
    )

    with pytest.raises(MissingMandatoryAnswerError):
        service.submit(user, submission_schema)

    # Verify transactionality: no submission or answers should have been created.
    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_raises_cross_questionnaire_error(
    user: RevelUser, complex_questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test that submitting an answer for a different questionnaire raises an error."""
    service = QuestionnaireService(complex_questionnaire.id)

    # Create a question in the *other* questionnaire
    other_mcq = MultipleChoiceQuestion.objects.create(questionnaire=another_questionnaire, question="Wrong Q")
    other_opt = MultipleChoiceOption.objects.create(question=other_mcq, option="Wrong Opt", is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=complex_questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=other_mcq.id, options_id=[other_opt.id]),
        ],
    )

    with pytest.raises(CrossQuestionnaireSubmissionError):
        service.submit(user, submission_schema)

    assert QuestionnaireSubmission.objects.count() == 0


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
    assert questionnaire.multiplechoicequestion_questions.filter(section__isnull=True).count() == 1  # NEW
    top_mcq = questionnaire.multiplechoicequestion_questions.filter(section__isnull=True).first()  # NEW
    assert top_mcq is not None
    # ...
    assert questionnaire.freetextquestion_questions.filter(section__isnull=True).count() == 1  # NEW
    top_ftq = questionnaire.freetextquestion_questions.filter(section__isnull=True).first()  # NEW
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


def test_get_questionnaire_schema(complex_questionnaire: Questionnaire) -> None:
    """Test that the questionnaire schema can be retrieved."""
    complex_questionnaire.evaluation_mode = "manual"
    schema = get_questionnaire_schema(complex_questionnaire)
    assert schema.name == complex_questionnaire.name
    assert len(schema.multiplechoicequestion_questions) == 2
    assert len(schema.freetextquestion_questions) == 2
    assert len(schema.sections) == 2


@pytest.fixture
def evaluator() -> RevelUser:
    return RevelUser.objects.create(username="evaluator", password="password")


def test_get_submissions_queryset(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that submissions queryset is retrieved correctly."""
    # Create some submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    submission2 = QuestionnaireSubmission.objects.create(
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    # Create submission for different questionnaire to ensure filtering works
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    QuestionnaireSubmission.objects.create(
        user=user, questionnaire=other_questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    service = QuestionnaireService(questionnaire.id)
    submissions = service.get_submissions_queryset()

    # Should only return submissions for this questionnaire
    submission_ids = list(submissions.values_list("id", flat=True))
    assert len(submission_ids) == 2
    assert submission1.id in submission_ids
    assert submission2.id in submission_ids


def test_get_submission_detail(
    questionnaire: Questionnaire,
    user: RevelUser,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    free_text_question: FreeTextQuestion,
) -> None:
    """Test that submission detail is retrieved with answers."""
    from questionnaires.models import FreeTextAnswer, MultipleChoiceAnswer

    # Create submission with answers
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    # Add answers
    mc_answer = MultipleChoiceAnswer.objects.create(
        submission=submission, question=single_answer_mc_question, option=correct_option
    )
    ft_answer = FreeTextAnswer.objects.create(
        submission=submission, question=free_text_question, answer="This is my answer"
    )

    service = QuestionnaireService(questionnaire.id)
    retrieved_submission = service.get_submission_detail(submission.id)

    assert retrieved_submission.id == submission.id
    assert retrieved_submission.user == user
    assert retrieved_submission.questionnaire == questionnaire

    # Check that related data is prefetched
    mc_answers = list(retrieved_submission.multiplechoiceanswer_answers.all())
    ft_answers = list(retrieved_submission.freetextanswer_answers.all())

    assert len(mc_answers) == 1
    assert len(ft_answers) == 1
    assert mc_answers[0].id == mc_answer.id
    assert ft_answers[0].id == ft_answer.id


def test_get_submission_detail_wrong_questionnaire(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that get_submission_detail raises error for wrong questionnaire."""

    # Create submission for different questionnaire
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=other_questionnaire)

    service = QuestionnaireService(questionnaire.id)

    with pytest.raises(QuestionnaireSubmission.DoesNotExist):
        service.get_submission_detail(submission.id)


def test_evaluate_submission_create_new(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser, org_questionnaire: object
) -> None:
    """Test creating a new evaluation for a submission."""
    from decimal import Decimal

    from questionnaires.models import QuestionnaireEvaluation
    from questionnaires.schema import EvaluationCreateSchema

    # Create submission
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    # Create evaluator
    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=Decimal("85.50"),
        comments="Good submission",
    )

    evaluation = service.evaluate_submission(submission.id, payload, evaluator)

    assert evaluation.submission == submission
    assert evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    assert evaluation.score == Decimal("85.50")
    assert evaluation.comments == "Good submission"
    assert evaluation.evaluator == evaluator
    assert evaluation.automatically_evaluated is False


def test_evaluate_submission_update_existing(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser, org_questionnaire: object
) -> None:
    """Test updating an existing evaluation for a submission."""
    from decimal import Decimal

    from questionnaires.models import QuestionnaireEvaluation
    from questionnaires.schema import EvaluationCreateSchema

    # Create submission and initial evaluation
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    initial_evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        score=Decimal("70.00"),
        comments="Initial evaluation",
        evaluator=evaluator,
    )

    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        score=Decimal("60.00"),
        comments="Updated: needs improvement",
    )

    updated_evaluation = service.evaluate_submission(submission.id, payload, evaluator)

    # Should be the same evaluation object, just updated
    assert updated_evaluation.id == initial_evaluation.id
    assert updated_evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    assert updated_evaluation.score == Decimal("60.00")
    assert updated_evaluation.comments == "Updated: needs improvement"
    assert updated_evaluation.evaluator == evaluator
    assert updated_evaluation.automatically_evaluated is False


def test_evaluate_submission_wrong_questionnaire(
    questionnaire: Questionnaire, user: RevelUser, evaluator: RevelUser
) -> None:
    """Test that evaluate_submission raises error for wrong questionnaire."""
    from questionnaires.models import QuestionnaireEvaluation
    from questionnaires.schema import EvaluationCreateSchema

    # Create submission for different questionnaire
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=other_questionnaire)

    service = QuestionnaireService(questionnaire.id)
    payload = EvaluationCreateSchema(
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED, score=None, comments="Should fail"
    )

    with pytest.raises(QuestionnaireSubmission.DoesNotExist):
        service.evaluate_submission(submission.id, payload, evaluator)
