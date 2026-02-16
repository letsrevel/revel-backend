from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    GeneralUserPreferences,
    Organization,
    Payment,
    Ticket,
    TicketTier,
)
from events.tasks import build_attendee_visibility_flags, cleanup_expired_payments, cleanup_ticket_file_cache

pytestmark = pytest.mark.django_db


@patch("events.service.user_preferences_service.resolve_visibility_fast")
def test_build_attendee_visibility_flags(
    mock_resolve_visibility_fast: MagicMock,
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Test that build_attendee_visibility_flags creates flags correctly."""
    # Arrange
    # 2 attendees, 1 invitee
    attendee1 = revel_user_factory()
    attendee2 = revel_user_factory()
    invitee = revel_user_factory()

    # Viewer is attendee1, target is attendee2
    # Let's say attendee1 can see attendee2
    # Configure the mock to return boolean values directly
    mock_resolve_visibility_fast.side_effect = lambda viewer, target, *args, **kwargs: (
        viewer == attendee1 and target == attendee2
    )

    tier = event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    EventRSVP.objects.create(event=event, user=attendee2, status=EventRSVP.RsvpStatus.YES)
    event.invitations.create(user=invitee)

    # Act
    build_attendee_visibility_flags(str(event.id))

    # Assert
    event.refresh_from_db()
    assert event.attendee_count == 2

    assert AttendeeVisibilityFlag.objects.count() > 0

    # Viewer: attendee1, Target: attendee2 -> Visible
    flag1 = AttendeeVisibilityFlag.objects.get(user=attendee1, event=event, target=attendee2)
    assert flag1.is_visible is True

    # Viewer: attendee2, Target: attendee1 -> Not Visible
    flag2 = AttendeeVisibilityFlag.objects.get(user=attendee2, event=event, target=attendee1)
    assert flag2.is_visible is False

    # Check that a flag was created for the invitee as a viewer
    assert AttendeeVisibilityFlag.objects.filter(user=invitee, event=event).exists()


def test_build_attendee_visibility_flags_integration(
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """
    Integration test for build_attendee_visibility_flags without mocking resolve_visibility.
    """
    # Arrange
    attendee1 = revel_user_factory()
    attendee2 = revel_user_factory()

    tier = event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    EventRSVP.objects.create(event=event, user=attendee2, status=EventRSVP.RsvpStatus.YES)

    # attendee1 wants to be seen by everyone
    GeneralUserPreferences.objects.filter(user=attendee1).update(
        show_me_on_attendee_list=GeneralUserPreferences.VisibilityPreference.ALWAYS
    )
    # attendee2 wants to be seen by no one
    GeneralUserPreferences.objects.filter(user=attendee2).update(
        show_me_on_attendee_list=GeneralUserPreferences.VisibilityPreference.NEVER
    )

    # Act
    build_attendee_visibility_flags(str(event.id))

    # Assert
    # Anyone can see attendee1
    assert AttendeeVisibilityFlag.objects.get(user=attendee2, event=event, target=attendee1).is_visible

    # No one can see attendee2
    assert not AttendeeVisibilityFlag.objects.get(user=attendee1, event=event, target=attendee2).is_visible


def test_build_attendee_visibility_flags_no_attendees(
    event: Event,
) -> None:
    """Test that build_attendee_visibility_flags handles the case with no attendees."""
    # Act
    build_attendee_visibility_flags(str(event.id))

    # Assert
    event.refresh_from_db()
    assert event.attendee_count == 0
    assert AttendeeVisibilityFlag.objects.filter(event=event).count() == 0


def test_build_attendee_visibility_flags_replaces_existing(
    event: Event,
    revel_user_factory: RevelUserFactory,
) -> None:
    """Test that build_attendee_visibility_flags replaces existing flags."""
    # Arrange
    attendee1 = revel_user_factory()
    attendee2 = revel_user_factory()

    # Create tickets to make them attendees
    tier = event.ticket_tiers.first()
    assert tier is not None
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee1, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )
    Ticket.objects.create(
        guest_name="Test Guest", event=event, user=attendee2, tier=tier, status=Ticket.TicketStatus.ACTIVE
    )

    # Create initial visibility flags (opposite of what they should be)
    AttendeeVisibilityFlag.objects.all().delete()
    AttendeeVisibilityFlag.objects.create(user=attendee1, event=event, target=attendee2, is_visible=False)
    AttendeeVisibilityFlag.objects.create(user=attendee2, event=event, target=attendee1, is_visible=False)

    # Set preferences to make them visible to each other
    GeneralUserPreferences.objects.filter(user=attendee1).update(
        show_me_on_attendee_list=GeneralUserPreferences.VisibilityPreference.ALWAYS
    )
    GeneralUserPreferences.objects.filter(user=attendee2).update(
        show_me_on_attendee_list=GeneralUserPreferences.VisibilityPreference.ALWAYS
    )

    # Act
    build_attendee_visibility_flags(str(event.id))

    # Assert
    # The flags should be replaced with new ones where is_visible=True
    assert AttendeeVisibilityFlag.objects.get(user=attendee1, event=event, target=attendee2).is_visible
    assert AttendeeVisibilityFlag.objects.get(user=attendee2, event=event, target=attendee1).is_visible


class TestCleanupExpiredPayments:
    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        return revel_user_factory()

    @pytest.fixture
    def another_organization(self, user: RevelUser) -> Organization:
        return Organization.objects.create(name="Another Org", slug="another-org", owner=user)

    @pytest.fixture
    def another_event(self, another_organization: Organization, next_week: datetime) -> Event:
        return Event.objects.create(organization=another_organization, name="Another Event", start=next_week)

    @pytest.fixture
    def tier(self, event: Event) -> TicketTier:
        tier, _ = TicketTier.objects.get_or_create(event=event, name="Paid Tier", price=Decimal("10.00"))
        return tier

    @pytest.fixture
    def another_tier(self, another_event: Event) -> TicketTier:
        tier, _ = TicketTier.objects.get_or_create(event=another_event, name="Another Tier", price=Decimal("20.00"))
        return tier

    def test_cleanup_no_expired_payments(self) -> None:
        """Test that the task does nothing and returns 0 when there are no expired payments."""
        result = cleanup_expired_payments()
        assert result == 0

    def test_cleanup_single_expired_payment(self, tier: TicketTier, user: RevelUser) -> None:
        """Test that a single expired payment and its ticket are deleted, and tier quantity is updated."""
        # Arrange
        ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            stripe_session_id="sess_expired",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        tier.quantity_sold = 1
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 0
        assert not Payment.objects.exists()
        assert not Ticket.objects.exists()

    def test_cleanup_multiple_expired_payments(
        self, tier: TicketTier, another_tier: TicketTier, user: RevelUser
    ) -> None:
        """Test cleanup of multiple payments across different tiers."""
        # Arrange
        # Payment 1
        ticket1 = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=ticket1,
            user=user,
            stripe_session_id="sess_expired1",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        # Payment 2
        ticket2 = Ticket.objects.create(
            guest_name="Test Guest",
            event=another_tier.event,
            tier=another_tier,
            user=user,
            status=Ticket.TicketStatus.PENDING,
        )
        Payment.objects.create(
            ticket=ticket2,
            user=user,
            stripe_session_id="sess_expired2",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=another_tier.price,
            platform_fee=10,
        )

        tier.quantity_sold = 1
        tier.save()
        another_tier.quantity_sold = 1
        another_tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 2
        tier.refresh_from_db()
        another_tier.refresh_from_db()
        assert tier.quantity_sold == 0
        assert another_tier.quantity_sold == 0
        assert not Payment.objects.exists()
        assert not Ticket.objects.exists()

    def test_cleanup_ignores_non_expired_payments(
        self, tier: TicketTier, user: RevelUser, member_user: RevelUser
    ) -> None:
        """Test that active pending payments are not affected."""
        # Arrange
        # Expired payment
        expired_ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.PENDING
        )
        Payment.objects.create(
            ticket=expired_ticket,
            user=user,
            stripe_session_id="sess_expired",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=10,
        )
        # Active payment
        active_ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=member_user, status=Ticket.TicketStatus.PENDING
        )
        active_payment = Payment.objects.create(
            ticket=active_ticket,
            user=member_user,
            stripe_session_id="sess_active",
            status=Payment.PaymentStatus.PENDING,
            expires_at=timezone.now() + timedelta(minutes=30),
            amount=tier.price,
            platform_fee=10,
        )

        tier.quantity_sold = 2
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 1
        tier.refresh_from_db()
        assert tier.quantity_sold == 1  # One was released
        assert Payment.objects.count() == 1
        assert Payment.objects.first() == active_payment
        assert Ticket.objects.count() == 1
        assert Ticket.objects.first() == active_ticket

    def test_cleanup_ignores_non_pending_payments(self, tier: TicketTier, user: RevelUser) -> None:
        """Test that succeeded, failed, etc. payments are not cleaned up even if expired."""
        # Arrange
        ticket = Ticket.objects.create(
            guest_name="Test Guest", event=tier.event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE
        )
        Payment.objects.create(
            ticket=ticket,
            user=user,
            stripe_session_id="sess_succeeded",
            status=Payment.PaymentStatus.SUCCEEDED,
            expires_at=timezone.now() - timedelta(minutes=1),
            amount=tier.price,
            platform_fee=5,
        )

        tier.quantity_sold = 1
        tier.save()

        # Act
        result = cleanup_expired_payments()

        # Assert
        assert result == 0
        tier.refresh_from_db()
        assert tier.quantity_sold == 1
        assert Payment.objects.count() == 1
        assert Ticket.objects.count() == 1


class TestCleanupTicketFileCache:
    """Tests for the cleanup_ticket_file_cache Celery task.

    This task deletes cached PDF/pkpass files for tickets whose events
    have ended, freeing storage for past events.
    """

    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        """User for ticket creation."""
        return revel_user_factory()

    @pytest.fixture
    def past_org(self, user: RevelUser) -> Organization:
        """Organization for past event tests."""
        return Organization.objects.create(name="Past Org", slug="past-org", owner=user)

    @pytest.fixture
    def past_event(self, past_org: Organization) -> Event:
        """Event that has already ended."""
        now = timezone.now()
        return Event.objects.create(
            organization=past_org,
            name="Past Event",
            slug="past-event",
            start=now - timedelta(days=2),
            end=now - timedelta(days=1),
            requires_ticket=True,
            status=Event.EventStatus.CLOSED,
        )

    @pytest.fixture
    def future_org(self, user: RevelUser) -> Organization:
        """Organization for future event tests."""
        return Organization.objects.create(name="Future Org", slug="future-org", owner=user)

    @pytest.fixture
    def future_event(self, future_org: Organization) -> Event:
        """Event that has not ended yet."""
        now = timezone.now()
        return Event.objects.create(
            organization=future_org,
            name="Future Event",
            slug="future-event",
            start=now + timedelta(days=7),
            end=now + timedelta(days=7, hours=3),
            requires_ticket=True,
            status=Event.EventStatus.OPEN,
        )

    @pytest.fixture
    def past_tier(self, past_event: Event) -> TicketTier:
        return TicketTier.objects.create(event=past_event, name="Past Tier", price=0)

    @pytest.fixture
    def future_tier(self, future_event: Event) -> TicketTier:
        return TicketTier.objects.create(event=future_event, name="Future Tier", price=0)

    def _create_ticket_with_files(
        self,
        event: Event,
        tier: TicketTier,
        user: RevelUser,
        *,
        has_pdf: bool = False,
        has_pkpass: bool = False,
    ) -> Ticket:
        """Helper to create a ticket and attach cached files via the service."""
        from events.service import ticket_file_service

        ticket = Ticket.objects.create(
            event=event,
            user=user,
            tier=tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name="Cache Test",
        )
        if has_pdf or has_pkpass:
            ticket_file_service.cache_files(
                ticket,
                pdf_bytes=b"%PDF-cached" if has_pdf else None,
                pkpass_bytes=b"PK-cached" if has_pkpass else None,
            )
            ticket.refresh_from_db()
        return ticket

    def test_cleans_both_files_for_past_event(self, past_event: Event, past_tier: TicketTier, user: RevelUser) -> None:
        """Should delete both PDF and pkpass for tickets of past events."""
        ticket = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 1}
        ticket.refresh_from_db()
        assert not ticket.pdf_file
        assert not ticket.pkpass_file
        assert ticket.file_content_hash is None

    def test_cleans_pdf_only_ticket(self, past_event: Event, past_tier: TicketTier, user: RevelUser) -> None:
        """Should handle tickets with only a PDF file."""
        ticket = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 1}
        ticket.refresh_from_db()
        assert not ticket.pdf_file

    def test_cleans_pkpass_only_ticket(self, past_event: Event, past_tier: TicketTier, user: RevelUser) -> None:
        """Should handle tickets with only a pkpass file."""
        ticket = self._create_ticket_with_files(past_event, past_tier, user, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 1}
        ticket.refresh_from_db()
        assert not ticket.pkpass_file

    def test_skips_future_event_tickets(
        self,
        future_event: Event,
        future_tier: TicketTier,
        user: RevelUser,
    ) -> None:
        """Should not clean up files for tickets whose events have not ended."""
        ticket = self._create_ticket_with_files(future_event, future_tier, user, has_pdf=True, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 0}
        ticket.refresh_from_db()
        assert ticket.pdf_file
        assert ticket.pkpass_file

    def test_skips_tickets_without_files(self, past_event: Event, past_tier: TicketTier, user: RevelUser) -> None:
        """Should not count tickets that have no cached files."""
        self._create_ticket_with_files(past_event, past_tier, user, has_pdf=False, has_pkpass=False)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 0}

    def test_returns_correct_count_for_multiple_tickets(
        self, past_event: Event, past_tier: TicketTier, user: RevelUser
    ) -> None:
        """Should return the correct count when cleaning multiple tickets."""
        self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)
        self._create_ticket_with_files(past_event, past_tier, user, has_pkpass=True)
        self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 3}

    def test_mixed_past_and_future_events(
        self,
        past_event: Event,
        past_tier: TicketTier,
        future_event: Event,
        future_tier: TicketTier,
        user: RevelUser,
    ) -> None:
        """Should only clean files for past events, leaving future events intact."""
        past_ticket = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)
        future_ticket = self._create_ticket_with_files(future_event, future_tier, user, has_pdf=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 1}
        past_ticket.refresh_from_db()
        future_ticket.refresh_from_db()
        assert not past_ticket.pdf_file
        assert future_ticket.pdf_file

    def test_continues_processing_when_one_ticket_fails(
        self, past_event: Event, past_tier: TicketTier, user: RevelUser
    ) -> None:
        """Should continue cleaning remaining tickets when one ticket's file deletion fails."""
        ticket1 = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)
        ticket2 = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)
        ticket3 = self._create_ticket_with_files(past_event, past_tier, user, has_pdf=True)

        call_count = 0

        def delete_side_effect(save: bool = True) -> None:
            nonlocal call_count
            call_count += 1
            # Fail on the second ticket's delete
            if call_count == 2:
                raise OSError("disk error")

        with patch.object(
            Ticket.pdf_file.field.attr_class,  # type: ignore[attr-defined]
            "delete",
            side_effect=delete_side_effect,
        ):
            result = cleanup_ticket_file_cache()

        # 2 out of 3 should succeed
        assert result == {"cleaned": 2}
        ticket1.refresh_from_db()
        ticket2.refresh_from_db()
        ticket3.refresh_from_db()
        # ticket2 should still have its file (deletion failed)
        assert ticket2.pdf_file
        assert ticket2.file_content_hash is not None
