import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    EventSeries,
    GeneralUserPreferences,
    HeldSeriesPass,
    Organization,
    Payment,
    SeriesPass,
    SeriesPassTierLink,
    Ticket,
    TicketTier,
)
from events.tasks import (
    build_attendee_visibility_flags,
    cleanup_expired_payments,
    cleanup_ticket_file_cache,
    generate_recurring_events_task,
)

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


class TestCleanupSeriesPassFileCacheSweep:
    """Tests for cleanup_ticket_file_cache's HeldSeriesPass sweep (same task, no new beat row).

    A held pass is only swept once every event its pass covers has ended, gated by the
    LAST covered event's end (not the first) — a pass still covering an upcoming event
    stays downloadable even if it also covers past events.
    """

    @pytest.fixture
    def user(self, revel_user_factory: RevelUserFactory) -> RevelUser:
        return revel_user_factory()

    @pytest.fixture
    def org(self, user: RevelUser) -> Organization:
        return Organization.objects.create(name="Pass Cache Org", slug="pass-cache-org", owner=user)

    @pytest.fixture
    def event_series(self, org: Organization) -> EventSeries:
        return EventSeries.objects.create(organization=org, name="Pass Cache Series", slug="pass-cache-series")

    def _make_covered_event(
        self,
        org: Organization,
        event_series: EventSeries,
        series_pass: SeriesPass,
        suffix: str,
        start: t.Any,
        end: t.Any,
    ) -> None:
        """Create an event + tier and link it to series_pass."""
        event = Event.objects.create(
            organization=org,
            event_series=event_series,
            name=f"Cache Event {suffix}",
            slug=f"cache-event-{suffix}",
            start=start,
            end=end,
            requires_ticket=True,
            status=Event.EventStatus.OPEN,
        )
        tier = TicketTier.objects.create(event=event, name=f"Cache Tier {suffix}", price=0, currency="EUR")
        SeriesPassTierLink.objects.create(series_pass=series_pass, event=event, tier=tier)

    @pytest.fixture
    def past_series_pass(self, org: Organization, event_series: EventSeries) -> SeriesPass:
        """A pass whose only covered event has already ended."""
        now = timezone.now()
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Past Pass",
            price=Decimal("10.00"),
            pro_rata_discount=Decimal("1.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        self._make_covered_event(
            org, event_series, series_pass, "past", now - timedelta(days=2), now - timedelta(days=1)
        )
        return series_pass

    @pytest.fixture
    def ongoing_series_pass(self, org: Organization, event_series: EventSeries) -> SeriesPass:
        """A pass covering one past and one future event — not fully over yet."""
        now = timezone.now()
        series_pass = SeriesPass.objects.create(
            event_series=event_series,
            name="Ongoing Pass",
            price=Decimal("20.00"),
            pro_rata_discount=Decimal("2.00"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        self._make_covered_event(
            org, event_series, series_pass, "ongoing-past", now - timedelta(days=2), now - timedelta(days=1)
        )
        self._make_covered_event(
            org, event_series, series_pass, "ongoing-future", now + timedelta(days=7), now + timedelta(days=7, hours=2)
        )
        return series_pass

    def _create_held_pass_with_files(
        self,
        series_pass: SeriesPass,
        user: RevelUser,
        *,
        has_pdf: bool = False,
        has_pkpass: bool = False,
    ) -> HeldSeriesPass:
        """Helper to create a held pass and attach cached files directly (no real PDF/pkpass generation)."""
        held_pass = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=user,
            status=HeldSeriesPass.Status.ACTIVE,
            price_paid=series_pass.price,
        )
        if has_pdf:
            held_pass.pdf_file.save("pass.pdf", ContentFile(b"%PDF-cached"), save=False)
        if has_pkpass:
            held_pass.pkpass_file.save("pass.pkpass", ContentFile(b"PK-cached"), save=False)
        if has_pdf or has_pkpass:
            held_pass.file_content_hash = "cached-hash"
            held_pass.save()
        return held_pass

    def test_cleans_pass_whose_last_covered_event_ended(self, past_series_pass: SeriesPass, user: RevelUser) -> None:
        held_pass = self._create_held_pass_with_files(past_series_pass, user, has_pdf=True, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 1}
        held_pass.refresh_from_db()
        assert not held_pass.pdf_file
        assert not held_pass.pkpass_file
        assert held_pass.file_content_hash is None

    def test_skips_pass_with_upcoming_covered_event(self, ongoing_series_pass: SeriesPass, user: RevelUser) -> None:
        """The pass covers a past AND a future event — its LAST event hasn't ended, so it's untouched."""
        held_pass = self._create_held_pass_with_files(ongoing_series_pass, user, has_pdf=True, has_pkpass=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 0}
        held_pass.refresh_from_db()
        assert held_pass.pdf_file
        assert held_pass.pkpass_file

    def test_skips_pass_without_files(self, past_series_pass: SeriesPass, user: RevelUser) -> None:
        self._create_held_pass_with_files(past_series_pass, user)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 0}

    def test_continues_processing_when_one_pass_fails(
        self, past_series_pass: SeriesPass, user: RevelUser, revel_user_factory: RevelUserFactory
    ) -> None:
        """Should continue cleaning remaining held passes when one pass's file deletion fails."""
        held_pass1 = self._create_held_pass_with_files(past_series_pass, user, has_pdf=True)
        other_user = revel_user_factory()
        held_pass2 = self._create_held_pass_with_files(past_series_pass, other_user, has_pdf=True)

        with patch.object(
            HeldSeriesPass.pdf_file.field.attr_class,  # type: ignore[attr-defined]
            "delete",
            side_effect=OSError("disk error"),
        ):
            result = cleanup_ticket_file_cache()

        # Both fail (same patched delete), but the loop must not raise partway through.
        assert result == {"cleaned": 0}
        held_pass1.refresh_from_db()
        held_pass2.refresh_from_db()
        assert held_pass1.pdf_file
        assert held_pass2.pdf_file

    def test_counts_tickets_and_passes_together(
        self,
        past_series_pass: SeriesPass,
        user: RevelUser,
        org: Organization,
        event_series: EventSeries,
    ) -> None:
        """The combined 'cleaned' counter spans both tickets and held passes in one run."""
        now = timezone.now()
        past_event = Event.objects.create(
            organization=org,
            event_series=event_series,
            name="Direct Past Event",
            slug="direct-past-event",
            start=now - timedelta(days=2),
            end=now - timedelta(days=1),
            requires_ticket=True,
            status=Event.EventStatus.CLOSED,
        )
        past_tier = TicketTier.objects.create(event=past_event, name="Direct Past Tier", price=0)
        ticket = Ticket.objects.create(
            event=past_event, user=user, tier=past_tier, status=Ticket.TicketStatus.ACTIVE, guest_name="Direct"
        )
        ticket.pdf_file.save("ticket.pdf", ContentFile(b"%PDF-cached"), save=False)
        ticket.file_content_hash = "cached-hash"
        ticket.save()
        self._create_held_pass_with_files(past_series_pass, user, has_pdf=True)

        result = cleanup_ticket_file_cache()

        assert result == {"cleaned": 2}


class TestGenerateRecurringEventsTask:
    """Tests for the daily Celery beat task that materializes recurring series."""

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_processes_active_series_only(
        self,
        mock_notify: MagicMock,
        organization: Organization,
    ) -> None:
        """The task only processes active series with both a rule and template."""
        from datetime import datetime as dt

        from freezegun import freeze_time

        from events.models import EventSeries, RecurrenceRule

        dtstart = timezone.make_aware(dt(2026, 4, 6, 10, 0))
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0],
            dtstart=dtstart,
        )
        active = EventSeries.objects.create(
            organization=organization,
            name="Active Series",
            recurrence_rule=rule,
            is_active=True,
            generation_window_weeks=4,
        )
        template = Event.objects.create(
            organization=organization,
            event_series=active,
            name="Active Template",
            start=dtstart,
            end=dtstart + timedelta(hours=2),
            status=Event.EventStatus.DRAFT,
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            is_template=True,
        )
        active.template_event = template
        active.save(update_fields=["template_event"])

        # Inactive series — must be skipped.
        rule2 = RecurrenceRule.objects.create(
            frequency=RecurrenceRule.Frequency.WEEKLY,
            interval=1,
            weekdays=[0],
            dtstart=dtstart,
        )
        inactive = EventSeries.objects.create(
            organization=organization,
            name="Inactive Series",
            recurrence_rule=rule2,
            is_active=False,
            generation_window_weeks=4,
        )
        inactive_template = Event.objects.create(
            organization=organization,
            event_series=inactive,
            name="Inactive Template",
            start=dtstart,
            end=dtstart + timedelta(hours=2),
            status=Event.EventStatus.DRAFT,
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            is_template=True,
        )
        inactive.template_event = inactive_template
        inactive.save(update_fields=["template_event"])

        # Series without recurrence rule or template — must be skipped via the queryset filter.
        EventSeries.objects.create(
            organization=organization,
            name="Bare Series",
            is_active=True,
        )

        # Act
        with freeze_time("2026-04-06 10:00:00"):
            result = generate_recurring_events_task()

        # Assert — only the active series is dispatched (inactive series and
        # series without rule/template are filtered out by the queryset). With
        # CELERY_TASK_ALWAYS_EAGER the dispatched subtask runs inline and
        # materializes 5 weekly Mondays — see test_weekly_rule_generates_expected_count
        # for the timezone boundary discussion.
        assert result == {"series_dispatched": 1}
        assert active.events.filter(is_template=False).count() == 5
        assert inactive.events.filter(is_template=False).count() == 0

    @patch("notifications.service.notification_helpers.notify_series_events_generated")
    def test_per_series_subtask_isolation_on_failure(
        self,
        mock_notify: MagicMock,
        organization: Organization,
        settings: t.Any,
    ) -> None:
        """One failing series subtask must not prevent the other series from generating.

        The beat task dispatches per-series subtasks via ``.delay()``. In
        production these are independent and a failure in one doesn't affect
        the others. In tests with CELERY_TASK_EAGER_PROPAGATES we disable
        propagation so the test reflects production semantics.
        """
        from datetime import datetime as dt

        from freezegun import freeze_time

        from events.models import EventSeries, RecurrenceRule

        # In production, .delay() dispatches and returns immediately; a failing
        # subtask is handled by Celery's retry loop. Emulate that here.
        settings.CELERY_TASK_EAGER_PROPAGATES = False

        dtstart = timezone.make_aware(dt(2026, 4, 6, 10, 0))

        def _build_series(name: str) -> EventSeries:
            rule = RecurrenceRule.objects.create(
                frequency=RecurrenceRule.Frequency.WEEKLY,
                interval=1,
                weekdays=[0],
                dtstart=dtstart,
            )
            series = EventSeries.objects.create(
                organization=organization,
                name=name,
                recurrence_rule=rule,
                is_active=True,
                generation_window_weeks=4,
            )
            template = Event.objects.create(
                organization=organization,
                event_series=series,
                name=f"{name} Template",
                start=dtstart,
                end=dtstart + timedelta(hours=2),
                status=Event.EventStatus.DRAFT,
                visibility=Event.Visibility.PUBLIC,
                event_type=Event.EventType.PUBLIC,
                is_template=True,
            )
            series.template_event = template
            series.save(update_fields=["template_event"])
            return series

        bad = _build_series("Bad Series")
        good = _build_series("Good Series")

        # Fake generate_series_events so it raises for the bad series and runs
        # normally for the good one. Patched at the tasks module where it's
        # imported inside the subtask.
        from events.service.recurrence_service import generate_series_events as real_generate

        def _side_effect(series: EventSeries, **kwargs: t.Any) -> list[Event]:
            if series.id == bad.id:
                raise RuntimeError("boom")
            return real_generate(series, **kwargs)

        with patch("events.service.recurrence_service.generate_series_events", side_effect=_side_effect):
            with freeze_time("2026-04-06 10:00:00"):
                result = generate_recurring_events_task()

        assert result == {"series_dispatched": 2}
        assert good.events.filter(is_template=False).count() == 5
        assert bad.events.filter(is_template=False).count() == 0
