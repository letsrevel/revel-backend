"""Unit tests for questionnaire content duplication service."""

import typing as t

import pytest

from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from questionnaires.service.duplication import duplicate_questionnaire_content

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_full_questionnaire() -> Questionnaire:
    """Build a questionnaire with sections, all question types, and options.

    Returns:
        A published Questionnaire instance with a rich structure.
    """
    q = Questionnaire.objects.create(
        name="Original",
        min_score=50,
        description="A description",
        shuffle_questions=True,
        shuffle_sections=False,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        max_attempts=3,
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    # Top-level MC question (no section)
    mc_top = MultipleChoiceQuestion.objects.create(
        questionnaire=q,
        question="Top-level MC",
        order=1,
        allow_multiple_answers=False,
        shuffle_options=True,
    )
    MultipleChoiceOption.objects.create(question=mc_top, option="Opt A", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc_top, option="Opt B", is_correct=False, order=2)

    # Top-level FreeText question
    FreeTextQuestion.objects.create(
        questionnaire=q,
        question="Top-level FT",
        order=2,
        llm_guidelines="Be precise.",
    )

    # Top-level FileUpload question
    FileUploadQuestion.objects.create(
        questionnaire=q,
        question="Upload something",
        order=3,
        max_files=2,
        max_file_size=1024 * 1024,
        allowed_mime_types=["application/pdf"],
    )

    # A section with its own questions
    section = QuestionnaireSection.objects.create(questionnaire=q, name="Section A", order=1)
    mc_sec = MultipleChoiceQuestion.objects.create(
        questionnaire=q,
        section=section,
        question="Section MC",
        order=1,
    )
    MultipleChoiceOption.objects.create(question=mc_sec, option="Yes", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc_sec, option="No", is_correct=False, order=2)
    FreeTextQuestion.objects.create(questionnaire=q, section=section, question="Section FT", order=2)
    FileUploadQuestion.objects.create(questionnaire=q, section=section, question="Section FU", order=3)
    return q


# ---------------------------------------------------------------------------
# Basic scalar / structural copy
# ---------------------------------------------------------------------------


def test_duplicate_creates_new_questionnaire() -> None:
    """A duplicate produces a distinct Questionnaire row with a new pk."""
    orig = Questionnaire.objects.create(name="Q", min_score=75)
    new_q = duplicate_questionnaire_content(orig, new_name="Q Copy")

    assert new_q.pk != orig.pk
    assert new_q.name == "Q Copy"


def test_duplicate_preserves_name_when_none() -> None:
    """If new_name is None the template name is kept."""
    orig = Questionnaire.objects.create(name="Keep Me")
    new_q = duplicate_questionnaire_content(orig)

    assert new_q.name == "Keep Me"


def test_duplicate_always_draft() -> None:
    """The copy is always created in DRAFT regardless of the template status."""
    orig = Questionnaire.objects.create(
        name="Published",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    assert new_q.status == Questionnaire.QuestionnaireStatus.DRAFT
    # Original unchanged
    orig.refresh_from_db()
    assert orig.status == Questionnaire.QuestionnaireStatus.PUBLISHED


def test_duplicate_copies_scalar_fields() -> None:
    """All non-excluded scalar fields are copied from the template."""
    orig = Questionnaire.objects.create(
        name="Original",
        min_score=60,
        description="Desc",
        shuffle_questions=True,
        shuffle_sections=True,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        max_attempts=5,
    )
    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    assert new_q.min_score == orig.min_score
    assert new_q.description == orig.description
    assert new_q.shuffle_questions == orig.shuffle_questions
    assert new_q.shuffle_sections == orig.shuffle_sections
    assert new_q.evaluation_mode == orig.evaluation_mode
    assert new_q.max_attempts == orig.max_attempts


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def test_duplicate_copies_sections() -> None:
    """Sections are duplicated with correct name/description/order."""
    orig = Questionnaire.objects.create(name="Q")
    QuestionnaireSection.objects.create(questionnaire=orig, name="Sec 1", order=1, description="D1")
    QuestionnaireSection.objects.create(questionnaire=orig, name="Sec 2", order=2)

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_sections = list(new_q.sections.order_by("order"))
    assert len(new_sections) == 2
    assert new_sections[0].name == "Sec 1"
    assert new_sections[0].description == "D1"
    assert new_sections[0].order == 1
    assert new_sections[1].name == "Sec 2"
    # Sections belong to new questionnaire
    for s in new_sections:
        assert s.questionnaire_id == new_q.pk


def test_duplicate_no_sections() -> None:
    """A questionnaire with no sections produces an empty sections set."""
    orig = Questionnaire.objects.create(name="Flat")
    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    assert new_q.sections.count() == 0


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def test_duplicate_copies_mc_questions_and_options() -> None:
    """MC questions and their options are fully copied."""
    orig = Questionnaire.objects.create(name="Q")
    mc = MultipleChoiceQuestion.objects.create(
        questionnaire=orig, question="Q?", order=1, allow_multiple_answers=True, shuffle_options=True
    )
    MultipleChoiceOption.objects.create(question=mc, option="A", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc, option="B", is_correct=False, order=2)

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_mc_qs = list(new_q.multiplechoicequestion_questions.all())
    assert len(new_mc_qs) == 1
    new_mc = new_mc_qs[0]
    assert new_mc.question == "Q?"
    assert new_mc.allow_multiple_answers is True
    assert new_mc.shuffle_options is True
    assert new_mc.pk != mc.pk

    new_opts = list(new_mc.options.order_by("order"))
    assert len(new_opts) == 2
    assert new_opts[0].option == "A"
    assert new_opts[0].is_correct is True
    assert new_opts[1].option == "B"
    # Options carry new PKs
    for opt in new_opts:
        assert opt.question_id == new_mc.pk


def test_duplicate_copies_freetext_questions() -> None:
    """FreeText questions are copied with llm_guidelines."""
    orig = Questionnaire.objects.create(name="Q")
    FreeTextQuestion.objects.create(questionnaire=orig, question="Explain?", order=1, llm_guidelines="Be brief.")

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_fts = list(new_q.freetextquestion_questions.all())
    assert len(new_fts) == 1
    assert new_fts[0].question == "Explain?"
    assert new_fts[0].llm_guidelines == "Be brief."
    assert new_fts[0].questionnaire_id == new_q.pk


def test_duplicate_copies_fileupload_questions() -> None:
    """FileUpload questions are copied with all type-specific fields."""
    orig = Questionnaire.objects.create(name="Q")
    FileUploadQuestion.objects.create(
        questionnaire=orig,
        question="Upload doc",
        order=1,
        max_files=3,
        max_file_size=5 * 1024 * 1024,
        allowed_mime_types=["application/pdf", "image/png"],
    )

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_fus = list(new_q.fileuploadquestion_questions.all())
    assert len(new_fus) == 1
    assert new_fus[0].question == "Upload doc"
    assert new_fus[0].max_files == 3
    assert new_fus[0].max_file_size == 5 * 1024 * 1024
    assert new_fus[0].allowed_mime_types == ["application/pdf", "image/png"]


def test_duplicate_copies_section_assigned_questions() -> None:
    """Questions inside sections are remapped to the new sections."""
    orig = Questionnaire.objects.create(name="Q")
    sec = QuestionnaireSection.objects.create(questionnaire=orig, name="S", order=1)
    MultipleChoiceQuestion.objects.create(questionnaire=orig, section=sec, question="MC?", order=1)
    FreeTextQuestion.objects.create(questionnaire=orig, section=sec, question="FT?", order=2)
    FileUploadQuestion.objects.create(questionnaire=orig, section=sec, question="FU?", order=3)

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_sec = new_q.sections.get()
    assert new_sec.name == "S"
    assert new_q.multiplechoicequestion_questions.filter(section=new_sec).count() == 1
    assert new_q.freetextquestion_questions.filter(section=new_sec).count() == 1
    assert new_q.fileuploadquestion_questions.filter(section=new_sec).count() == 1


# ---------------------------------------------------------------------------
# depends_on_option remapping
# ---------------------------------------------------------------------------


def test_duplicate_remaps_section_depends_on_option() -> None:
    """Section depends_on_option is remapped to the new option."""
    orig = Questionnaire.objects.create(name="Q")
    mc = MultipleChoiceQuestion.objects.create(questionnaire=orig, question="Gate?", order=1)
    opt_a = MultipleChoiceOption.objects.create(question=mc, option="Yes", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc, option="No", is_correct=False, order=2)

    sec = QuestionnaireSection.objects.create(questionnaire=orig, name="Conditional", order=1)
    sec.depends_on_option = opt_a
    sec.save(update_fields=["depends_on_option", "updated_at"])

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_sec = new_q.sections.get(name="Conditional")
    assert new_sec.depends_on_option is not None
    # Must point at a new option, not the old one
    assert new_sec.depends_on_option.pk != opt_a.pk
    # Must belong to the new questionnaire
    assert new_sec.depends_on_option.question.questionnaire_id == new_q.pk
    assert new_sec.depends_on_option.option == "Yes"


def test_duplicate_remaps_question_depends_on_option() -> None:
    """BaseQuestion.depends_on_option is remapped to the new option."""
    orig = Questionnaire.objects.create(name="Q")
    mc_gate = MultipleChoiceQuestion.objects.create(questionnaire=orig, question="Gate?", order=1)
    opt = MultipleChoiceOption.objects.create(question=mc_gate, option="Yes", is_correct=True, order=1)

    ft = FreeTextQuestion.objects.create(questionnaire=orig, question="Follow-up?", order=2)
    ft.depends_on_option = opt
    ft.save(update_fields=["depends_on_option", "updated_at"])

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_ft = new_q.freetextquestion_questions.get(question="Follow-up?")
    assert new_ft.depends_on_option is not None
    assert new_ft.depends_on_option.pk != opt.pk
    assert new_ft.depends_on_option.question.questionnaire_id == new_q.pk


def test_duplicate_remaps_mc_question_depends_on_option() -> None:
    """MC question depends_on_option is correctly remapped."""
    orig = Questionnaire.objects.create(name="Q")
    mc_gate = MultipleChoiceQuestion.objects.create(questionnaire=orig, question="Gate?", order=1)
    opt = MultipleChoiceOption.objects.create(question=mc_gate, option="Opt", is_correct=True, order=1)

    mc_dep = MultipleChoiceQuestion.objects.create(questionnaire=orig, question="Dependent?", order=2)
    mc_dep.depends_on_option = opt
    mc_dep.save(update_fields=["depends_on_option", "updated_at"])

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_mc_dep = new_q.multiplechoicequestion_questions.get(question="Dependent?")
    assert new_mc_dep.depends_on_option is not None
    assert new_mc_dep.depends_on_option.pk != opt.pk
    assert new_mc_dep.depends_on_option.question.questionnaire_id == new_q.pk


def test_duplicate_remaps_fu_question_depends_on_option() -> None:
    """FileUpload question depends_on_option is correctly remapped."""
    orig = Questionnaire.objects.create(name="Q")
    mc_gate = MultipleChoiceQuestion.objects.create(questionnaire=orig, question="Gate?", order=1)
    opt = MultipleChoiceOption.objects.create(question=mc_gate, option="Opt", is_correct=True, order=1)

    fu = FileUploadQuestion.objects.create(questionnaire=orig, question="Upload?", order=2)
    fu.depends_on_option = opt
    fu.save(update_fields=["depends_on_option", "updated_at"])

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    new_fu = new_q.fileuploadquestion_questions.get(question="Upload?")
    assert new_fu.depends_on_option is not None
    assert new_fu.depends_on_option.pk != opt.pk
    assert new_fu.depends_on_option.question.questionnaire_id == new_q.pk


# ---------------------------------------------------------------------------
# NOT copied: submissions, answers, evaluations
# ---------------------------------------------------------------------------


def test_duplicate_does_not_copy_submissions(user: t.Any) -> None:
    """Submissions are not copied to the new questionnaire."""
    orig = Questionnaire.objects.create(name="Q")
    QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=orig,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    assert orig.questionnaire_submissions.count() == 1
    assert new_q.questionnaire_submissions.count() == 0


# ---------------------------------------------------------------------------
# Full rich questionnaire round-trip
# ---------------------------------------------------------------------------


def test_duplicate_full_questionnaire_structure() -> None:
    """End-to-end: a complex questionnaire is duplicated with correct counts."""
    orig = _make_full_questionnaire()

    new_q = duplicate_questionnaire_content(orig, new_name="Copy")

    # Counts must match
    assert new_q.sections.count() == orig.sections.count()
    assert new_q.multiplechoicequestion_questions.count() == orig.multiplechoicequestion_questions.count()
    assert new_q.freetextquestion_questions.count() == orig.freetextquestion_questions.count()
    assert new_q.fileuploadquestion_questions.count() == orig.fileuploadquestion_questions.count()

    # All PKs are distinct from the template
    orig_mc_pks = set(orig.multiplechoicequestion_questions.values_list("pk", flat=True))
    new_mc_pks = set(new_q.multiplechoicequestion_questions.values_list("pk", flat=True))
    assert orig_mc_pks.isdisjoint(new_mc_pks)

    orig_opt_pks = set(MultipleChoiceOption.objects.filter(question__questionnaire=orig).values_list("pk", flat=True))
    new_opt_pks = set(MultipleChoiceOption.objects.filter(question__questionnaire=new_q).values_list("pk", flat=True))
    assert orig_opt_pks.isdisjoint(new_opt_pks)

    # Status always DRAFT regardless of original
    assert new_q.status == Questionnaire.QuestionnaireStatus.DRAFT
