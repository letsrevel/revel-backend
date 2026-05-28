"""Integration tests for POST /polls/{poll_id}/duplicate."""

import typing as t

import pytest
from django.test.client import Client

from accounts.models import RevelUser
from events.models.mixins import ResourceVisibility
from events.models.organization import Organization, OrganizationStaff, PermissionMap, PermissionsSchema
from polls.models import Poll
from polls.schema import PollCreateSchema
from polls.service import poll_service
from questionnaires.models import MultipleChoiceOption, MultipleChoiceQuestion, Questionnaire

pytestmark = pytest.mark.django_db


def _create_payload(organization: Organization, **overrides: t.Any) -> PollCreateSchema:
    base: dict[str, t.Any] = {
        "name": "Template",
        "vote_visibility": ResourceVisibility.PUBLIC,
        "result_visibility": ResourceVisibility.PUBLIC,
        "result_timing": Poll.PollResultTiming.AFTER_VOTE,
        "staff_anonymous": True,
        "public_anonymous": True,
    }
    base.update(overrides)
    return PollCreateSchema(**base)


def _duplicate_url(poll: Poll) -> str:
    return f"/api/polls/{poll.id}/duplicate"


@pytest.fixture
def template_poll(organization: Organization) -> Poll:
    """A simple DRAFT poll to use as the duplication template."""
    return poll_service.create_poll(organization, _create_payload(organization, name="Source Poll"))


@pytest.fixture
def staff_no_polls_client(organization: Organization, revel_user_factory: t.Any) -> Client:
    """Authenticated client for a staff member WITHOUT manage_polls permission."""
    from ninja_jwt.tokens import RefreshToken

    staff_user: RevelUser = revel_user_factory()
    permissions = PermissionsSchema(default=PermissionMap(manage_polls=False))
    OrganizationStaff.objects.create(
        organization=organization,
        user=staff_user,
        permissions=permissions.model_dump(mode="json"),
    )
    refresh = RefreshToken.for_user(staff_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# --- Permission checks ---


def test_duplicate_requires_authentication(anonymous_client: Client, template_poll: Poll) -> None:
    response = anonymous_client.post(
        _duplicate_url(template_poll),
        data={"name": "Copy"},
        content_type="application/json",
    )
    assert response.status_code == 401


def test_duplicate_requires_manage_polls_permission(staff_no_polls_client: Client, template_poll: Poll) -> None:
    response = staff_no_polls_client.post(
        _duplicate_url(template_poll),
        data={"name": "Copy"},
        content_type="application/json",
    )
    assert response.status_code == 403


def test_duplicate_non_member_gets_forbidden(authenticated_client: Client, template_poll: Poll) -> None:
    """A random authenticated user who is not in the org cannot duplicate."""
    response = authenticated_client.post(
        _duplicate_url(template_poll),
        data={"name": "Copy"},
        content_type="application/json",
    )
    assert response.status_code in {403, 404}


def test_duplicate_unknown_poll_returns_404(owner_client: Client) -> None:
    import uuid

    response = owner_client.post(
        f"/api/polls/{uuid.uuid4()}/duplicate",
        data={"name": "Copy"},
        content_type="application/json",
    )
    assert response.status_code == 404


# --- Success cases ---


def test_duplicate_owner_succeeds_201(owner_client: Client, template_poll: Poll) -> None:
    response = owner_client.post(
        _duplicate_url(template_poll),
        data={"name": "My Copy"},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Poll.PollStatus.DRAFT.value
    # A new poll was created
    assert body["id"] != str(template_poll.id)


def test_duplicate_staff_with_manage_polls_succeeds(staff_client: Client, template_poll: Poll) -> None:
    response = staff_client.post(
        _duplicate_url(template_poll),
        data={"name": "Staff Copy"},
        content_type="application/json",
    )
    assert response.status_code == 201


def test_duplicate_response_has_new_questionnaire(owner_client: Client, template_poll: Poll) -> None:
    """The response questionnaire_id is different from the template's."""
    response = owner_client.post(
        _duplicate_url(template_poll),
        data={"name": "New Name"},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["questionnaire_id"] != str(template_poll.questionnaire_id)
    # And the new questionnaire exists
    assert Questionnaire.objects.filter(pk=body["questionnaire_id"]).exists()


def test_duplicate_response_lifecycle_is_reset(owner_client: Client, organization: Organization) -> None:
    """Duplicating an OPEN poll returns a DRAFT with no lifecycle timestamps."""
    poll = poll_service.create_poll(organization, _create_payload(organization))
    poll_service.open_poll(poll)

    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Copy of Open"},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == Poll.PollStatus.DRAFT.value
    assert body["opened_at"] is None
    assert body["closed_at"] is None
    assert body["closes_at"] is None


def test_duplicate_questions_are_copied(owner_client: Client, organization: Organization) -> None:
    """Questions in the template questionnaire appear in the duplicate."""
    poll = poll_service.create_poll(organization, _create_payload(organization))
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=poll.questionnaire, question="Best language?")
    MultipleChoiceOption.objects.create(question=mcq, option="Python")
    MultipleChoiceOption.objects.create(question=mcq, option="Rust")

    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Poll With Questions Copy"},
        content_type="application/json",
    )
    assert response.status_code == 201
    new_poll = Poll.objects.get(pk=response.json()["id"])
    new_mc_questions = list(new_poll.questionnaire.multiplechoicequestion_questions.prefetch_related("options"))
    assert len(new_mc_questions) == 1
    assert new_mc_questions[0].pk != mcq.pk
    option_texts = sorted(opt.option for opt in new_mc_questions[0].options.all())
    assert option_texts == ["Python", "Rust"]


def test_duplicate_anonymity_override_applied(owner_client: Client, organization: Organization) -> None:
    """staff_anonymous/public_anonymous overrides in the payload are applied."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(organization, staff_anonymous=True, public_anonymous=True),
    )
    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Override Copy", "staff_anonymous": False},
        content_type="application/json",
    )
    assert response.status_code == 201
    new_poll = Poll.objects.get(pk=response.json()["id"])
    assert new_poll.staff_anonymous is False
    assert new_poll.public_anonymous is True  # not overridden → copied


def test_duplicate_anonymity_copied_when_omitted(owner_client: Client, organization: Organization) -> None:
    """When override fields are omitted the template's values are used."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(organization, staff_anonymous=False, public_anonymous=True),
    )
    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Verbatim Copy"},
        content_type="application/json",
    )
    assert response.status_code == 201
    new_poll = Poll.objects.get(pk=response.json()["id"])
    assert new_poll.staff_anonymous is False
    assert new_poll.public_anonymous is True


# --- Constraint-preserving copy of restricted-visibility poll ---


def test_duplicate_attendees_only_poll_preserves_event(
    owner_client: Client, organization: Organization, event: t.Any
) -> None:
    """Duplicating a poll with ATTENDEES_ONLY visibility copies the event FK."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            vote_visibility=ResourceVisibility.ATTENDEES_ONLY,
            event_id=event.id,
        ),
    )
    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Restricted Copy"},
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["event_id"] == str(event.id)
    assert body["vote_visibility"] == ResourceVisibility.ATTENDEES_ONLY.value


# --- Constraint-violation: public_anonymous override conflicts with result_visibility ---


def test_duplicate_public_anonymous_false_with_public_result_visibility_returns_422(
    owner_client: Client, organization: Organization
) -> None:
    """public_anonymous=False override when result_visibility=PUBLIC must return 422."""
    poll = poll_service.create_poll(
        organization,
        _create_payload(
            organization,
            result_visibility=ResourceVisibility.PUBLIC,
            public_anonymous=True,
        ),
    )
    response = owner_client.post(
        _duplicate_url(poll),
        data={"name": "Bad Copy", "public_anonymous": False},
        content_type="application/json",
    )
    assert response.status_code == 422
    # The original poll is untouched
    poll.refresh_from_db()
    assert poll.public_anonymous is True
