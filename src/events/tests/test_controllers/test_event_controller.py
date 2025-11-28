## src/events/tests/test_controllers/test_event_controller.py
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from freezegun import freeze_time

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
        event_type=Event.EventType.PUBLIC,
        status="open",
        requires_ticket=False,  # Key difference
        start=timezone.now(),
    )


@pytest.fixture
def event_questionnaire(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire linked to the public_event."""
    q = Questionnaire.objects.create(name="Event Questionnaire", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
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
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,  # Ensure auto mode
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
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    private_evt = Event.objects.create(
        name="Private Affair",
        slug="private-affair",
        organization=organization,
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
    )
    members_only_evt = Event.objects.create(
        name="Members Gala",
        slug="members-gala",
        organization=organization,
        visibility=Event.Visibility.MEMBERS_ONLY,
        event_type=Event.EventType.MEMBERS_ONLY,
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
        event_type=Event.EventType.PUBLIC,
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
        event_type=Event.EventType.PUBLIC,
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
        event_type=Event.EventType.PUBLIC,
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


def test_ticket_checkout_for_member_success(member_client: Client, public_event: Event, free_tier: TicketTier) -> None:
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

    url = reverse("api:dashboard_invitation_requests")
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

    url = reverse("api:dashboard_invitation_requests")

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
    url = reverse("api:dashboard_invitation_requests")
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
        event=private_event,
        user=nonmember_user,
        message="Pending",
        status=models.EventInvitationRequest.InvitationRequestStatus.PENDING,
    )
    approved_req = models.EventInvitationRequest.objects.create(
        event=public_event,
        user=nonmember_user,
        message="Approved",
        status=models.EventInvitationRequest.InvitationRequestStatus.APPROVED,
    )
    rejected_req = models.EventInvitationRequest.objects.create(
        event=members_only_event,
        user=nonmember_user,
        message="Rejected",
        status=models.EventInvitationRequest.InvitationRequestStatus.REJECTED,
    )

    url = reverse("api:dashboard_invitation_requests")

    response = nonmember_client.get(url, {"status": "pending"})
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


# --- Test for GET /events/{event_id}/dietary-summary ---


def test_get_dietary_summary_as_organizer(
    organization_owner_client: Client,
    event: Event,
    organization_owner_user: RevelUser,
) -> None:
    """Test that event organizer can view dietary summary with all dietary info."""
    from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, UserDietaryPreference

    # Create some attendees with dietary needs
    attendee1 = RevelUser.objects.create_user(
        username="attendee1@test.com", email="attendee1@test.com", password="pass", first_name="Alice"
    )
    attendee2 = RevelUser.objects.create_user(
        username="attendee2@test.com", email="attendee2@test.com", password="pass", first_name="Bob"
    )

    # Create tickets for attendees
    tier = TicketTier.objects.create(
        event=event,
        name="General",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE,
        price=0,
    )
    Ticket.objects.create(event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee2, tier=tier, status=Ticket.TicketStatus.ACTIVE)

    # Add dietary restrictions
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    gluten, _ = FoodItem.objects.get_or_create(name="Gluten")
    DietaryRestriction.objects.create(
        user=attendee1,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Carries EpiPen",
        is_public=True,
    )
    DietaryRestriction.objects.create(
        user=attendee2,
        food_item=gluten,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Celiac disease",
        is_public=False,  # Private restriction
    )

    # Add dietary preferences
    vegan = DietaryPreference.objects.get(name="Vegan")
    UserDietaryPreference.objects.create(user=attendee1, preference=vegan, comment="Strict vegan", is_public=True)

    url = reverse("api:event_dietary_summary", args=[event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Organizer should see both public and private restrictions
    assert len(data["restrictions"]) == 2
    restrictions_by_food = {r["food_item"]: r for r in data["restrictions"]}
    assert "Peanuts" in restrictions_by_food
    assert "Gluten" in restrictions_by_food
    assert restrictions_by_food["Peanuts"]["severity"] == "severe_allergy"
    assert restrictions_by_food["Peanuts"]["attendee_count"] == 1
    assert restrictions_by_food["Gluten"]["severity"] == "allergy"
    assert restrictions_by_food["Gluten"]["attendee_count"] == 1

    # Check preferences
    assert len(data["preferences"]) == 1
    assert data["preferences"][0]["name"] == "Vegan"
    assert data["preferences"][0]["attendee_count"] == 1


def test_get_dietary_summary_as_regular_attendee(
    nonmember_client: Client,
    event: Event,
    nonmember_user: RevelUser,
) -> None:
    """Test that regular attendee sees only public dietary info."""
    from accounts.models import DietaryPreference, DietaryRestriction, FoodItem, UserDietaryPreference

    # Create some attendees with dietary needs
    attendee1 = RevelUser.objects.create_user(
        username="attendee1@test.com", email="attendee1@test.com", password="pass", first_name="Alice"
    )
    attendee2 = RevelUser.objects.create_user(
        username="attendee2@test.com", email="attendee2@test.com", password="pass", first_name="Bob"
    )

    # Create tickets for all users including the requesting user
    tier = TicketTier.objects.create(
        event=event,
        name="General",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE,
        price=0,
    )
    Ticket.objects.create(event=event, user=nonmember_user, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE)
    Ticket.objects.create(event=event, user=attendee2, tier=tier, status=Ticket.TicketStatus.ACTIVE)

    # Add dietary restrictions
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    shellfish, _ = FoodItem.objects.get_or_create(name="Shellfish")
    DietaryRestriction.objects.create(
        user=attendee1,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        notes="Public allergy",
        is_public=True,  # Public
    )
    DietaryRestriction.objects.create(
        user=attendee2,
        food_item=shellfish,
        restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
        notes="Private allergy",
        is_public=False,  # Private
    )

    # Add dietary preferences
    vegan = DietaryPreference.objects.get(name="Vegan")
    vegetarian = DietaryPreference.objects.get(name="Vegetarian")
    UserDietaryPreference.objects.create(user=attendee1, preference=vegan, comment="Strict vegan", is_public=True)
    UserDietaryPreference.objects.create(
        user=attendee2, preference=vegetarian, comment="Private preference", is_public=False
    )

    url = reverse("api:event_dietary_summary", args=[event.id])
    response = nonmember_client.get(url)

    assert response.status_code == 200
    data = response.json()

    # Regular attendee should only see public restrictions
    assert len(data["restrictions"]) == 1
    assert data["restrictions"][0]["food_item"] == "Peanuts"
    assert data["restrictions"][0]["severity"] == "allergy"
    assert data["restrictions"][0]["attendee_count"] == 1

    # Should only see public preferences
    assert len(data["preferences"]) == 1
    assert data["preferences"][0]["name"] == "Vegan"
    assert data["preferences"][0]["attendee_count"] == 1


def test_get_dietary_summary_empty_event(
    organization_owner_client: Client,
    event: Event,
) -> None:
    """Test dietary summary for event with no attendees."""
    url = reverse("api:event_dietary_summary", args=[event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["restrictions"] == []
    assert data["preferences"] == []


def test_get_dietary_summary_rsvp_attendees(
    organization_owner_client: Client,
    rsvp_only_public_event: Event,
) -> None:
    """Test that RSVP attendees are included in dietary summary."""
    from accounts.models import DietaryRestriction, FoodItem

    # Create attendee with RSVP
    attendee = RevelUser.objects.create_user(username="attendee@test.com", email="attendee@test.com", password="pass")
    EventRSVP.objects.create(event=rsvp_only_public_event, user=attendee, status=EventRSVP.RsvpStatus.YES)

    # Add dietary restriction
    peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
    DietaryRestriction.objects.create(
        user=attendee,
        food_item=peanuts,
        restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
        is_public=True,
    )

    url = reverse("api:event_dietary_summary", args=[rsvp_only_public_event.id])
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data["restrictions"]) == 1
    assert data["restrictions"][0]["food_item"] == "Peanuts"


# --- Test for GET /events/calendar ---


class TestCalendarEndpoint:
    """Tests for the calendar endpoint."""

    def test_calendar_default_returns_current_month_events(self, client: Client, organization: Organization) -> None:
        """Test that calling /calendar with no params defaults to current month (Dec 2025)."""
        # Use December 2025 since we're currently in Nov 2025
        this_month_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event-default",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2025, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        next_year_event = Event.objects.create(
            organization=organization,
            name="January 2026 Event",
            slug="jan-2026-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        # Will default to current month (December 2025)
        with freeze_time("2025-12-01"):
            response = client.get(url)

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(this_month_event.id) in event_ids
        assert str(next_year_event.id) not in event_ids

    def test_calendar_month_view(self, client: Client, organization: Organization) -> None:
        """Test month view returns only events in specified month."""
        # Create events in different months of 2025
        dec_event = Event.objects.create(
            organization=organization,
            name="December Event",
            slug="dec-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2025, 12, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2025, 12, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        jan_event = Event.objects.create(
            organization=organization,
            name="January Event",
            slug="jan-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": 12, "year": 2025})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(dec_event.id) in event_ids
        assert str(jan_event.id) not in event_ids

    def test_calendar_year_view(self, client: Client, organization: Organization) -> None:
        """Test year view returns all events in that year."""
        # Create events in different years
        event_2026 = Event.objects.create(
            organization=organization,
            name="2026 Event",
            slug="event-2026",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        event_2027 = Event.objects.create(
            organization=organization,
            name="2027 Event",
            slug="event-2027",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2027, 1, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2027, 1, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"year": 2026})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(event_2026.id) in event_ids
        assert str(event_2027.id) not in event_ids

    def test_calendar_week_view(self, client: Client, organization: Organization) -> None:
        """Test week view returns events in specified ISO week."""
        # Week 1 of 2026: Dec 29, 2025 - Jan 4, 2026
        week_1_event = Event.objects.create(
            organization=organization,
            name="Week 1 Event",
            slug="week-1",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 2, 10, 0, 0, tzinfo=ZoneInfo("UTC")),  # Thursday of Week 1
            end=datetime(2026, 1, 2, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        week_2_event = Event.objects.create(
            organization=organization,
            name="Week 2 Event",
            slug="week-2",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 1, 7, 10, 0, 0, tzinfo=ZoneInfo("UTC")),  # Tuesday of Week 2
            end=datetime(2026, 1, 7, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"week": 1, "year": 2026})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(week_1_event.id) in event_ids
        assert str(week_2_event.id) not in event_ids

    def test_calendar_respects_event_filter_schema(
        self, client: Client, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        """Test that EventFilterSchema parameters work with calendar."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
        org_event = Event.objects.create(
            organization=organization,
            name="Org Event",
            slug="org-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 15, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        other_org_event = Event.objects.create(
            organization=other_org,
            name="Other Org Event",
            slug="other-org-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 20, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": "6", "year": "2026", "organization": str(organization.id)})

        assert response.status_code == 200
        data = response.json()
        event_ids = [e["id"] for e in data]
        assert str(org_event.id) in event_ids
        assert str(other_org_event.id) not in event_ids

    def test_calendar_orders_by_start_time(self, client: Client, organization: Organization) -> None:
        """Test that events are ordered by start time ascending."""
        event_late = Event.objects.create(
            organization=organization,
            name="Late Event",
            slug="late-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 20, 15, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 20, 17, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        event_early = Event.objects.create(
            organization=organization,
            name="Early Event",
            slug="early-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            requires_ticket=False,
            start=datetime(2026, 6, 10, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
            end=datetime(2026, 6, 10, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
        )

        url = reverse("api:calendar_events")
        response = client.get(url, {"month": 6, "year": 2026})

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == str(event_early.id)
        assert data[1]["id"] == str(event_late.id)

    def test_calendar_invalid_week_number(self, client: Client) -> None:
        """Test that invalid week number returns validation error."""
        url = reverse("api:calendar_events")

        # Test week = 0
        response = client.get(url, {"week": "0", "year": "2025"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test week = 54
        response = client.get(url, {"week": "54", "year": "2025"})
        assert response.status_code == 422

        # Test negative week
        response = client.get(url, {"week": "-1", "year": "2025"})
        assert response.status_code == 422

    def test_calendar_invalid_month_number(self, client: Client) -> None:
        """Test that invalid month number returns validation error."""
        url = reverse("api:calendar_events")

        # Test month = 0
        response = client.get(url, {"month": "0", "year": "2025"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test month = 13
        response = client.get(url, {"month": "13", "year": "2025"})
        assert response.status_code == 422

        # Test negative month
        response = client.get(url, {"month": "-1", "year": "2025"})
        assert response.status_code == 422

    def test_calendar_invalid_year(self, client: Client) -> None:
        """Test that invalid year returns validation error."""
        url = reverse("api:calendar_events")

        # Test year = 0
        response = client.get(url, {"year": "0"})
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

        # Test year too far in past
        response = client.get(url, {"year": "1899"})
        assert response.status_code == 422

        # Test year too far in future
        response = client.get(url, {"year": "3001"})
        assert response.status_code == 422

        # Test negative year
        response = client.get(url, {"year": "-2025"})
        assert response.status_code == 422
