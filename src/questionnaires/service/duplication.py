"""Questionnaire content duplication service.

This module is intentionally free of any imports from the ``events`` app so that
it can safely be reused by poll duplication (#465) without creating circular
imports.  The events-app wrapper (``OrganizationQuestionnaire``) duplication
lives in ``events.service.event_questionnaire_service``.
"""

import typing as t

from django.db import models, transaction

from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)

# Fields that are NOT copied from the template Questionnaire.
# Everything else in _meta.concrete_fields is copied as-is so that fields
# added in the future propagate without touching this file.
_QUESTIONNAIRE_EXCLUDED: frozenset[str] = frozenset(
    {
        # primary key / timestamps — always auto-managed
        "id",
        "created_at",
        "updated_at",
        # explicit overrides: caller may supply new_name; status always DRAFT
        "name",
        "status",
    }
)

# Fields that are NOT copied from a QuestionnaireSection.
_SECTION_EXCLUDED: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        # FK to the new questionnaire is set explicitly
        "questionnaire",
        "questionnaire_id",
        # depends_on_option is wired in a second pass after MC options exist
        "depends_on_option",
        "depends_on_option_id",
    }
)

# Fields that are NOT copied from any BaseQuestion subclass.
_QUESTION_EXCLUDED: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        # FK to the new questionnaire is set explicitly
        "questionnaire",
        "questionnaire_id",
        # FK remapped from old section to new section
        "section",
        "section_id",
        # depends_on_option is wired in a second pass after MC options exist
        "depends_on_option",
        "depends_on_option_id",
    }
)

# Fields that are NOT copied from a MultipleChoiceOption.
_OPTION_EXCLUDED: frozenset[str] = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        # FK to the new question is set explicitly
        "question",
        "question_id",
    }
)


def collect_concrete_field_values(
    instance: models.Model,
    excluded: t.Container[str],
) -> dict[str, t.Any]:
    """Collect copyable concrete field values from a model instance.

    Iterates ``instance._meta.concrete_fields`` and skips any field whose
    ``field.name`` **or** ``field.attname`` appears in *excluded*.  This
    means callers may list either the logical name (``"section"``) or the DB
    column name (``"section_id"``), whichever is more readable.  FK fields
    are collected via ``field.attname`` (e.g. ``section_id``) to avoid
    triggering extra DB queries.

    New fields added to the model propagate automatically without changes to
    this helper or the exclusion sets — only intentionally excluded fields
    need to be listed.

    Args:
        instance: The Django model instance to collect field values from.
        excluded: Any container of field names / attnames to skip (e.g. a
            ``frozenset``, ``set``, or ``list``).

    Returns:
        A ``dict`` of ``{attname: value}`` ready to splat into
        ``Model.objects.create(**kwargs)``.
    """
    values: dict[str, t.Any] = {}
    for field in instance._meta.concrete_fields:
        if field.name in excluded or field.attname in excluded:
            continue
        values[field.attname] = getattr(instance, field.attname)
    return values


def _duplicate_sections(
    template: Questionnaire,
    new_questionnaire: Questionnaire,
) -> dict[t.Any, QuestionnaireSection]:
    """Create copies of all sections for the new questionnaire.

    Sections are created *without* ``depends_on_option``; that FK is wired in
    the second pass once MC options exist.

    Args:
        template: The source questionnaire.
        new_questionnaire: The newly created destination questionnaire.

    Returns:
        Mapping of old section pk to new ``QuestionnaireSection`` instance.
    """
    section_map: dict[t.Any, QuestionnaireSection] = {}
    for old_section in template.sections.all():
        s_kwargs = collect_concrete_field_values(old_section, _SECTION_EXCLUDED)
        s_kwargs["questionnaire"] = new_questionnaire
        new_section = QuestionnaireSection.objects.create(**s_kwargs)
        section_map[old_section.pk] = new_section
    return section_map


def _duplicate_mc_questions(
    template: Questionnaire,
    new_questionnaire: Questionnaire,
    section_map: dict[t.Any, QuestionnaireSection],
) -> tuple[dict[t.Any, MultipleChoiceQuestion], dict[t.Any, MultipleChoiceOption]]:
    """Create copies of MC questions and their options for the new questionnaire.

    MC questions are created *without* ``depends_on_option``; that FK is wired
    in the second pass.  Options are created immediately since they carry no
    cross-questionnaire dependencies.

    Args:
        template: The source questionnaire.
        new_questionnaire: The newly created destination questionnaire.
        section_map: Mapping of old section pk to new section instance.

    Returns:
        A tuple of:
        - Mapping of old MC question pk to new ``MultipleChoiceQuestion``.
        - Mapping of old option pk to new ``MultipleChoiceOption``.
    """
    mc_question_map: dict[t.Any, MultipleChoiceQuestion] = {}
    option_map: dict[t.Any, MultipleChoiceOption] = {}

    for old_q in template.multiplechoicequestion_questions.prefetch_related("options"):
        q_kwargs = collect_concrete_field_values(old_q, _QUESTION_EXCLUDED)
        q_kwargs["questionnaire"] = new_questionnaire
        if old_q.section_id is not None:
            q_kwargs["section"] = section_map[old_q.section_id]
        new_q = MultipleChoiceQuestion.objects.create(**q_kwargs)
        mc_question_map[old_q.pk] = new_q

        for old_opt in old_q.options.all():
            opt_kwargs = collect_concrete_field_values(old_opt, _OPTION_EXCLUDED)
            opt_kwargs["question"] = new_q
            new_opt = MultipleChoiceOption.objects.create(**opt_kwargs)
            option_map[old_opt.pk] = new_opt

    return mc_question_map, option_map


def _duplicate_ft_questions(
    template: Questionnaire,
    new_questionnaire: Questionnaire,
    section_map: dict[t.Any, QuestionnaireSection],
) -> dict[t.Any, FreeTextQuestion]:
    """Create copies of FreeText questions for the new questionnaire.

    Created *without* ``depends_on_option``; wired in the second pass.

    Args:
        template: The source questionnaire.
        new_questionnaire: The newly created destination questionnaire.
        section_map: Mapping of old section pk to new section instance.

    Returns:
        Mapping of old FreeText question pk to new ``FreeTextQuestion`` instance.
    """
    ft_question_map: dict[t.Any, FreeTextQuestion] = {}
    for old_q in template.freetextquestion_questions.all():
        q_kwargs = collect_concrete_field_values(old_q, _QUESTION_EXCLUDED)
        q_kwargs["questionnaire"] = new_questionnaire
        if old_q.section_id is not None:
            q_kwargs["section"] = section_map[old_q.section_id]
        new_q = FreeTextQuestion.objects.create(**q_kwargs)
        ft_question_map[old_q.pk] = new_q
    return ft_question_map


def _duplicate_fu_questions(
    template: Questionnaire,
    new_questionnaire: Questionnaire,
    section_map: dict[t.Any, QuestionnaireSection],
) -> dict[t.Any, FileUploadQuestion]:
    """Create copies of FileUpload questions for the new questionnaire.

    Created *without* ``depends_on_option``; wired in the second pass.

    Args:
        template: The source questionnaire.
        new_questionnaire: The newly created destination questionnaire.
        section_map: Mapping of old section pk to new section instance.

    Returns:
        Mapping of old FileUpload question pk to new ``FileUploadQuestion`` instance.
    """
    fu_question_map: dict[t.Any, FileUploadQuestion] = {}
    for old_q in template.fileuploadquestion_questions.all():
        q_kwargs = collect_concrete_field_values(old_q, _QUESTION_EXCLUDED)
        q_kwargs["questionnaire"] = new_questionnaire
        if old_q.section_id is not None:
            q_kwargs["section"] = section_map[old_q.section_id]
        new_q = FileUploadQuestion.objects.create(**q_kwargs)
        fu_question_map[old_q.pk] = new_q
    return fu_question_map


def _wire_depends_on_option(
    template: Questionnaire,
    section_map: dict[t.Any, QuestionnaireSection],
    mc_question_map: dict[t.Any, MultipleChoiceQuestion],
    ft_question_map: dict[t.Any, FreeTextQuestion],
    fu_question_map: dict[t.Any, FileUploadQuestion],
    option_map: dict[t.Any, MultipleChoiceOption],
) -> None:
    """Second pass: wire ``depends_on_option`` on sections and questions.

    Now that all MC options exist, we can safely set ``depends_on_option``
    on every section/question that had one in the template, then save with
    ``update_fields`` to trigger ``full_clean()`` (``TimeStampedModel.save()``
    always calls it — including on ``update_fields`` saves).

    Args:
        template: The source questionnaire (read-only; provides old objects).
        section_map: old section pk → new section.
        mc_question_map: old MC question pk → new MC question.
        ft_question_map: old FT question pk → new FT question.
        fu_question_map: old FU question pk → new FU question.
        option_map: old option pk → new option.
    """
    for old_section in template.sections.all():
        if old_section.depends_on_option_id is not None:
            new_section = section_map[old_section.pk]
            new_section.depends_on_option = option_map[old_section.depends_on_option_id]
            new_section.save(update_fields=["depends_on_option", "updated_at"])

    for old_mc_q in template.multiplechoicequestion_questions.all():
        if old_mc_q.depends_on_option_id is not None:
            new_mc_q = mc_question_map[old_mc_q.pk]
            new_mc_q.depends_on_option = option_map[old_mc_q.depends_on_option_id]
            new_mc_q.save(update_fields=["depends_on_option", "updated_at"])

    for old_ft_q in template.freetextquestion_questions.all():
        if old_ft_q.depends_on_option_id is not None:
            new_ft_q = ft_question_map[old_ft_q.pk]
            new_ft_q.depends_on_option = option_map[old_ft_q.depends_on_option_id]
            new_ft_q.save(update_fields=["depends_on_option", "updated_at"])

    for old_fu_q in template.fileuploadquestion_questions.all():
        if old_fu_q.depends_on_option_id is not None:
            new_fu_q = fu_question_map[old_fu_q.pk]
            new_fu_q.depends_on_option = option_map[old_fu_q.depends_on_option_id]
            new_fu_q.save(update_fields=["depends_on_option", "updated_at"])


@transaction.atomic
def duplicate_questionnaire_content(
    template: Questionnaire,
    *,
    new_name: str | None = None,
) -> Questionnaire:
    """Deep-copy a Questionnaire's content (sections, questions, options).

    Creates a brand-new ``Questionnaire`` in DRAFT status, then reproduces every
    section, every question (MC / FreeText / FileUpload), and every MC option.

    Intra-questionnaire references are remapped so they point at the new objects:

    * ``QuestionnaireSection.depends_on_option``
    * ``BaseQuestion.section``
    * ``BaseQuestion.depends_on_option``

    NOT copied: submissions, answers (MC / FreeText / FileUpload), evaluations,
    ``QuestionnaireFile`` uploads.

    Implementation uses six ordered passes to ensure every ``clean()`` call sees
    valid state (``TimeStampedModel.save()`` runs ``full_clean()`` on every save,
    including ``save(update_fields=…)``):

    1. Create new ``Questionnaire`` (DRAFT).
    2. Create all sections *without* ``depends_on_option``; record old→new map.
    3. Create all MC questions (*section* remapped, *without* ``depends_on_option``)
       and their options; record old→new maps.
    4. Create FreeText questions (*section* remapped, *without* ``depends_on_option``).
    5. Create FileUpload questions (*section* remapped, *without* ``depends_on_option``).
    6. Second pass: set ``depends_on_option`` on sections and all questions.

    Args:
        template: The ``Questionnaire`` instance to copy from.
        new_name: Optional override for the copy's name.  If ``None``, the
            template's name is preserved.

    Returns:
        The newly created ``Questionnaire`` in DRAFT status.
    """
    # Pass 1: Create the new Questionnaire
    q_kwargs = collect_concrete_field_values(template, _QUESTIONNAIRE_EXCLUDED)
    q_kwargs["name"] = new_name if new_name is not None else template.name
    q_kwargs["status"] = Questionnaire.QuestionnaireStatus.DRAFT
    new_questionnaire = Questionnaire.objects.create(**q_kwargs)

    # Pass 2: Sections (no depends_on_option yet)
    section_map = _duplicate_sections(template, new_questionnaire)

    # Pass 3: MC questions + options
    mc_question_map, option_map = _duplicate_mc_questions(template, new_questionnaire, section_map)

    # Pass 4: FreeText questions
    ft_question_map = _duplicate_ft_questions(template, new_questionnaire, section_map)

    # Pass 5: FileUpload questions
    fu_question_map = _duplicate_fu_questions(template, new_questionnaire, section_map)

    # Pass 6: Wire depends_on_option (second pass)
    _wire_depends_on_option(template, section_map, mc_question_map, ft_question_map, fu_question_map, option_map)

    return new_questionnaire
