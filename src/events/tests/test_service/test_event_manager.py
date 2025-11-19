import typing as t
from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventRSVP,
    EventSeries,
    MembershipTier,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationStaff,
    Ticket,
    TicketTier,
)
from events.service.event_manager import EligibilityService, EventManager, NextStep, Reasons, UserIsIneligibleError
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


# --- Test Cases for Privileged Access (Fast Path) ---


def test_owner_gets_immediate_access(organization_owner_user: RevelUser, public_event: Event) -> None:
    """The organization owner should always get access with the 'staff' tier."""
    handler = EligibilityService(user=organization_owner_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_staff_gets_immediate_access(
    organization_staff_user: RevelUser, public_event: Event, staff_member: OrganizationStaff
) -> None:
    """A staff member should always get access with the 'staff' tier."""
    handler = EligibilityService(user=organization_staff_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_staff_tier_overrides_invitation_tier(
    organization_staff_user: RevelUser, private_event: Event, vip_tier: TicketTier, staff_member: OrganizationStaff
) -> None:
    """Tests logical hierarchy: staff access is checked before invitations."""
    # Invite a staff member to a VIP tier
    TicketTier.objects.create(event=private_event, name="VIP")  # ensure tier exists for private event
    EventInvitation.objects.create(user=organization_staff_user, event=private_event, tier=vip_tier)

    handler = EligibilityService(user=organization_staff_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Standard Access and Tiers ---


def test_public_user_gets_access_to_public_event(public_user: RevelUser, public_event: Event) -> None:
    """A general user should get access to a public event with no specific tier."""
    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_member_gets_member_tier_for_public_event(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember
) -> None:
    """A member should be assigned the 'member' tier for a public event."""
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_invited_user_gets_invited_tier(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """An invited user should be assigned the tier from their invitation."""
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Gates (Failures) ---


def test_event_is_full(public_user: RevelUser, member_user: RevelUser, public_event: Event) -> None:
    """Test the availability gate: deny access if event is at max capacity."""
    public_event.max_attendees = 1
    public_event.waitlist_open = True
    public_event.save()

    # The first user takes the only spot
    general_tier = TicketTier.objects.create(event=public_event, name="General")
    Ticket.objects.create(event=public_event, user=public_user, tier=general_tier)

    # The second user should be denied access
    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.EVENT_IS_FULL
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.JOIN_WAITLIST


def test_event_no_max_attendees(public_user: RevelUser, member_user: RevelUser, public_event: Event) -> None:
    """Test that if max_attendees are 0, the event is open to all."""
    public_event.max_attendees = 0
    public_event.save()
    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()
    assert eligibility.allowed


def test_private_event_requires_invitation(public_user: RevelUser, private_event: Event) -> None:
    """A non-invited user should be denied access to a private event."""
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.REQUEST_INVITATION


def test_members_only_event_requires_membership(public_user: RevelUser, members_only_event: Event) -> None:
    """A non-member should be denied access to a members-only event."""
    handler = EligibilityService(user=public_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERS_ONLY
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.BECOME_MEMBER


def test_members_only_event_blocks_inactive_member(member_user: RevelUser, members_only_event: Event) -> None:
    """A member with inactive status should be denied access to a members-only event."""
    # Create a membership with PAUSED status
    membership = OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE
    assert eligibility.next_step is None  # User needs to contact org to reactivate

    # Test with CANCELLED status
    membership.status = OrganizationMember.MembershipStatus.CANCELLED
    membership.save()

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE

    # Test with BANNED status
    membership.status = OrganizationMember.MembershipStatus.BANNED
    membership.save()

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_INACTIVE


def test_members_only_event_allows_active_member(member_user: RevelUser, members_only_event: Event) -> None:
    """An active member should be allowed access to a members-only event."""
    # Create a membership with ACTIVE status
    OrganizationMember.objects.create(
        organization=members_only_event.organization,
        user=member_user,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    handler = EligibilityService(user=member_user, event=members_only_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Questionnaire Gate ---


def test_questionnaire_is_missing(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    questionnaire: Questionnaire,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if a required questionnaire has not been submitted."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [questionnaire.id]


def test_questionnaire_is_pending_review(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    submitted_submission: QuestionnaireSubmission,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their submission has not yet been evaluated."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_PENDING_REVIEW
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION
    assert eligibility.questionnaires_pending_review == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 1
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_FAILED
    assert eligibility.next_step is None
    assert eligibility.questionnaires_failed == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected_and_can_retake_after_time(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected, but can retake after a certain time."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = timedelta(hours=1)
    org_questionnaire.questionnaire.save()

    rejected_evaluation.submission.submitted_at = timezone.now() - timedelta(hours=2)
    rejected_evaluation.submission.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_rejected_and_must_wait_to_retake(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected and must wait to retake."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = timedelta(hours=23, minutes=59, seconds=59)
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_FAILED
    assert eligibility.next_step == NextStep.WAIT_TO_RETAKE_QUESTIONNAIRE


def test_questionnaire_is_rejected_and_can_retake_immediately(
    member_user: RevelUser,
    public_event: Event,
    event_series: EventSeries,
    organization_membership: OrganizationMember,
    rejected_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is denied if their evaluation was rejected, but can retake immediately."""
    org_questionnaire.event_series.add(event_series)
    public_event.event_series = event_series
    public_event.save()
    org_questionnaire.questionnaire.max_attempts = 2
    org_questionnaire.questionnaire.can_retake_after = None
    org_questionnaire.questionnaire.save()

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.QUESTIONNAIRE_MISSING
    assert eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE
    assert eligibility.questionnaires_missing == [org_questionnaire.questionnaire_id]


def test_questionnaire_is_approved(
    member_user: RevelUser,
    public_event: Event,
    organization_membership: OrganizationMember,
    approved_evaluation: QuestionnaireEvaluation,
    org_questionnaire: OrganizationQuestionnaire,
) -> None:
    """User is allowed if their evaluation was approved."""
    org_questionnaire.events.add(public_event)

    handler = EligibilityService(user=member_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Override Logic ---


def test_invitation_overrides_max_attendees(
    public_user: RevelUser, member_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """An invited user should get access even if the event is full."""
    private_event.max_attendees = 1
    private_event.save()
    invitation.overrides_max_attendees = True
    invitation.save()

    # A different user takes the only spot
    general_tier = TicketTier.objects.create(event=private_event, name="General")
    Ticket.objects.create(event=private_event, user=member_user, tier=general_tier)

    # The invited user should still be allowed
    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_invitation_waives_questionnaire(
    public_user: RevelUser, private_event: Event, org_questionnaire: OrganizationQuestionnaire
) -> None:
    """An invited user with an override should get access despite questionnaire requirements."""
    # This user has no submission, which would normally fail
    EventInvitation.objects.create(user=public_user, event=private_event, waives_questionnaire=True)

    handler = EligibilityService(user=public_user, event=private_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


@pytest.fixture
def free_tier(public_event: Event) -> TicketTier:
    """Create a free ticket tier for convenience"""
    return TicketTier.objects.create(
        event=public_event,
        name="Free Tier",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def test_creates_ticket_for_eligible_user(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """
    Verify that a ticket is successfully created for an eligible user.
    The correct tier ("member") should be determined and created.
    """
    handler = EventManager(user=member_user, event=public_event)

    assert Ticket.objects.count() == 0

    # Act
    ticket = handler.create_ticket(free_tier)
    assert isinstance(ticket, Ticket)

    # Assert
    assert Ticket.objects.count() == 1
    assert ticket.user == member_user
    assert ticket.event == public_event
    assert ticket.status == Ticket.TicketStatus.ACTIVE

    # Verify the "member" tier was created and assigned
    assert public_event.ticket_tiers.filter(name=free_tier.name).exists()


def test_create_ticket_is_idempotent(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """
    Verify that calling create_ticket multiple times does not create duplicate tickets
    due to the use of get_or_create.
    """
    handler = EventManager(user=member_user, event=public_event)

    # Act
    handler.create_ticket(free_tier)
    with pytest.raises(HttpError):
        handler.create_ticket(free_tier)

    # Assert
    assert Ticket.objects.count() == 1
    assert public_event.ticket_tiers.filter(name=free_tier.name).exists()


def test_raises_error_for_ineligible_user(
    public_user: RevelUser, members_only_event: Event, free_tier: TicketTier
) -> None:
    """
    Verify that UserIsIneligibleError is raised when attempting to create a ticket
    for a user who does not pass the eligibility checks.
    """
    handler = EventManager(user=public_user, event=members_only_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(free_tier)

    # Assert that the exception contains the correct, detailed eligibility object
    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERS_ONLY
    assert eligibility.next_step is not None
    assert eligibility.next_step == NextStep.BECOME_MEMBER
    assert Ticket.objects.count() == 0


def test_bypass_eligibility_creates_ticket_for_ineligible_user(
    public_user: RevelUser, members_only_event: Event, free_tier: TicketTier
) -> None:
    """
    Verify that setting bypass_eligibility_checks=True successfully creates a ticket
    for a user who would otherwise be ineligible.
    """
    handler = EventManager(user=public_user, event=members_only_event)

    # Act
    ticket = handler.create_ticket(free_tier, bypass_eligibility_checks=True)
    assert isinstance(ticket, Ticket)

    # Assert
    assert Ticket.objects.count() == 1
    assert ticket.user == public_user


def test_private_event_rsvp_requires_invitation(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_INVITATION
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_requires_ticket(public_user: RevelUser, private_event: Event) -> None:
    """Test that a user cannot RSVP without invitation."""
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.rsvp(EventRSVP.RsvpStatus.YES)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.REQUIRES_TICKET
    assert not EventRSVP.objects.filter(event=private_event, user=public_user).exists()


def test_private_event_rsvp_with_invitation(
    public_user: RevelUser, private_event: Event, invitation: EventInvitation
) -> None:
    """Test that a user can RSVP with an invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    handler.rsvp(EventRSVP.RsvpStatus.YES)

    rsvp = EventRSVP.objects.filter(event=private_event, user=public_user).first()
    assert rsvp is not None
    assert rsvp.status == EventRSVP.RsvpStatus.YES


def test_private_event_create_ticket_rsvp_only(
    public_user: RevelUser, private_event: Event, free_tier: TicketTier
) -> None:
    """Test that a user cannot RSVP without invitation."""
    private_event.requires_ticket = False
    private_event.save()
    handler = EventManager(user=public_user, event=private_event)
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(free_tier)

    eligibility = exc_info.value.eligibility

    assert eligibility.reason == Reasons.MUST_RSVP
    assert not Ticket.objects.filter(event=private_event, user=public_user).exists()


# --- Test Cases for RSVP Deadline Gate ---


def test_rsvp_deadline_passed_blocks_access(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline blocks access when deadline has passed."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.RSVP_DEADLINE_PASSED
    assert eligibility.next_step is None


def test_rsvp_deadline_allows_access_before_deadline(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline allows access when deadline has not passed."""
    # Set up event without tickets and with future RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() + timedelta(hours=1)  # 1 hour from now
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_ignored_for_ticket_events(public_user: RevelUser, public_event: Event) -> None:
    """Test that RSVP deadline is ignored for events that require tickets."""
    # Set up event with tickets and expired RSVP deadline
    public_event.requires_ticket = True
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since RSVP deadline doesn't apply to ticket events
    assert eligibility.allowed is True


def test_rsvp_deadline_waived_by_invitation(public_user: RevelUser, public_event: Event) -> None:
    """Test that invitation can waive RSVP deadline."""
    # Set up event without tickets and with expired RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = timezone.now() - timedelta(hours=1)  # 1 hour ago
    public_event.save()

    # Create invitation that waives RSVP deadline
    EventInvitation.objects.create(user=public_user, event=public_event, waives_rsvp_deadline=True)

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_rsvp_deadline_no_deadline_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that no RSVP deadline allows access."""
    # Set up event without tickets and no RSVP deadline
    public_event.requires_ticket = False
    public_event.rsvp_before = None
    public_event.save()

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


# --- Test Cases for Ticket Sales Window Gate ---


def test_ticket_sales_window_active(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when sales window is active."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create ticket tier with active sales window
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=timezone.now() - timedelta(hours=1),  # Started 1 hour ago
        sales_end_at=timezone.now() + timedelta(hours=1),  # Ends in 1 hour
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_window_not_started(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets cannot be purchased before sales window starts."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with future sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() + timedelta(hours=1),  # Starts in 1 hour
        sales_end_at=timezone.now() + timedelta(hours=24),  # Ends in 24 hours
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE
    assert eligibility.next_step is None


def test_ticket_sales_window_ended(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets cannot be purchased after sales window ends."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with past sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=24),  # Started 24 hours ago
        sales_end_at=timezone.now() - timedelta(hours=1),  # Ended 1 hour ago
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE
    assert eligibility.next_step is None


def test_ticket_sales_no_window_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when no sales window is set."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create ticket tier with no sales window
    TicketTier.objects.create(
        event=public_event,
        name="General",
        # No sales_start_at or sales_end_at set
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_multiple_tiers_one_active(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets can be purchased when at least one tier has active sales."""
    # Set up event with tickets required
    public_event.requires_ticket = True
    public_event.save()

    # Create one tier with past sales window
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=24),
        sales_end_at=timezone.now() - timedelta(hours=1),
    )

    # Create another tier with active sales window
    TicketTier.objects.create(
        event=public_event,
        name="Regular",
        sales_start_at=timezone.now() - timedelta(hours=1),
        sales_end_at=timezone.now() + timedelta(hours=1),
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    assert eligibility.allowed is True


def test_ticket_sales_window_ignored_for_rsvp_events(public_user: RevelUser, public_event: Event) -> None:
    """Test that sales windows are ignored for events that don't require tickets."""
    # Set up event without tickets required
    public_event.requires_ticket = False
    public_event.save()

    # Create ticket tier with past sales window (should be ignored)
    TicketTier.objects.create(
        event=public_event,
        name="RSVP",
        sales_start_at=timezone.now() - timedelta(hours=24),
        sales_end_at=timezone.now() - timedelta(hours=1),
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since this is an RSVP event, not a ticket event
    assert eligibility.allowed is True


def test_ticket_sales_uses_event_end_when_sales_end_not_set(public_user: RevelUser, public_event: Event) -> None:
    """Test that TicketSalesGate uses event end when sales_end_at is not provided."""
    # Set up event with tickets required and end time in the future
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=2)
    public_event.save()

    # Create ticket tier with sales_start_at but no sales_end_at
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=timezone.now() - timedelta(hours=1),  # Started 1 hour ago
        sales_end_at=None,  # No explicit end time - should use event end
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed since event hasn't ended yet
    assert eligibility.allowed is True


def test_ticket_sales_blocks_when_event_ended_and_no_sales_end(public_user: RevelUser, public_event: Event) -> None:
    """Test that tickets are blocked when event has ended and no sales_end_at is set."""
    # Set up event with tickets required and end time in the past
    public_event.requires_ticket = True
    public_event.start = timezone.now() - timedelta(hours=12)
    public_event.end = timezone.now() + timedelta(hours=1)
    public_event.save()

    public_event.ticket_tiers.all().delete()

    # Create ticket tier with no sales_end_at (should use past event end)
    TicketTier.objects.create(
        event=public_event,
        name="General",
        sales_start_at=public_event.start - timedelta(days=4),  # Started 4 hours ago
        sales_end_at=None,  # No explicit end time - should use event end
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be blocked since event has ended
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE


def test_ticket_sales_explicit_end_overrides_event_end(public_user: RevelUser, public_event: Event) -> None:
    """Test that explicit sales_end_at takes precedence over event end."""
    # Set up event with end time in the future
    public_event.ticket_tiers.all().delete()
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=5)
    public_event.save()
    public_event.ticket_tiers.all().delete()

    # Create ticket tier with explicit sales_end_at that is in the past
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=2),
        sales_end_at=timezone.now() - timedelta(hours=1),  # Explicit end in the past
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be blocked because explicit sales_end_at has passed, even though event hasn't ended
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.NO_TICKETS_ON_SALE


def test_ticket_sales_mixed_tiers_one_uses_event_end(public_user: RevelUser, public_event: Event) -> None:
    """Test mixed scenario with one tier using event end and another with explicit end."""
    # Set up event with end time in the future
    public_event.requires_ticket = True
    public_event.start = timezone.now()
    public_event.end = timezone.now() + timedelta(hours=3)
    public_event.save()

    # Create one tier with explicit sales_end_at in the past
    TicketTier.objects.create(
        event=public_event,
        name="Early Bird",
        sales_start_at=timezone.now() - timedelta(hours=2),
        sales_end_at=timezone.now() - timedelta(hours=1),  # Ended 1 hour ago
    )

    # Create another tier with no sales_end_at (should use event end)
    TicketTier.objects.create(
        event=public_event,
        name="Regular",
        sales_start_at=timezone.now() - timedelta(hours=1),
        sales_end_at=None,  # Should use event end (3 hours from now)
    )

    handler = EligibilityService(user=public_user, event=public_event)
    eligibility = handler.check_eligibility()

    # Should be allowed because Regular tier is still on sale (using event end)
    assert eligibility.allowed is True


# --- Test Cases for Waives Purchase Logic ---


def test_invitation_waives_purchase_creates_complimentary_ticket(
    public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that invitation with waives_purchase=True creates complimentary ACTIVE ticket."""
    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert ticket.user == public_user
    assert ticket.event == public_event
    assert ticket.tier == free_tier


def test_waives_purchase_increments_quantity_sold(public_user: RevelUser, public_event: Event) -> None:
    """Test that complimentary tickets properly increment quantity_sold."""
    # Create tier with quantity tracking
    tier = TicketTier.objects.create(
        event=public_event,
        name="Limited Tier",
        total_quantity=10,
        quantity_sold=5,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    handler.create_ticket(tier)

    # Assert - quantity_sold should be incremented
    tier.refresh_from_db()
    assert tier.quantity_sold == 6


def test_waives_purchase_bypasses_payment_flow(public_user: RevelUser, public_event: Event) -> None:
    """Test that waives_purchase bypasses normal payment flow for paid tiers."""
    # Create paid tier
    paid_tier = TicketTier.objects.create(
        event=public_event,
        name="Paid Tier",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(paid_tier)

    # Assert - should get direct ticket, not payment flow
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE
    assert not hasattr(ticket, "payment")  # No payment object should be created


def test_normal_user_without_waives_purchase_gets_payment_flow(public_user: RevelUser, public_event: Event) -> None:
    """Test that normal users without waives_purchase go through payment flow."""
    from unittest.mock import patch

    # Create paid tier
    paid_tier = TicketTier.objects.create(
        event=public_event,
        name="Paid Tier",
        price=50.00,
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    handler = EventManager(user=public_user, event=public_event)

    # Mock the ticket service checkout to return a checkout URL
    with patch("events.service.ticket_service.TicketService.checkout") as mock_checkout:
        mock_checkout.return_value = "https://checkout.stripe.com/mock-url"

        # Act
        result = handler.create_ticket(paid_tier)

        # Assert - should get checkout URL string, not ticket object
        assert isinstance(result, str)
        assert result.startswith("https://")  # Should be Stripe checkout URL
        mock_checkout.assert_called_once()


def test_waives_purchase_respects_capacity_limits(
    public_user: RevelUser, member_user: RevelUser, public_event: Event
) -> None:
    """Test that complimentary tickets still respect tier capacity limits."""
    # Create tier at capacity
    tier = TicketTier.objects.create(
        event=public_event,
        name="Limited Tier",
        total_quantity=1,
        quantity_sold=1,  # Already at capacity
        payment_method=TicketTier.PaymentMethod.ONLINE,
    )

    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act & Assert - should still fail due to capacity
    with pytest.raises(UserIsIneligibleError) as exc_info:  # Should raise some capacity-related error
        handler.create_ticket(tier)
    assert exc_info.value.eligibility.reason == Reasons.SOLD_OUT


def test_waives_purchase_works_with_free_tiers(
    public_user: RevelUser, public_event: Event, free_tier: TicketTier
) -> None:
    """Test that waives_purchase works correctly with already-free tiers."""
    # Create invitation that waives purchase
    EventInvitation.objects.create(user=public_user, event=public_event, waives_purchase=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert - should still create complimentary ticket (bypassing any free flow)
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


# --- Test Cases for Membership Tier Restrictions ---


def test_ticket_tier_without_membership_restriction_allows_all(
    member_user: RevelUser, public_event: Event, organization_membership: OrganizationMember, free_tier: TicketTier
) -> None:
    """Test that ticket tiers without membership restrictions allow any member to purchase."""
    # free_tier has no restricted_to_membership_tiers set
    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(free_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_allows_correct_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with required membership tier can purchase restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Assign user to gold tier
    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=gold_tier, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(ticket_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_blocks_wrong_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with wrong membership tier cannot purchase restricted ticket."""
    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    # Assign user to silver tier
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    # Create ticket tier restricted to gold members only
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_blocks_non_member(
    public_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that non-member cannot purchase membership-restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=public_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_allows_multiple_tiers(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that ticket tier restricted to multiple membership tiers allows any of them."""
    # Create membership tiers
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")
    platinum_tier = MembershipTier.objects.create(organization=organization, name="Platinum")
    silver_tier = MembershipTier.objects.create(organization=organization, name="Silver")

    # Assign user to silver tier
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=silver_tier,
        status=OrganizationMember.MembershipStatus.ACTIVE,
    )

    # Create ticket tier restricted to silver OR gold OR platinum
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="Premium Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier, platinum_tier, silver_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act
    ticket = handler.create_ticket(ticket_tier)

    # Assert
    assert isinstance(ticket, Ticket)
    assert ticket.status == Ticket.TicketStatus.ACTIVE


def test_ticket_tier_with_membership_restriction_blocks_inactive_member(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that user with paused/cancelled membership cannot purchase restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Assign user to gold tier but with PAUSED status
    OrganizationMember.objects.create(
        organization=organization,
        user=member_user,
        tier=gold_tier,
        status=OrganizationMember.MembershipStatus.PAUSED,
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP


def test_ticket_tier_with_membership_restriction_waived_by_invitation(
    public_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that invitation with waives_membership_required bypasses membership tier requirement."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    # Create invitation that waives membership requirement
    EventInvitation.objects.create(user=public_user, event=public_event, waives_membership_required=True)

    handler = EventManager(user=public_user, event=public_event)

    # Act - should succeed despite not having required tier
    result = handler.create_ticket(ticket_tier)

    # Assert - will go through payment flow since waives_purchase is False
    # But membership tier check should be bypassed
    assert result is not None


def test_ticket_tier_with_membership_restriction_blocks_member_without_tier(
    member_user: RevelUser, public_event: Event, organization: t.Any
) -> None:
    """Test that member without any tier cannot purchase tier-restricted ticket."""
    # Create membership tier
    gold_tier = MembershipTier.objects.create(organization=organization, name="Gold")

    # Create member without tier assignment
    OrganizationMember.objects.create(
        organization=organization, user=member_user, tier=None, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    # Create ticket tier restricted to gold members
    ticket_tier = TicketTier.objects.create(
        event=public_event, name="VIP Ticket", payment_method=TicketTier.PaymentMethod.FREE
    )
    ticket_tier.restricted_to_membership_tiers.add(gold_tier)

    handler = EventManager(user=member_user, event=public_event)

    # Act & Assert
    with pytest.raises(UserIsIneligibleError) as exc_info:
        handler.create_ticket(ticket_tier)

    eligibility = exc_info.value.eligibility
    assert eligibility.allowed is False
    assert eligibility.reason == Reasons.MEMBERSHIP_TIER_REQUIRED
    assert eligibility.next_step == NextStep.UPGRADE_MEMBERSHIP
