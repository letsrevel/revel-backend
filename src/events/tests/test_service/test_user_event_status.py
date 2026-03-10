"""Tests for get_user_event_status with cancelled tickets.

Regression: users with only cancelled tickets were skipping eligibility
checks (e.g. questionnaire gate) because any ticket presence was treated
as "already interacting with the event".
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    Organization,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from events.service.event_manager import NextStep, Reasons
from events.service.event_manager.types import EventUserEligibility
from events.service.ticket_service import UserEventStatus, get_user_event_status
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


class TestGetUserEventStatusCancelledTickets:
    """Tests for get_user_event_status when user only has cancelled tickets."""

    @pytest.fixture
    def org_owner(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Organization owner."""
        return revel_user_factory(username="org_owner")

    @pytest.fixture
    def org(self, org_owner: RevelUser) -> Organization:
        """Test organization."""
        return Organization.objects.create(
            name="Test Org",
            slug="test-org",
            owner=org_owner,
        )

    @pytest.fixture
    def event(self, org: Organization) -> Event:
        """Open public event requiring tickets."""
        return Event.objects.create(
            organization=org,
            name="Test Event",
            slug="test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
        )

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        """Free ticket tier."""
        return TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """Regular user."""
        return revel_user_factory(username="regular_user")

    def test_cancelled_ticket_with_questionnaire_returns_eligibility(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
        org: Organization,
    ) -> None:
        """User with only cancelled tickets should be blocked by questionnaire gate.

        Scenario: organizer cancels a user's ticket, then adds a questionnaire.
        The user should not be able to bypass the questionnaire requirement.
        """
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            guest_name="Test User",
            status=Ticket.TicketStatus.CANCELLED,
        )

        questionnaire = Questionnaire.objects.create(
            name="Admission Q",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=org,
            questionnaire=questionnaire,
        )
        org_q.events.add(event)

        result = get_user_event_status(event, user)

        assert isinstance(result, EventUserEligibility)
        assert result.allowed is False
        assert result.reason == Reasons.QUESTIONNAIRE_MISSING
        assert result.next_step == NextStep.COMPLETE_QUESTIONNAIRE

    def test_cancelled_ticket_without_questionnaire_returns_status(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
    ) -> None:
        """User with only cancelled tickets and no blockers should get UserEventStatus.

        When eligible, the response should include cancelled tickets and allow
        purchasing more.
        """
        cancelled_ticket = Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            guest_name="Test User",
            status=Ticket.TicketStatus.CANCELLED,
        )

        result = get_user_event_status(event, user)

        assert isinstance(result, UserEventStatus)
        assert len(result.tickets) == 1
        assert result.tickets[0].id == cancelled_ticket.id
        assert result.can_purchase_more is True

    def test_active_ticket_skips_eligibility(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
        org: Organization,
    ) -> None:
        """User with an active ticket should get UserEventStatus even with questionnaire.

        A user who already has an active ticket should not be re-checked for
        eligibility (they already passed it when purchasing).
        """
        Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            guest_name="Test User",
            status=Ticket.TicketStatus.ACTIVE,
        )

        questionnaire = Questionnaire.objects.create(
            name="Admission Q",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=org,
            questionnaire=questionnaire,
        )
        org_q.events.add(event)

        result = get_user_event_status(event, user)

        assert isinstance(result, UserEventStatus)
        assert len(result.tickets) == 1
