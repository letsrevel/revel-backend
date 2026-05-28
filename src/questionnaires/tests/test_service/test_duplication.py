"""Unit tests for questionnaire content duplication service."""

import typing as t

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import RevelUser
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireFile,
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


def test_duplicate_dependency_chain_fully_remapped() -> None:
    """Full dependency chain (MC question → option → section → question) is remapped.

    Builds a template with:
    - A top-level MC question Q1 with option O1.
    - A section S1 whose ``depends_on_option`` is O1.
    - A FreeText question Q2 inside S1 whose ``depends_on_option`` is also O1.

    After duplication every reference in the copy must point at the NEW objects
    (copy of O1, copy of Q1, copy of S1, copy of Q2) — none must point at any
    template object.  This regression test locks in the six-pass ordering
    against future refactors.
    """
    # ---- build template structure ----
    template = Questionnaire.objects.create(
        name="Chain Template",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )

    # Pass 3 creates this: MC question Q1 with option O1
    q1 = MultipleChoiceQuestion.objects.create(
        questionnaire=template,
        question="Q1: Gate question",
        order=1,
    )
    o1 = MultipleChoiceOption.objects.create(question=q1, option="Yes", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=q1, option="No", is_correct=False, order=2)

    # Pass 2 creates this: section S1 conditionally shown on O1
    s1 = QuestionnaireSection.objects.create(questionnaire=template, name="S1: Conditional Section", order=1)
    s1.depends_on_option = o1
    s1.save(update_fields=["depends_on_option", "updated_at"])

    # Pass 4 creates this: FreeText question Q2 in S1, also gated on O1
    q2 = FreeTextQuestion.objects.create(
        questionnaire=template,
        section=s1,
        question="Q2: Follow-up inside S1",
        order=1,
    )
    q2.depends_on_option = o1
    q2.save(update_fields=["depends_on_option", "updated_at"])

    # ---- duplicate ----
    copy = duplicate_questionnaire_content(template, new_name="Chain Copy")

    # ---- locate new objects ----
    new_s1 = copy.sections.get(name="S1: Conditional Section")
    new_q2 = copy.freetextquestion_questions.get(question="Q2: Follow-up inside S1")
    new_q1_qs = copy.multiplechoicequestion_questions.filter(question="Q1: Gate question")
    assert new_q1_qs.count() == 1
    new_q1 = new_q1_qs.get()
    new_o1 = new_q1.options.get(option="Yes")

    # ---- section dependency chain ----
    # S1 on the copy must point at the NEW option, not the template option.
    assert new_s1.depends_on_option is not None, "S1 copy must have depends_on_option set"
    assert new_s1.depends_on_option.pk != o1.pk, "S1 copy must NOT reference the template option"
    assert new_s1.depends_on_option.pk == new_o1.pk, "S1 copy must reference the copy of O1"

    # The option the section depends on must belong to the new questionnaire.
    assert new_s1.depends_on_option.question.questionnaire_id == copy.pk

    # ---- question dependency chain ----
    # Q2 on the copy must point at the NEW option.
    assert new_q2.depends_on_option is not None, "Q2 copy must have depends_on_option set"
    assert new_q2.depends_on_option.pk != o1.pk, "Q2 copy must NOT reference the template option"
    assert new_q2.depends_on_option.pk == new_o1.pk, "Q2 copy must reference the copy of O1"

    # The option Q2 depends on must belong to the new questionnaire.
    assert new_q2.depends_on_option.question.questionnaire_id == copy.pk

    # ---- section membership ----
    assert new_s1.questionnaire_id == copy.pk, "New section must belong to the copy"
    assert new_q2.section_id == new_s1.pk, "Q2 copy must be in the new section, not the template section"

    # ---- no template objects referenced from the copy ----
    template_pks = {template.pk, q1.pk, o1.pk, s1.pk, q2.pk}
    assert new_s1.pk not in template_pks
    assert new_q1.pk not in template_pks
    assert new_o1.pk not in template_pks
    assert new_q2.pk not in template_pks


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


def test_duplicate_does_not_copy_answers_evaluations_or_files(user: RevelUser) -> None:
    """Answers, evaluations, and QuestionnaireFile uploads are NOT copied.

    Builds a template questionnaire with one MC, one FreeText, and one
    FileUpload question, then creates a submission with answers of each type,
    an evaluation, and a QuestionnaireFile.  After duplication the new
    questionnaire must have no submissions, no answers of any type, no
    evaluations, and the QuestionnaireFile must not be referenced by any
    question on the new questionnaire.  The template must remain intact
    (duplication is non-destructive).
    """
    # ---- build template structure ----
    template = Questionnaire.objects.create(
        name="Template",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    mc_q = MultipleChoiceQuestion.objects.create(
        questionnaire=template,
        question="MC question?",
        order=1,
        allow_multiple_answers=False,
    )
    opt_correct = MultipleChoiceOption.objects.create(question=mc_q, option="Correct", is_correct=True, order=1)
    MultipleChoiceOption.objects.create(question=mc_q, option="Wrong", is_correct=False, order=2)
    ft_q = FreeTextQuestion.objects.create(
        questionnaire=template,
        question="FT question?",
        order=2,
    )
    fu_q = FileUploadQuestion.objects.create(
        questionnaire=template,
        question="Upload something",
        order=3,
        max_files=1,
        allowed_mime_types=["application/pdf"],
    )

    # ---- create user data on the template ----
    submission = QuestionnaireSubmission.objects.create(
        user=user,
        questionnaire=template,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )
    mc_answer = MultipleChoiceAnswer.objects.create(
        submission=submission,
        question=mc_q,
        option=opt_correct,
    )
    ft_answer = FreeTextAnswer.objects.create(
        submission=submission,
        question=ft_q,
        answer="Some text answer",
    )
    uploaded_file = SimpleUploadedFile(
        name="doc.pdf",
        content=b"pdf content",
        content_type="application/pdf",
    )
    qfile = QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="doc.pdf",
        file_hash="duplication_test_hash_001",
        mime_type="application/pdf",
        file_size=len(b"pdf content"),
    )
    fu_answer = FileUploadAnswer.objects.create(
        submission=submission,
        question=fu_q,
    )
    fu_answer.files.add(qfile)

    evaluation = QuestionnaireEvaluation.objects.create(
        submission=submission,
        status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        score=90,
    )

    # ---- duplicate ----
    new_q = duplicate_questionnaire_content(template, new_name="Copy")

    # ---- new questionnaire has no user data ----
    assert new_q.questionnaire_submissions.count() == 0, "No submissions should be copied"

    new_mc_pks = list(new_q.multiplechoicequestion_questions.values_list("pk", flat=True))
    new_ft_pks = list(new_q.freetextquestion_questions.values_list("pk", flat=True))
    new_fu_pks = list(new_q.fileuploadquestion_questions.values_list("pk", flat=True))

    assert MultipleChoiceAnswer.objects.filter(question__in=new_mc_pks).count() == 0, "No MC answers should be copied"
    assert FreeTextAnswer.objects.filter(question__in=new_ft_pks).count() == 0, "No FreeText answers should be copied"
    assert FileUploadAnswer.objects.filter(question__in=new_fu_pks).count() == 0, (
        "No FileUpload answers should be copied"
    )
    assert QuestionnaireEvaluation.objects.filter(submission__questionnaire=new_q).count() == 0, (
        "No evaluations should be copied"
    )

    # QuestionnaireFile objects are user-owned and not linked to questionnaire
    # questions directly — they are attached via FileUploadAnswer.files (M2M).
    # We verify the file is not referenced by any answer on the new questionnaire.
    assert not qfile.file_upload_answers.filter(question__in=new_fu_pks).exists(), (
        "QuestionnaireFile should not be referenced by any answer on the new questionnaire"
    )

    # ---- template is unchanged (duplication is non-destructive) ----
    assert template.questionnaire_submissions.count() == 1, "Template submission must survive"
    assert MultipleChoiceAnswer.objects.filter(pk=mc_answer.pk).exists(), "Template MC answer must survive"
    assert FreeTextAnswer.objects.filter(pk=ft_answer.pk).exists(), "Template FreeText answer must survive"
    assert FileUploadAnswer.objects.filter(pk=fu_answer.pk).exists(), "Template FileUpload answer must survive"
    assert QuestionnaireEvaluation.objects.filter(pk=evaluation.pk).exists(), "Template evaluation must survive"
    assert QuestionnaireFile.objects.filter(pk=qfile.pk).exists(), "QuestionnaireFile must survive"


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
