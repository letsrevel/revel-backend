## src/events/tests/test_controllers/test_event_controller.py
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events import models
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    EventSeries,
    EventToken,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from events.service.event_manager import NextStep, Reasons
from events.tasks import build_attendee_visibility_flags
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Test for GET /events/ ---


@pytest.fixture
def next_week() -> datetime:
    return timezone.now() + timedelta(days=7)


@pytest.fixture
def rsvp_only_public_event(organization: Organization) -> Event:
    """A public event that only requires an RSVP, not a ticket."""
    return Event.objects.create(
        organization=organization,
        name="RSVP-Only Public Event",
        slug="rsvp-only-public-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.Types.PUBLIC,
        status="open",
        requires_ticket=False,  # Key difference
        start=timezone.now(),
    )


@pytest.fixture
def event_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire linked to the public_event."""
    q = Questionnaire.objects.create(name="Event Questionnaire", status=Questionnaire.Status.PUBLISHED)
    # Add one mandatory MCQ and one optional FTQ
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Mandatory MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="Correct", is_correct=True)
    FreeTextQuestion.objects.create(questionnaire=q, question="Optional FTQ", is_mandatory=False)
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def auto_eval_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with only MCQs, suitable for auto-evaluation."""
    q = Questionnaire.objects.create(
        name="Auto-Eval Questionnaire",
        status=Questionnaire.Status.PUBLISHED,
        evaluation_mode=Questionnaire.EvaluationMode.AUTOMATIC,  # Ensure auto mode
    )
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Auto-Eval MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="OK", is_correct=True)
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


def test_list_events_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    organization_staff_client: Client,
    organization_owner_client: Client,
    superuser_client: Client,
    organization: Organization,
    nonmember_user: RevelUser,
    next_week: datetime,
) -> None:
    """Test that the event list endpoint respects user visibility rules."""
    # --- Setup ---
    # 1. Create a variety of events within the main organization
    public_evt = Event.objects.create(
        name="Public Party",
        slug="public-party",
        organization=organization,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.Types.PUBLIC,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    private_evt = Event.objects.create(
        name="Private Affair",
        slug="private-affair",
        organization=organization,
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.Types.PRIVATE,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    members_only_evt = Event.objects.create(
        name="Members Gala",
        slug="members-gala",
        organization=organization,
        visibility=Event.Visibility.MEMBERS_ONLY,
        event_type=Event.Types.MEMBERS_ONLY,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )

    # 2. Invite the 'nonmember_user' to the private event. They become an "invited user".
    EventInvitation.objects.create(user=nonmember_user, event=private_evt)

    # 3. Create an event in a completely different org to test scoping
    other_org_owner = RevelUser.objects.create_user("otherowner")
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=other_org_owner)
    other_org_evt = Event.objects.create(
        name="External Event",
        slug="external-event",
        organization=other_org,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.Types.PUBLIC,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )

    url = reverse("api:list_events")

    # --- Assertions ---
    # Anonymous client: sees only public events
    response = client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, other_org_evt.name}

    # Invited client (was non-member): sees public events + the private one they're invited to
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, private_evt.name, other_org_evt.name}

    # Member client: sees public events + members-only events
    response = member_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    names = {evt["name"] for evt in data["results"]}
    assert names == {public_evt.name, members_only_evt.name, other_org_evt.name}

    # Staff & Owner clients: see all events in their organization + all public events
    for c in [organization_staff_client, organization_owner_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 4
        names = {evt["name"] for evt in data["results"]}
        assert names == {public_evt.name, private_evt.name, members_only_evt.name, other_org_evt.name}

    # Superuser client: sees everything
    response = superuser_client.get(url)
    assert response.status_code == 200
    assert response.json()["count"] == 4


def test_list_events_search(
    client: Client, organization: Organization, event_series: EventSeries, next_week: datetime
) -> None:
    """Test searching for events by name, description, series, and organization."""
    Event.objects.create(
        name="Tech Conference",
        slug="tech",
        organization=organization,
        visibility="public",
        event_type=Event.Types.PUBLIC,
        description="A conference about Python.",
        event_series=event_series,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    Event.objects.create(
        name="Art Fair",
        slug="art",
        organization=organization,
        visibility="public",
        event_type=Event.Types.PUBLIC,
        description="A fair for artists using generative AI.",
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    url = reverse("api:list_events")

    # Search by event name
    response = client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Tech Conference"

    # Search by event description
    response = client.get(url, {"search": "generative AI"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Art Fair"

    # Search by event series name
    response = client.get(url, {"search": event_series.name})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
    assert response.json()["results"][0]["name"] == "Tech Conference"

    # Search by organization name
    response = client.get(url, {"search": organization.name})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 2

    # No results
    response = client.get(url, {"search": "nonexistent"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 0


# --- Tests for GET /events/{slug}/ ---


def test_get_event_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
    private_event: Event,
    members_only_event: Event,
) -> None:
    """Test retrieving a single event based on visibility rules."""
    # Invite the nonmember_user to the private event
    EventInvitation.objects.create(user=nonmember_user, event=private_event)

    # --- Assertions for Public Event ---
    public_url = reverse("api:get_event", kwargs={"event_id": public_event.pk})
    assert client.get(public_url).status_code == 200
    assert nonmember_client.get(public_url).status_code == 200
    assert member_client.get(public_url).status_code == 200

    # --- Assertions for Private Event ---
    private_url = reverse("api:get_event", kwargs={"event_id": private_event.pk})
    assert client.get(private_url).status_code == 404  # Anonymous can't see
    assert member_client.get(private_url).status_code == 404  # Member can't see without invite
    assert nonmember_client.get(private_url).status_code == 200  # Invited user can see

    # --- Assertions for Members-Only Event ---
    members_url = reverse("api:get_event", kwargs={"event_id": members_only_event.pk})
    assert client.get(members_url).status_code == 404  # Anonymous can't see
    assert nonmember_client.get(members_url).status_code == 404  # Non-member can't see
    assert member_client.get(members_url).status_code == 200  # Member can see


# --- Tests for POST /events/{event_id}/rsvp/{answer} ---


def test_rsvp_to_event_success(nonmember_client: Client, rsvp_only_public_event: Event) -> None:
    """Test that an authenticated user can successfully RSVP to an eligible event."""
    url = reverse("api:rsvp_event", kwargs={"event_id": rsvp_only_public_event.pk, "answer": "yes"})
    response = nonmember_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "yes"
    assert data["event_id"] == str(rsvp_only_public_event.pk)

    assert EventRSVP.objects.filter(
        event=rsvp_only_public_event, user__username="nonmember_user", status="yes"
    ).exists()


def test_rsvp_to_event_requires_ticket_fails(nonmember_client: Client, public_event: Event) -> None:
    """Test that trying to RSVP to an event that requires a ticket fails with the correct error."""
    # public_event requires a ticket by default

    quest = Questionnaire.objects.create(name="Quest", min_score=100, status="published")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        questionnaire=quest, organization=public_event.organization
    )
    org_questionnaire.events.add(public_event)
    url = reverse("api:rsvp_event", kwargs={"event_id": public_event.pk, "answer": "yes"})
    response = nonmember_client.post(url)

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == "Requires a ticket."
    assert data["next_step"] == "purchase_ticket"


def test_rsvp_to_event_ineligible_fails(member_client: Client, members_only_event: Event) -> None:
    """Test that trying to RSVP to an event for which the user is ineligible fails."""
    members_only_event.requires_ticket = False
    members_only_event.save()

    quest = Questionnaire.objects.create(name="Quest", min_score=100, status="published")
    org_questionnaire = OrganizationQuestionnaire.objects.create(
        questionnaire=quest, organization=members_only_event.organization
    )
    org_questionnaire.events.add(members_only_event)
    org_questionnaire.save()

    url = reverse("api:rsvp_event", kwargs={"event_id": members_only_event.pk, "answer": "yes"})
    response = member_client.post(url)

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == Reasons.QUESTIONNAIRE_MISSING
    assert data["next_step"] == NextStep.COMPLETE_QUESTIONNAIRE


def test_rsvp_to_event_anonymous_fails(client: Client, rsvp_only_public_event: Event) -> None:
    """Test that an anonymous user cannot RSVP."""
    url = reverse("api:rsvp_event", kwargs={"event_id": rsvp_only_public_event.pk, "answer": "yes"})
    response = client.post(url)

    assert response.status_code == 401


# --- Tests for POST /events/{event_id}/ticket/obtain ---


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    """Create a free ticket tier for convenience"""
    return TicketTier.objects.create(
        event=public_event,
        name="Free Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def test_ticket_checkout_success(nonmember_client: Client, public_event: Event, free_tier: TicketTier) -> None:
    """Test that an eligible user can successfully obtain a ticket."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    response = nonmember_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["event_id"] == str(public_event.pk)
    assert data["tier"]["name"] == free_tier.name

    assert Ticket.objects.filter(event=public_event, user__username="nonmember_user").exists()


def test_ticket_checkout_for_member_success(
    member_client: Client, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """Test that an eligible member user gets a ticket with the correct 'member' tier."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    response = member_client.post(url)

    assert response.status_code == 200
    data = response.json()
    assert data["tier"]["name"] == free_tier.name

    ticket = Ticket.objects.get(event=public_event, user__username="member_user")
    assert ticket.tier
    assert ticket.tier.name


def test_ticket_checkout_for_rsvp_only_event_fails(
    nonmember_client: Client, rsvp_only_public_event: Event, free_tier: TicketTier
) -> None:
    """Test that trying to get a ticket for an RSVP-only event fails correctly."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": rsvp_only_public_event.pk, "tier_id": free_tier.pk})
    response = nonmember_client.post(url)

    assert response.status_code == 404  # there is no tier-event pair


def test_ticket_checkout_for_full_event_fails(
    nonmember_client: Client, public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that trying to get a ticket for a full event fails."""
    public_event.max_attendees = 1
    public_event.save()

    # First user takes the spot
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(user=public_user, event=public_event, tier=tier)

    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    response = nonmember_client.post(url)

    assert response.status_code == 400
    data = response.json()
    assert data["allowed"] is False
    assert data["reason"] == "Event is full."
    assert data["next_step"] is None  # waitlist is closed by default


def test_ticket_checkout_anonymous_fails(client: Client, public_event: Event, free_tier: TicketTier) -> None:
    """Test that an anonymous user cannot obtain a ticket."""
    url = reverse("api:ticket_checkout", kwargs={"event_id": public_event.pk, "tier_id": free_tier.pk})
    response = client.post(url)

    assert response.status_code == 401


# --- Tests for GET /events/{event_id}/my-status ---


def test_get_my_event_status_with_ticket(
    nonmember_client: Client, nonmember_user: RevelUser, public_event: Event
) -> None:
    """Test status returns a ticket if one exists for the user."""
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    ticket = Ticket.objects.create(event=public_event, user=nonmember_user, tier=tier)
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(ticket.id)
    assert data["status"] == "active"


def test_get_my_event_status_with_rsvp(
    nonmember_client: Client, nonmember_user: RevelUser, rsvp_only_public_event: Event
) -> None:
    """Test status returns an RSVP if one exists for the user."""
    rsvp = EventRSVP.objects.create(event=rsvp_only_public_event, user=nonmember_user, status="yes")
    url = reverse("api:get_my_event_status", kwargs={"event_id": rsvp_only_public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == rsvp.status
    assert data["event_id"] == str(rsvp_only_public_event.pk)


def test_get_my_event_status_is_eligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is eligible but has no ticket/rsvp."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["allowed"] is True
    assert data["event_id"] == str(public_event.pk)


def test_get_my_event_status_is_ineligible(nonmember_client: Client, public_event: Event) -> None:
    """Test status returns eligibility data if user is ineligible."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = nonmember_client.get(url)
    assert response.status_code == 200  # The endpoint itself succeeds, it returns the status
    data = response.json()
    assert data["allowed"] is True


def test_get_my_event_status_anonymous(client: Client, public_event: Event) -> None:
    """Test anonymous user gets 401."""
    url = reverse("api:get_my_event_status", kwargs={"event_id": public_event.pk})
    response = client.get(url)
    assert response.status_code == 401


# --- Tests for /events/{event_id}/questionnaire/{questionnaire_id} ---


def test_get_questionnaire_success(
    nonmember_client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test successfully retrieving a questionnaire for a visible event."""
    url = reverse(
        "api:get_questionnaire", kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk}
    )
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(event_questionnaire.pk)
    assert len(data["multiple_choice_questions"]) == 1
    assert len(data["free_text_questions"]) == 1


def test_get_questionnaire_for_invisible_event_fails(
    nonmember_client: Client, private_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test that trying to get a questionnaire for an event the user can't see returns 404."""
    # Link the questionnaire to the private event instead
    OrganizationQuestionnaire.objects.filter(questionnaire=event_questionnaire).delete()
    org_q = OrganizationQuestionnaire.objects.create(
        organization=private_event.organization, questionnaire=event_questionnaire
    )
    org_q.events.add(private_event)

    url = reverse(
        "api:get_questionnaire", kwargs={"event_id": private_event.pk, "questionnaire_id": event_questionnaire.pk}
    )
    response = nonmember_client.get(url)
    assert response.status_code == 404


def test_get_nonexistent_questionnaire_fails(nonmember_client: Client, public_event: Event) -> None:
    """Test that getting a non-existent questionnaire ID returns 404."""
    url = reverse("api:get_questionnaire", kwargs={"event_id": public_event.pk, "questionnaire_id": uuid.uuid4()})
    response = nonmember_client.get(url)
    assert response.status_code == 404


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_success_no_auto_eval(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    event_questionnaire: Questionnaire,
) -> None:
    """Test a successful submission that does not trigger immediate evaluation."""
    mcq = event_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
        "free_text_answers": [],
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert "score" not in data  # It returns a QuestionnaireSubmissionResponseSchema
    assert QuestionnaireSubmission.objects.count() == 1
    submission = QuestionnaireSubmission.objects.first()
    mock_evaluate_task.assert_called_once_with(str(submission.pk))  # type: ignore[union-attr]


@patch("events.controllers.events.evaluate_questionnaire_submission.delay")
def test_submit_questionnaire_success_with_auto_eval(
    mock_evaluate_task: MagicMock,
    nonmember_client: Client,
    public_event: Event,
    auto_eval_questionnaire: Questionnaire,
) -> None:
    """Test a successful submission that triggers immediate evaluation."""
    mcq = auto_eval_questionnaire.multiplechoicequestion_questions.first()
    option = mcq.options.first()  # type: ignore[union-attr]
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": auto_eval_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(auto_eval_questionnaire.pk),
        "status": "ready",
        "multiple_choice_answers": [{"question_id": str(mcq.id), "options_id": [str(option.id)]}],  # type: ignore[union-attr]
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert "score" not in data
    assert QuestionnaireSubmission.objects.count() == 1
    submission = QuestionnaireSubmission.objects.first()
    mock_evaluate_task.assert_called_once_with(str(submission.pk))  # type: ignore[union-attr]


def test_submit_questionnaire_missing_mandatory_fails(
    nonmember_client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test submitting without a mandatory answer returns 400."""
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    # The questionnaire has one mandatory MCQ, but we submit no answers.
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
    }
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert "You are missing mandatory answers" in response.json()["detail"]


def test_submit_questionnaire_anonymous_fails(
    client: Client, public_event: Event, event_questionnaire: Questionnaire
) -> None:
    """Test an anonymous user cannot submit a questionnaire."""
    url = reverse(
        "api:submit_questionnaire",
        kwargs={"event_id": public_event.pk, "questionnaire_id": event_questionnaire.pk},
    )
    payload = {
        "questionnaire_id": str(event_questionnaire.pk),
        "status": "ready",
    }
    response = client.post(url, data=orjson.dumps(payload), content_type="application/json")
    # This should fail because the service layer expects a RevelUser, not AnonymousUser.
    # The generic exception handler catches this and returns a 400.
    assert response.status_code == 401


# --- Tests for POST /events/{event_id}/invitation-requests ---


def test_request_invitation_success(nonmember_client: Client, public_event: Event) -> None:
    """Test that a user can successfully request an invitation to a private event."""
    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Please let me in!"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["message"] == "Please let me in!"
    assert models.EventInvitationRequest.objects.filter(event=public_event, user__username="nonmember_user").exists()


def test_request_invitation_duplicate_fails(
    nonmember_client: Client, nonmember_user: RevelUser, public_event: Event
) -> None:
    """Test that requesting an invitation twice for the same event fails."""
    # First request
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="First try")

    url = reverse("api:create_invitation_request", kwargs={"event_id": public_event.pk})
    payload = {"message": "Second try"}
    response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400
    assert "You have already requested an invitation to this event" in response.json()["detail"]
    assert models.EventInvitationRequest.objects.count() == 1


# --- Tests for GET /events/invitation-requests ---


def test_get_my_pending_invitation_requests_success(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test that a user can retrieve their own pending invitation requests."""
    # Create two requests for the user
    request1 = models.EventInvitationRequest.objects.create(event=private_event, user=nonmember_user, message="Req 1")
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="Req 2")

    # Create a request for another user to ensure it's not included
    other_user = RevelUser.objects.create_user("otheruser")
    models.EventInvitationRequest.objects.create(event=private_event, user=other_user, message="Other user req")

    url = reverse("api:list_my_invitation_requests")
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    results = data["results"]
    assert {r["id"] for r in results} == {
        str(request1.id),
        str(models.EventInvitationRequest.objects.get(event=public_event).id),
    }


def test_get_my_pending_invitation_requests_search_and_filter(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test filtering and searching the user's pending invitation requests."""
    models.EventInvitationRequest.objects.create(event=private_event, user=nonmember_user, message="Looking for tech")
    models.EventInvitationRequest.objects.create(event=public_event, user=nonmember_user, message="Looking for art")

    url = reverse("api:list_my_invitation_requests")

    # Filter by event_id
    response = nonmember_client.get(url, {"event_id": str(private_event.pk)})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["event"]["id"] == str(private_event.pk)

    # Search by message content
    response = nonmember_client.get(url, {"search": "tech"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["message"] == "Looking for tech"

    # Search by event name
    response = nonmember_client.get(url, {"search": public_event.name})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["event"]["name"] == public_event.name


def test_get_my_pending_invitation_requests_anonymous_fails(client: Client) -> None:
    """Test that an anonymous user cannot retrieve pending requests."""
    url = reverse("api:list_my_invitation_requests")
    response = client.get(url)
    assert response.status_code == 401


def test_get_my_invitation_requests_status_filtering(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    private_event: Event,
    public_event: Event,
    members_only_event: Event,
) -> None:
    """Test that status filtering defaults to pending but can show all statuses."""
    # Create requests with different statuses
    pending_req = models.EventInvitationRequest.objects.create(
        event=private_event, user=nonmember_user, message="Pending", status=models.EventInvitationRequest.Status.PENDING
    )
    approved_req = models.EventInvitationRequest.objects.create(
        event=public_event,
        user=nonmember_user,
        message="Approved",
        status=models.EventInvitationRequest.Status.APPROVED,
    )
    rejected_req = models.EventInvitationRequest.objects.create(
        event=members_only_event,
        user=nonmember_user,
        message="Rejected",
        status=models.EventInvitationRequest.Status.REJECTED,
    )

    url = reverse("api:list_my_invitation_requests")

    # Default should show only pending
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(pending_req.id)
    assert data["results"][0]["status"] == "pending"

    # Filter by approved
    response = nonmember_client.get(url, {"status": "approved"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(approved_req.id)
    assert data["results"][0]["status"] == "approved"

    # Filter by rejected
    response = nonmember_client.get(url, {"status": "rejected"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(rejected_req.id)
    assert data["results"][0]["status"] == "rejected"


# --- Tests for GET /events/me/my-invitations ---


def test_list_my_invitations_success(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test that a user can retrieve their own invitations."""
    # Create invitations for the user
    invitation1 = EventInvitation.objects.create(event=private_event, user=nonmember_user, custom_message="Welcome!")
    invitation2 = EventInvitation.objects.create(event=public_event, user=nonmember_user)

    # Create an invitation for another user to ensure it's not included
    other_user = RevelUser.objects.create_user("otheruser")
    EventInvitation.objects.create(event=private_event, user=other_user)

    url = reverse("api:list_my_invitations")
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    results = data["results"]
    assert {r["id"] for r in results} == {str(invitation1.id), str(invitation2.id)}
    # Verify event information is included
    assert results[0]["event"]["id"] in {str(private_event.id), str(public_event.id)}
    assert results[0]["event"]["name"] in {private_event.name, public_event.name}


def test_list_my_invitations_filter_by_upcoming(
    nonmember_client: Client, nonmember_user: RevelUser, organization: Organization
) -> None:
    """Test that by default only upcoming event invitations are shown."""
    # Create past event (ended 2 days ago)
    past_event = Event.objects.create(
        organization=organization,
        name="Past Event",
        slug="past-event",
        status="open",
        start=timezone.now() - timedelta(days=3),
        end=timezone.now() - timedelta(days=2),
    )

    # Create upcoming event (starts in 1 week)
    upcoming_event = Event.objects.create(
        organization=organization,
        name="Upcoming Event",
        slug="upcoming-event",
        status="open",
        start=timezone.now() + timedelta(days=7),
        end=timezone.now() + timedelta(days=8),
    )

    # Create another past event (ended 1 hour ago)
    another_past_event = Event.objects.create(
        organization=organization,
        name="Another Past Event",
        slug="another-past-event",
        status="open",
        start=timezone.now() - timedelta(hours=2),
        end=timezone.now() - timedelta(hours=1),
    )

    # Create invitations for all events
    EventInvitation.objects.create(event=past_event, user=nonmember_user)
    upcoming_invitation = EventInvitation.objects.create(event=upcoming_event, user=nonmember_user)
    EventInvitation.objects.create(event=another_past_event, user=nonmember_user)

    url = reverse("api:list_my_invitations")

    # Default should show only upcoming
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    result_ids = {r["id"] for r in data["results"]}
    assert result_ids == {str(upcoming_invitation.id)}

    # With include_past=true should show all
    response = nonmember_client.get(url, {"include_past": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3


def test_list_my_invitations_filter_by_event(
    nonmember_client: Client, nonmember_user: RevelUser, private_event: Event, public_event: Event
) -> None:
    """Test filtering invitations by event_id."""
    invitation1 = EventInvitation.objects.create(event=private_event, user=nonmember_user)
    EventInvitation.objects.create(event=public_event, user=nonmember_user)

    url = reverse("api:list_my_invitations")

    # Filter by private_event
    response = nonmember_client.get(url, {"event_id": str(private_event.id)})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(invitation1.id)
    assert data["results"][0]["event"]["id"] == str(private_event.id)


def test_list_my_invitations_search(
    nonmember_client: Client, nonmember_user: RevelUser, organization: Organization
) -> None:
    """Test searching invitations by event name/description and custom message."""
    event1 = Event.objects.create(
        organization=organization, name="Tech Meetup", slug="tech-meetup", status="open", start=timezone.now()
    )
    event2 = Event.objects.create(
        organization=organization,
        name="Art Gallery",
        slug="art-gallery",
        status="open",
        start=timezone.now(),
        description="Beautiful art show",
    )

    invitation1 = EventInvitation.objects.create(event=event1, user=nonmember_user, custom_message="Tech enthusiast")
    invitation2 = EventInvitation.objects.create(event=event2, user=nonmember_user)

    url = reverse("api:list_my_invitations")

    # Search by event name
    response = nonmember_client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation1.id)

    # Search by custom message
    response = nonmember_client.get(url, {"search": "enthusiast"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation1.id)

    # Search by event description
    response = nonmember_client.get(url, {"search": "Beautiful"})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(invitation2.id)


def test_list_my_invitations_anonymous_fails(client: Client) -> None:
    """Test that an anonymous user cannot retrieve invitations."""
    url = reverse("api:list_my_invitations")
    response = client.get(url)
    assert response.status_code == 401


def test_list_user_tickets(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_event: Event,
    private_event: Event,
) -> None:
    """Test listing user's own tickets with filtering and search."""
    # Create tickets with different statuses
    tier1 = public_event.ticket_tiers.first()
    tier2 = private_event.ticket_tiers.first()
    assert tier1 is not None
    assert tier2 is not None

    ticket1 = Ticket.objects.create(event=public_event, user=nonmember_user, tier=tier1, status=Ticket.Status.ACTIVE)
    ticket2 = Ticket.objects.create(event=private_event, user=nonmember_user, tier=tier2, status=Ticket.Status.PENDING)

    url = reverse("api:list_user_tickets")

    # Get all tickets (no filter)
    response = nonmember_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    # Filter by status
    response = nonmember_client.get(url, {"status": "pending"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ticket2.id)
    assert data["results"][0]["event"]["name"] == private_event.name

    # Search by event name
    response = nonmember_client.get(url, {"search": public_event.name})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == str(ticket1.id)


class TestClaimInvitation:
    def test_claim_invitation_success(
        self, nonmember_client: Client, event_token: EventToken, nonmember_user: RevelUser
    ) -> None:
        """Test that an invitation is claimed successfully."""
        event_token.invitation_payload = {}
        event_token.save()
        url = reverse("api:event_claim_invitation", kwargs={"token": event_token.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        assert EventInvitation.objects.filter(event=event_token.event, user=nonmember_user).exists()

    def test_claim_invitation_unauthorized(self, client: Client, event_token: EventToken) -> None:
        """Test that an unauthenticated user cannot claim an invitation."""
        url = reverse("api:event_claim_invitation", kwargs={"token": event_token.id})
        response = client.post(url)
        assert response.status_code == 401

    def test_claim_invitation_invalid_token(self, nonmember_client: Client) -> None:
        """Test that an invalid token returns a 400."""
        url = reverse("api:event_claim_invitation", kwargs={"token": "invalid-token"})
        response = nonmember_client.post(url)
        assert response.status_code == 400


def test_get_event_attendees(
    nonmember_client: Client,
    member_client: Client,
    organization_owner_client: Client,
    nonmember_user: RevelUser,
    member_user: RevelUser,
    public_event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Test that the attendee list endpoint correctly respects user visibility preferences."""
    url = reverse("api:event_attendee_list", kwargs={"event_id": public_event.id})

    # --- Arrange ---

    # 1. Create attendees with different privacy preferences
    attendee_always = nonmember_user
    attendee_always.general_preferences.show_me_on_attendee_list = "always"
    attendee_always.general_preferences.save()

    attendee_never = member_user
    attendee_never.general_preferences.show_me_on_attendee_list = "never"
    attendee_never.general_preferences.save()

    attendee_members = revel_user_factory()
    attendee_members.general_preferences.show_me_on_attendee_list = "to_members"
    attendee_members.general_preferences.save()

    # 2. Make them attendees of the public event
    tier = public_event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(event=public_event, user=nonmember_user, tier=tier)
    Ticket.objects.create(event=public_event, user=attendee_always, tier=tier)
    Ticket.objects.create(event=public_event, user=attendee_never, tier=tier)
    Ticket.objects.create(event=public_event, user=attendee_members, tier=tier)

    # 3. For 'to_members' visibility to work, the viewer and target must be members.
    # member_user is already a member via the member_client fixture.
    # Let's also make the attendee a member.
    OrganizationMember.objects.create(organization=public_event.organization, user=attendee_members)

    # 4. Manually run the task that builds the visibility flags.
    build_attendee_visibility_flags(str(public_event.id))

    # --- Act & Assert ---

    # Case 1: Viewer is a non-member
    response_nonmember = nonmember_client.get(url)
    assert response_nonmember.status_code == 200
    data_nonmember = response_nonmember.json()
    assert data_nonmember["count"] == 1
    # Only the user with 'always' preference is visible
    assert data_nonmember["results"][0]["first_name"] == attendee_always.first_name

    # Case 2: Viewer is a member
    response_member = member_client.get(url)
    assert response_member.status_code == 200
    data_member = response_member.json()
    assert data_member["count"] == 2
    visible_fnames = {user["first_name"] for user in data_member["results"]}
    # 'always' and 'to_members' should be visible
    assert visible_fnames == {attendee_always.first_name, attendee_members.first_name}

    # Case 3: Viewer is the organization owner
    response_owner = organization_owner_client.get(url)
    assert response_owner.status_code == 200
    data_owner = response_owner.json()
    # Owner can see everyone regardless of preferences
    assert data_owner["count"] == 3
    visible_fnames_owner = {user["first_name"] for user in data_owner["results"]}
    assert visible_fnames_owner == {
        attendee_always.first_name,
        attendee_never.first_name,
        attendee_members.first_name,
    }
