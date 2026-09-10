"""Conditional-question applicability, shared by the evaluator and the submit path.

A questionnaire may hide sections and questions behind a multiple-choice option
(``depends_on_option``). Two places need to know exactly which questions were
actually shown: the submit path (to decide which mandatory questions must be
answered) and the evaluator (to decide which questions to score). They differ
only in where the selected options come from — the inbound payload versus the
persisted answers — so the predicate itself lives here.

This module is pure: no queries. Callers pass prefetched iterables.
"""

from __future__ import annotations

import dataclasses
import typing as t
from uuid import UUID

if t.TYPE_CHECKING:
    from collections.abc import Iterable

    from questionnaires.models import BaseQuestion, QuestionnaireSection


@dataclasses.dataclass(frozen=True, slots=True)
class ApplicableIds:
    """The ids of the sections and questions that apply to a given set of answers."""

    sections: set[UUID]
    multiple_choice: set[UUID]
    free_text: set[UUID]
    file_upload: set[UUID]


def compute_applicable_ids(
    *,
    sections: Iterable[QuestionnaireSection],
    mc_questions: Iterable[BaseQuestion],
    ft_questions: Iterable[BaseQuestion],
    fu_questions: Iterable[BaseQuestion],
    selected_option_ids: set[UUID],
) -> ApplicableIds:
    """Compute which sections and questions are applicable given the selected options.

    A section is applicable if it has no ``depends_on_option``, or that option
    was selected. A question is applicable if its section (when it has one) is
    applicable **and** it has no ``depends_on_option`` of its own, or that
    option was selected.

    Args:
        sections: The questionnaire's sections.
        mc_questions: All multiple-choice questions (top level and sectioned).
        ft_questions: All free-text questions.
        fu_questions: All file-upload questions.
        selected_option_ids: The option ids selected in the submission.

    Returns:
        The applicable section and per-type question ids.
    """
    applicable_sections = {
        section.id
        for section in sections
        if section.depends_on_option_id is None or section.depends_on_option_id in selected_option_ids
    }

    def _is_applicable(question: BaseQuestion) -> bool:
        if question.section_id is not None and question.section_id not in applicable_sections:
            return False
        return question.depends_on_option_id is None or question.depends_on_option_id in selected_option_ids

    return ApplicableIds(
        sections=applicable_sections,
        multiple_choice={q.id for q in mc_questions if _is_applicable(q)},
        free_text={q.id for q in ft_questions if _is_applicable(q)},
        file_upload={q.id for q in fu_questions if _is_applicable(q)},
    )
