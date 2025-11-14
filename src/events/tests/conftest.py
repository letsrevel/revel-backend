import typing as t
from datetime import datetime, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventSeries,
    EventToken,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    OrganizationStaff,
    OrganizationToken,
    PermissionMap,
    PermissionsSchema,
    Ticket,
    TicketTier,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission


@pytest.fixture
def organization_owner_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="organization_owner_user", email="a@example.com", password="pass"
    )


@pytest.fixture
def organization_staff_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(
        username="organization_staff_user", email="b@example.com", password="pass"
    )


@pytest.fixture
def nonmember_user(django_user_model: t.Type[RevelUser]) -> RevelUser:
    return django_user_model.objects.create_user(username="nonmember_user", email="c@example.com", password="pass")


@pytest.fixture
def organization(organization_owner_user: RevelUser) -> Organization:
    return Organization.objects.create(
        name="Org", slug="org", owner=organization_owner_user, accept_membership_requests=True
    )


@pytest.fixture
def event_series(organization: Organization) -> EventSeries:
    return EventSeries.objects.create(organization=organization, name="Series", slug="series")


@pytest.fixture
def staff_member(organization: Organization, organization_staff_user: RevelUser) -> OrganizationStaff:
    return OrganizationStaff.objects.create(
        organization=organization,
        user=organization_staff_user,
        permissions=PermissionsSchema(default=PermissionMap(edit_organization=True)).model_dump(mode="json"),
    )


@pytest.fixture
def event(organization: Organization, event_series: EventSeries) -> Event:
    return Event.objects.create(
        organization=organization,
        name="Event",
        slug="event",
        event_type=Event.EventType.PUBLIC,
        event_series=event_series,
        max_attendees=100,
        start=timezone.now(),
        status="open",
        requires_ticket=True,
    )


# --- User Fixtures ---
# Your existing user fixtures are fine: organization_owner_user, organization_staff_user


@pytest.fixture
def member_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user who is a standard member of the organization."""
    return django_user_model.objects.create_user(username="member_user", email="member@example.com", password="pass")


@pytest.fixture
def public_user(django_user_model: type[RevelUser]) -> RevelUser:
    """A user with no special relationship to the organization."""
    return django_user_model.objects.create_user(username="public_user", email="public@example.com", password="pass")


# --- Organization Fixture ---
# Your existing organization fixture is fine


@pytest.fixture
def organization_membership(organization: Organization, member_user: RevelUser) -> OrganizationMember:
    """Make the member_user a member of the main organization."""
    return OrganizationMember.objects.create(organization=organization, user=member_user)


# --- Event and Tier Fixtures ---
@pytest.fixture
def public_event(organization: Organization, next_week: datetime) -> Event:
    """A standard public event."""
    return Event.objects.create(
        organization=organization,
        name="Public Event",
        slug="Public-Event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        max_attendees=10,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
        accept_invitation_requests=True,
        requires_ticket=True,
    )


@pytest.fixture
def private_event(organization: Organization, next_week: datetime) -> Event:
    """A private, invite-only event."""
    return Event.objects.create(
        organization=organization,
        name="Private Event",
        slug="Private-Event",
        visibility=Event.Visibility.PRIVATE,
        event_type=Event.EventType.PRIVATE,
        max_attendees=10,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
        accept_invitation_requests=True,
        requires_ticket=True,
    )


@pytest.fixture
def members_only_event(organization: Organization, next_week: datetime) -> Event:
    """A members-only event."""
    return Event.objects.create(
        organization=organization,
        name="Members Only Event",
        slug="Members-Only-Event",
        visibility=Event.Visibility.MEMBERS_ONLY,
        event_type=Event.EventType.MEMBERS_ONLY,
        status="open",
        start=next_week,
        end=next_week + timedelta(days=1),
        requires_ticket=True,
    )


@pytest.fixture
def vip_tier(public_event: Event) -> TicketTier:
    """A 'VIP' ticket tier for the public event."""
    return TicketTier.objects.create(event=public_event, name="VIP")


@pytest.fixture
def event_ticket_tier(event: Event) -> TicketTier:
    """A ticket tier for the generic event fixture."""
    return TicketTier.objects.create(
        event=event, name="General", price=10.00, payment_method=TicketTier.PaymentMethod.ONLINE
    )


@pytest.fixture
def ticket(event: Event, member_user: RevelUser, event_ticket_tier: TicketTier) -> Ticket:
    return Ticket.objects.create(event=event, user=member_user, tier=event_ticket_tier)


@pytest.fixture
def invitation(public_user: RevelUser, private_event: Event, vip_tier: TicketTier) -> EventInvitation:
    """An invitation for the public_user to the private_event for the VIP tier."""
    return EventInvitation.objects.create(
        user=public_user,
        event=private_event,
        tier=vip_tier,
        overrides_max_attendees=False,
        waives_questionnaire=False,
    )


# --- Request Fixtures ---
@pytest.fixture
def event_invitation_request(public_event: Event, public_user: RevelUser) -> "EventInvitationRequest":
    """An invitation request from the public_user for the public_event."""
    return EventInvitationRequest.objects.create(event=public_event, user=public_user)


@pytest.fixture
def event_token(event: Event) -> EventToken:
    """An event token."""
    return EventToken.objects.create(event=event, issuer=event.organization.owner)


@pytest.fixture
def organization_membership_request(
    organization: Organization, nonmember_user: RevelUser
) -> "OrganizationMembershipRequest":
    """A membership request from the nonmember_user for the organization."""
    return OrganizationMembershipRequest.objects.create(organization=organization, user=nonmember_user)


# --- Questionnaire Fixtures ---
@pytest.fixture
def questionnaire() -> Questionnaire:
    return Questionnaire.objects.create(name="Test Questionnaire", status=Questionnaire.QuestionnaireStatus.PUBLISHED)


@pytest.fixture
def org_questionnaire(organization: Organization, questionnaire: Questionnaire) -> OrganizationQuestionnaire:
    """Link the questionnaire to the main organization."""
    return OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)


@pytest.fixture
def submitted_submission(member_user: RevelUser, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    """A submitted questionnaire from the member_user."""
    return QuestionnaireSubmission.objects.create(
        user=member_user,
        questionnaire=questionnaire,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
    )


@pytest.fixture
def approved_evaluation(submitted_submission: QuestionnaireSubmission) -> QuestionnaireEvaluation:
    """An approved evaluation for the member's submission."""
    return QuestionnaireEvaluation.objects.create(
        submission=submitted_submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED
    )


@pytest.fixture
def rejected_evaluation(submitted_submission: QuestionnaireSubmission) -> QuestionnaireEvaluation:
    """A rejected evaluation for the member's submission."""
    return QuestionnaireEvaluation.objects.create(
        submission=submitted_submission, status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED
    )


@pytest.fixture
def png_bytes() -> bytes:
    """Return valid PNG bytes for a minimal 1x1 image."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01"  # Width: 1
        b"\x00\x00\x00\x01"  # Height: 1
        b"\x08\x06\x00\x00\x00"  # Bit depth, color type, compression, filter, interlace
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0aIDAT"
        b"\x78\x9c\x63\x00\x01\x00\x00\x05\x00\x01"
        b"\x0d\x0a\x2d\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.fixture
def png_file(png_bytes: bytes) -> SimpleUploadedFile:
    """Return a valid-looking PNG file upload."""
    return SimpleUploadedFile(
        name="test.png",
        content=png_bytes,
        content_type="image/png",
    )


@pytest.fixture
def organization_token(organization: Organization, organization_owner_user: RevelUser) -> OrganizationToken:
    return OrganizationToken.objects.create(
        organization=organization, name="Test Token", issuer=organization_owner_user
    )
