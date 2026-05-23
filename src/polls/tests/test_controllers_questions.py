"""Controller tests for poll-scoped question/section/option CRUD."""

import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone

from events.models.mixins import ResourceVisibility
from events.models.organization import Organization
from polls.models import Poll
from questionnaires.models import (
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def draft_poll(organization: Organization) -> Poll:
    """A DRAFT poll the org owner can manage."""
    q = Questionnaire.objects.create(name="draft-q")
    return Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )


@pytest.fixture
def open_poll(organization: Organization) -> Poll:
    """An OPEN poll — every write to its questionnaire must 423."""
    q = Questionnaire.objects.create(name="open-q")
    return Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )


@pytest.fixture
def other_draft_poll(organization: Organization) -> Poll:
    """A second DRAFT poll under the same org (cross-poll guard tests)."""
    q = Questionnaire.objects.create(name="other-draft-q")
    return Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.DRAFT,
    )


def _mc_question_payload(**overrides: t.Any) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {
        "question": "Pick one",
        "options": [
            {"option": "a", "is_correct": False, "order": 0},
            {"option": "b", "is_correct": False, "order": 1},
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- create


def test_create_section(owner_client: Client, draft_poll: Poll) -> None:
    response = owner_client.post(
        f"/api/polls/{draft_poll.id}/sections",
        data={"name": "intro", "description": "first", "order": 0},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["name"] == "intro"
    assert QuestionnaireSection.objects.filter(questionnaire_id=draft_poll.questionnaire_id, name="intro").exists()


def test_create_mc_question(owner_client: Client, draft_poll: Poll) -> None:
    response = owner_client.post(
        f"/api/polls/{draft_poll.id}/multiple-choice-questions",
        data=_mc_question_payload(),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["question"] == "Pick one"
    assert len(body["options"]) == 2


def test_create_mc_option(owner_client: Client, draft_poll: Poll) -> None:
    # Need an existing MC question first. It must be created BEFORE the poll
    # OR through the API on the DRAFT poll. Use the API path.
    create_resp = owner_client.post(
        f"/api/polls/{draft_poll.id}/multiple-choice-questions",
        data=_mc_question_payload(),
        content_type="application/json",
    )
    assert create_resp.status_code == 200
    question_id = create_resp.json()["id"]

    response = owner_client.post(
        f"/api/polls/{draft_poll.id}/multiple-choice-questions/{question_id}/options",
        data={"option": "c", "is_correct": False, "order": 2},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert MultipleChoiceOption.objects.filter(question_id=question_id, option="c").exists()


def test_create_ft_question(owner_client: Client, draft_poll: Poll) -> None:
    response = owner_client.post(
        f"/api/polls/{draft_poll.id}/free-text-questions",
        data={"question": "Why?"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert FreeTextQuestion.objects.filter(questionnaire_id=draft_poll.questionnaire_id, question="Why?").exists()


def test_create_fu_question(owner_client: Client, draft_poll: Poll) -> None:
    response = owner_client.post(
        f"/api/polls/{draft_poll.id}/file-upload-questions",
        data={"question": "Upload"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert FileUploadQuestion.objects.filter(questionnaire_id=draft_poll.questionnaire_id, question="Upload").exists()


# ---------------------------------------------------------------- update


def test_update_section(owner_client: Client, draft_poll: Poll) -> None:
    section = QuestionnaireSection.objects.create(questionnaire=draft_poll.questionnaire, name="old")
    response = owner_client.put(
        f"/api/polls/{draft_poll.id}/sections/{section.id}",
        data={"name": "new", "description": "now with text", "order": 5},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    section.refresh_from_db()
    assert section.name == "new"
    assert section.order == 5


def test_update_mc_question(owner_client: Client, draft_poll: Poll) -> None:
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="old?")
    response = owner_client.put(
        f"/api/polls/{draft_poll.id}/multiple-choice-questions/{mcq.id}",
        data={"question": "new?"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    mcq.refresh_from_db()
    assert mcq.question == "new?"


def test_update_mc_option(owner_client: Client, draft_poll: Poll) -> None:
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="?")
    opt = MultipleChoiceOption.objects.create(question=mcq, option="old")
    response = owner_client.put(
        f"/api/polls/{draft_poll.id}/multiple-choice-options/{opt.id}",
        data={"option": "new", "is_correct": True, "order": 3},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    opt.refresh_from_db()
    assert opt.option == "new"
    assert opt.is_correct is True


def test_update_ft_question(owner_client: Client, draft_poll: Poll) -> None:
    ft = FreeTextQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="old?")
    response = owner_client.put(
        f"/api/polls/{draft_poll.id}/free-text-questions/{ft.id}",
        data={"question": "new?"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    ft.refresh_from_db()
    assert ft.question == "new?"


def test_update_fu_question(owner_client: Client, draft_poll: Poll) -> None:
    fu = FileUploadQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="old?")
    response = owner_client.put(
        f"/api/polls/{draft_poll.id}/file-upload-questions/{fu.id}",
        data={"question": "new?"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    fu.refresh_from_db()
    assert fu.question == "new?"


# ---------------------------------------------------------------- delete


def test_delete_section(owner_client: Client, draft_poll: Poll) -> None:
    section = QuestionnaireSection.objects.create(questionnaire=draft_poll.questionnaire, name="s")
    response = owner_client.delete(f"/api/polls/{draft_poll.id}/sections/{section.id}", content_type="application/json")
    assert response.status_code == 204
    assert not QuestionnaireSection.objects.filter(pk=section.id).exists()


def test_delete_mc_question(owner_client: Client, draft_poll: Poll) -> None:
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="?")
    response = owner_client.delete(
        f"/api/polls/{draft_poll.id}/multiple-choice-questions/{mcq.id}", content_type="application/json"
    )
    assert response.status_code == 204
    assert not MultipleChoiceQuestion.objects.filter(pk=mcq.id).exists()


def test_delete_mc_option(owner_client: Client, draft_poll: Poll) -> None:
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="?")
    opt = MultipleChoiceOption.objects.create(question=mcq, option="o")
    response = owner_client.delete(
        f"/api/polls/{draft_poll.id}/multiple-choice-options/{opt.id}", content_type="application/json"
    )
    assert response.status_code == 204
    assert not MultipleChoiceOption.objects.filter(pk=opt.id).exists()


def test_delete_ft_question(owner_client: Client, draft_poll: Poll) -> None:
    ft = FreeTextQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="?")
    response = owner_client.delete(
        f"/api/polls/{draft_poll.id}/free-text-questions/{ft.id}", content_type="application/json"
    )
    assert response.status_code == 204
    assert not FreeTextQuestion.objects.filter(pk=ft.id).exists()


def test_delete_fu_question(owner_client: Client, draft_poll: Poll) -> None:
    fu = FileUploadQuestion.objects.create(questionnaire=draft_poll.questionnaire, question="?")
    response = owner_client.delete(
        f"/api/polls/{draft_poll.id}/file-upload-questions/{fu.id}", content_type="application/json"
    )
    assert response.status_code == 204
    assert not FileUploadQuestion.objects.filter(pk=fu.id).exists()


# ---------------------------------------------------------------- lockdown


@pytest.mark.parametrize(
    "method, path_suffix, body",
    [
        ("post", "/sections", {"name": "x"}),
        ("post", "/multiple-choice-questions", {"question": "?", "options": [{"option": "a"}]}),
        ("post", "/free-text-questions", {"question": "?"}),
        ("post", "/file-upload-questions", {"question": "?"}),
    ],
)
def test_writes_blocked_on_open_poll_return_423(
    owner_client: Client,
    open_poll: Poll,
    method: str,
    path_suffix: str,
    body: dict[str, t.Any],
) -> None:
    """Signal lockdown surfaces as HTTP 423 Locked on non-DRAFT polls."""
    url = f"/api/polls/{open_poll.id}{path_suffix}"
    func = getattr(owner_client, method)
    response = func(url, data=body, content_type="application/json")
    assert response.status_code == 423, response.content


def test_delete_section_blocked_on_open_poll_returns_423(owner_client: Client, organization: Organization) -> None:
    """Existing-row DELETE path also surfaces as 423 on non-DRAFT polls."""
    q = Questionnaire.objects.create(name="locked")
    section = QuestionnaireSection.objects.create(questionnaire=q, name="s")
    poll = Poll.objects.create(
        organization=organization,
        questionnaire=q,
        vote_visibility=ResourceVisibility.PUBLIC,
        status=Poll.PollStatus.OPEN,
        opened_at=timezone.now(),
    )
    response = owner_client.delete(f"/api/polls/{poll.id}/sections/{section.id}", content_type="application/json")
    assert response.status_code == 423, response.content


# ---------------------------------------------------------------- permissions


def test_create_section_requires_manage_polls_permission(authenticated_client: Client, draft_poll: Poll) -> None:
    """Non-staff users can't see the poll (visibility filter) → 404."""
    response = authenticated_client.post(
        f"/api/polls/{draft_poll.id}/sections",
        data={"name": "x"},
        content_type="application/json",
    )
    # ``Poll.objects.for_user`` filters DRAFT polls out for unrelated users,
    # so the lookup itself fails before permission evaluation.
    assert response.status_code in (403, 404)


# ---------------------------------------------------------------- cross-poll guard


def test_delete_section_from_other_poll_returns_404(
    owner_client: Client, draft_poll: Poll, other_draft_poll: Poll
) -> None:
    """A section that belongs to another poll's questionnaire must not be deletable."""
    other_section = QuestionnaireSection.objects.create(questionnaire=other_draft_poll.questionnaire, name="other")
    response = owner_client.delete(
        f"/api/polls/{draft_poll.id}/sections/{other_section.id}",
        content_type="application/json",
    )
    assert response.status_code == 404
    # Still exists.
    assert QuestionnaireSection.objects.filter(pk=other_section.id).exists()
