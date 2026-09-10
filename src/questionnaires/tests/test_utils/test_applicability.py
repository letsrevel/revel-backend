"""Unit tests for the shared conditional-applicability helper.

The helper is pure: every model instance below is unsaved, so these tests need
no database.
"""

import uuid

from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceQuestion,
    QuestionnaireSection,
)
from questionnaires.utils.applicability import ApplicableIds, compute_applicable_ids

# Option ids the questions/sections depend on.
OPT_SHOW_SECTION = uuid.uuid4()
OPT_SHOW_QUESTION = uuid.uuid4()
OPT_UNSELECTED = uuid.uuid4()


def _fixture() -> tuple[
    list[QuestionnaireSection], list[MultipleChoiceQuestion], list[FreeTextQuestion], list[FileUploadQuestion]
]:
    """Build one unconditional and one conditional section, with questions in both."""
    plain_section = QuestionnaireSection(name="plain")
    conditional_section = QuestionnaireSection(name="conditional", depends_on_option_id=OPT_SHOW_SECTION)

    mc_top = MultipleChoiceQuestion(question="top level")
    mc_nested = MultipleChoiceQuestion(question="inside conditional section", section_id=conditional_section.id)
    ft_plain = FreeTextQuestion(question="inside plain section", section_id=plain_section.id)
    ft_conditional = FreeTextQuestion(
        question="conditional inside plain section",
        section_id=plain_section.id,
        depends_on_option_id=OPT_SHOW_QUESTION,
    )
    fu_nested_conditional = FileUploadQuestion(
        question="conditional inside conditional section",
        section_id=conditional_section.id,
        depends_on_option_id=OPT_SHOW_QUESTION,
    )

    return (
        [plain_section, conditional_section],
        [mc_top, mc_nested],
        [ft_plain, ft_conditional],
        [fu_nested_conditional],
    )


def test_nothing_selected_hides_every_conditional() -> None:
    """With no options selected, only unconditional sections/questions apply."""
    sections, mcqs, ftqs, fuqs = _fixture()

    result = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids=set(),
    )

    assert result == ApplicableIds(
        sections={sections[0].id},
        multiple_choice={mcqs[0].id},
        free_text={ftqs[0].id},
        file_upload=set(),
    )


def test_section_trigger_reveals_its_unconditional_questions() -> None:
    """Selecting the section's trigger reveals the section and its plain questions."""
    sections, mcqs, ftqs, fuqs = _fixture()

    result = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids={OPT_SHOW_SECTION},
    )

    assert result.sections == {sections[0].id, sections[1].id}
    assert result.multiple_choice == {mcqs[0].id, mcqs[1].id}
    # The nested file-upload question still needs its own trigger.
    assert result.file_upload == set()


def test_question_trigger_alone_does_not_reveal_a_hidden_section() -> None:
    """A question's own trigger cannot override its section being inapplicable."""
    sections, mcqs, ftqs, fuqs = _fixture()

    result = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids={OPT_SHOW_QUESTION},
    )

    assert result.sections == {sections[0].id}
    # ft_conditional lives in the always-applicable section, so its trigger is enough.
    assert result.free_text == {ftqs[0].id, ftqs[1].id}
    # fu_nested_conditional's section is still hidden.
    assert result.file_upload == set()


def test_both_triggers_reveal_the_nested_conditional_question() -> None:
    """Section trigger + question trigger together reveal the nested question."""
    sections, mcqs, ftqs, fuqs = _fixture()

    result = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids={OPT_SHOW_SECTION, OPT_SHOW_QUESTION},
    )

    assert result == ApplicableIds(
        sections={sections[0].id, sections[1].id},
        multiple_choice={mcqs[0].id, mcqs[1].id},
        free_text={ftqs[0].id, ftqs[1].id},
        file_upload={fuqs[0].id},
    )


def test_unrelated_selection_changes_nothing() -> None:
    """Selecting an option nothing depends on leaves the applicable set untouched."""
    sections, mcqs, ftqs, fuqs = _fixture()

    baseline = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids=set(),
    )
    result = compute_applicable_ids(
        sections=sections,
        mc_questions=mcqs,
        ft_questions=ftqs,
        fu_questions=fuqs,
        selected_option_ids={OPT_UNSELECTED},
    )

    assert result == baseline


def test_empty_questionnaire_yields_empty_sets() -> None:
    """No sections and no questions produce four empty sets."""
    result = compute_applicable_ids(
        sections=[],
        mc_questions=[],
        ft_questions=[],
        fu_questions=[],
        selected_option_ids=set(),
    )

    assert result == ApplicableIds(sections=set(), multiple_choice=set(), free_text=set(), file_upload=set())
