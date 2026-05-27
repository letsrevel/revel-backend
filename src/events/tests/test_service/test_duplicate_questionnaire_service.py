"""Tests for the duplicate_organization_questionnaire service function."""

import pytest

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, OrganizationQuestionnaire
from events.service.event_questionnaire_service import duplicate_organization_questionnaire
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEFAULT_Q_TYPE = OrganizationQuestionnaire.QuestionnaireType.ADMISSION


def _make_org_questionnaire(
    organization: Organization,
    *,
    name: str = "Template",
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType = _DEFAULT_Q_TYPE,
    members_exempt: bool = False,
    per_event: bool = False,
    requires_evaluation: bool = True,
) -> OrganizationQuestionnaire:
    """Create an OrganizationQuestionnaire with an underlying Questionnaire.

    Args:
        organization: The owning organization.
        name: The questionnaire name.
        questionnaire_type: The type of questionnaire.
        members_exempt: Whether members are exempt.
        per_event: Whether the questionnaire is per-event.
        requires_evaluation: Whether evaluation is required.

    Returns:
        A new OrganizationQuestionnaire instance.
    """
    q = Questionnaire.objects.create(
        name=name,
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=q,
        questionnaire_type=questionnaire_type,
        members_exempt=members_exempt,
        per_event=per_event,
        requires_evaluation=requires_evaluation,
    )


# ---------------------------------------------------------------------------
# Wrapper-field copy
# ---------------------------------------------------------------------------


def test_duplicate_org_questionnaire_creates_new_wrapper(organization: Organization) -> None:
    """A new OrganizationQuestionnaire row is created within the same organization."""
    oq = _make_org_questionnaire(organization, name="Q")

    new_oq = duplicate_organization_questionnaire(oq, "Q Copy")

    assert new_oq.pk != oq.pk
    assert new_oq.organization_id == organization.pk
    assert new_oq.questionnaire_id != oq.questionnaire_id


def test_duplicate_org_questionnaire_copies_wrapper_fields(organization: Organization) -> None:
    """All OrganizationQuestionnaire config fields are copied."""
    oq = _make_org_questionnaire(
        organization,
        name="Q",
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        members_exempt=True,
        per_event=True,
        requires_evaluation=False,
    )

    new_oq = duplicate_organization_questionnaire(oq, "Q Copy")

    assert new_oq.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP
    assert new_oq.members_exempt is True
    assert new_oq.per_event is True
    assert new_oq.requires_evaluation is False


def test_duplicate_org_questionnaire_new_questionnaire_is_draft(organization: Organization) -> None:
    """The underlying new questionnaire is always in DRAFT status."""
    oq = _make_org_questionnaire(organization, name="Published Q")
    assert oq.questionnaire.status == Questionnaire.QuestionnaireStatus.PUBLISHED

    new_oq = duplicate_organization_questionnaire(oq, "Draft Copy")

    new_oq.questionnaire.refresh_from_db()
    assert new_oq.questionnaire.status == Questionnaire.QuestionnaireStatus.DRAFT


def test_duplicate_org_questionnaire_new_name_set(organization: Organization) -> None:
    """The new questionnaire uses the supplied new_name."""
    oq = _make_org_questionnaire(organization, name="Original Name")

    new_oq = duplicate_organization_questionnaire(oq, "Brand New Name")

    assert new_oq.questionnaire.name == "Brand New Name"
    # Template name unchanged
    oq.questionnaire.refresh_from_db()
    assert oq.questionnaire.name == "Original Name"


# ---------------------------------------------------------------------------
# copy_associations flag
# ---------------------------------------------------------------------------


def test_duplicate_org_questionnaire_no_associations_by_default(
    organization: Organization, event: Event, event_series: EventSeries
) -> None:
    """With copy_associations=False (default) events/event_series start empty."""
    oq = _make_org_questionnaire(organization)
    oq.events.add(event)
    oq.event_series.add(event_series)

    new_oq = duplicate_organization_questionnaire(oq, "Copy")

    assert new_oq.events.count() == 0
    assert new_oq.event_series.count() == 0
    # Original associations unchanged
    assert oq.events.count() == 1
    assert oq.event_series.count() == 1


def test_duplicate_org_questionnaire_copies_associations_when_requested(
    organization: Organization, event: Event, event_series: EventSeries
) -> None:
    """With copy_associations=True events/event_series are replicated."""
    oq = _make_org_questionnaire(organization)
    oq.events.add(event)
    oq.event_series.add(event_series)

    new_oq = duplicate_organization_questionnaire(oq, "Copy", copy_associations=True)

    assert new_oq.events.count() == 1
    assert new_oq.event_series.count() == 1
    assert event in new_oq.events.all()
    assert event_series in new_oq.event_series.all()


# ---------------------------------------------------------------------------
# Content (sections + questions) is deep-copied
# ---------------------------------------------------------------------------


def test_duplicate_org_questionnaire_copies_sections_and_questions(organization: Organization) -> None:
    """Sections and questions from the template are reproduced in the copy."""
    oq = _make_org_questionnaire(organization, name="Rich Q")
    q = oq.questionnaire
    section = QuestionnaireSection.objects.create(questionnaire=q, name="S", order=1)
    mc = MultipleChoiceQuestion.objects.create(questionnaire=q, section=section, question="Q?", order=1)
    MultipleChoiceOption.objects.create(question=mc, option="A", is_correct=True, order=1)

    new_oq = duplicate_organization_questionnaire(oq, "Rich Copy")

    new_q = new_oq.questionnaire
    assert new_q.sections.count() == 1
    assert new_q.multiplechoicequestion_questions.count() == 1
    new_mc = new_q.multiplechoicequestion_questions.get()
    assert new_mc.options.count() == 1
    # FK to new section, not old
    assert new_mc.section is not None
    assert new_mc.section.questionnaire_id == new_q.pk


# ---------------------------------------------------------------------------
# Submissions are NOT copied
# ---------------------------------------------------------------------------


def test_duplicate_org_questionnaire_no_submissions_copied(organization: Organization, member_user: RevelUser) -> None:
    """Submissions on the template questionnaire are not carried over."""
    oq = _make_org_questionnaire(organization)
    QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=oq.questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )

    new_oq = duplicate_organization_questionnaire(oq, "Copy")

    assert oq.questionnaire.questionnaire_submissions.count() == 1
    assert new_oq.questionnaire.questionnaire_submissions.count() == 0
