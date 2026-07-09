"""Tests for ApplePassGenerator.generate_series_pass / _build_series_pass_data."""

import io
import json
import typing as t
import zipfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)
from wallet.apple.generator import PASS_EXPIRATION_GRACE_PERIOD, ApplePassGenerator

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def event_series(organization: Organization) -> EventSeries:
    """Event series for wallet series pass tests."""
    return EventSeries.objects.create(organization=organization, name="Wallet Series", slug="wallet-series")


@pytest.fixture
def series_pass(event_series: EventSeries) -> SeriesPass:
    """Series pass product for wallet tests."""
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Wallet Season Pass",
        price=Decimal("60.00"),
        pro_rata_discount=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


def _covered_event(
    organization: Organization,
    event_series: EventSeries,
    series_pass: SeriesPass,
    name: str,
    slug: str,
    start_delta: timedelta,
) -> Event:
    """Create an OPEN covered event with a linked tier."""
    now = timezone.now()
    covered = Event.objects.create(
        organization=organization,
        event_series=event_series,
        name=name,
        slug=slug,
        start=now + start_delta,
        end=now + start_delta + timedelta(hours=2),
        requires_ticket=True,
        status=Event.EventStatus.OPEN,
    )
    tier = TicketTier.objects.create(
        event=covered, name=f"{name} Tier", price=10, currency="EUR", payment_method=TicketTier.PaymentMethod.FREE
    )
    SeriesPassTierLink.objects.create(series_pass=series_pass, event=covered, tier=tier)
    return covered


@pytest.fixture
def covered_events(organization: Organization, event_series: EventSeries, series_pass: SeriesPass) -> list[Event]:
    """Two future covered events (7 and 14 days out)."""
    return [
        _covered_event(organization, event_series, series_pass, "Covered One", "covered-one", timedelta(days=7)),
        _covered_event(organization, event_series, series_pass, "Covered Two", "covered-two", timedelta(days=14)),
    ]


@pytest.fixture
def held_pass(series_pass: SeriesPass, member_user: RevelUser, covered_events: list[Event]) -> HeldSeriesPass:
    """An active held series pass covering two future events."""
    return HeldSeriesPass.objects.create(
        series_pass=series_pass,
        user=member_user,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("50.00"),
    )


class TestBuildSeriesPassData:
    """Tests for _build_series_pass_data."""

    def test_builds_pass_data_from_held_pass(
        self,
        held_pass: HeldSeriesPass,
        covered_events: list[Event],
        mock_signer: MagicMock,
    ) -> None:
        """Should derive PassData fields from the pass and its covered events."""
        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_series_pass_data(held_pass)

        assert data.serial_number == str(held_pass.id)
        assert data.barcode_message == f"series:{held_pass.id}"
        assert data.organization_name == held_pass.series_pass.event_series.organization.name
        assert data.event_name == held_pass.series_pass.name
        assert data.description == f"Series Pass for {held_pass.series_pass.event_series.name}"
        assert data.ticket_tier == "Series Pass"
        assert data.ticket_price == "EUR 50.00"
        # Soonest upcoming event drives start/relevant_date
        assert data.event_start == covered_events[0].start
        assert data.relevant_date == covered_events[0].start
        # Latest-ending event drives end/expiration
        assert data.event_end == covered_events[1].end
        assert data.expiration_date == covered_events[1].end + PASS_EXPIRATION_GRACE_PERIOD

    def test_uses_next_upcoming_event_when_first_has_passed(
        self,
        organization: Organization,
        event_series: EventSeries,
        series_pass: SeriesPass,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """Should skip already-ended events when picking the representative event."""
        past = _covered_event(organization, event_series, series_pass, "Past Show", "past-show", timedelta(days=-7))
        upcoming = _covered_event(organization, event_series, series_pass, "Next Show", "next-show", timedelta(days=3))
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=Decimal("30"),
        )

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_series_pass_data(held)

        assert data.event_start == upcoming.start
        assert data.event_end == max(past.end, upcoming.end)

    def test_falls_back_to_latest_past_event_when_all_have_ended(
        self,
        organization: Organization,
        event_series: EventSeries,
        series_pass: SeriesPass,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """Should fall back to the most recent past event once the series is over."""
        _covered_event(organization, event_series, series_pass, "Old One", "old-one", timedelta(days=-14))
        latest = _covered_event(organization, event_series, series_pass, "Old Two", "old-two", timedelta(days=-7))
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=Decimal("30"),
        )

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_series_pass_data(held)

        assert data.event_start == latest.start
        assert data.event_end == latest.end

    def test_handles_pass_with_no_covered_events(
        self,
        series_pass: SeriesPass,
        member_user: RevelUser,
        mock_signer: MagicMock,
    ) -> None:
        """Defensive: a pass with no tier links still builds valid PassData."""
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass,
            user=member_user,
            status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
            price_paid=Decimal("0"),
        )

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_series_pass_data(held)

        assert data.event_start == held.created_at
        assert data.event_end == held.created_at
        assert data.address is None
        assert data.venue_name is None
        assert data.ticket_price == "Free"

    def test_uses_venue_of_representative_event(
        self,
        organization: Organization,
        held_pass: HeldSeriesPass,
        covered_events: list[Event],
        mock_signer: MagicMock,
    ) -> None:
        """Should surface the representative (soonest upcoming) event's venue."""
        from events.models.venue import Venue

        venue = Venue.objects.create(organization=organization, name="Series Venue", address="1 Series Street")
        covered_events[0].venue = venue
        covered_events[0].save()

        generator = ApplePassGenerator(signer=mock_signer)
        data = generator._build_series_pass_data(held_pass)

        assert data.venue_name == "Series Venue"
        assert data.address == "1 Series Street"


class TestGenerateSeriesPass:
    """Tests for generate_series_pass."""

    def test_generates_complete_pkpass(
        self,
        settings: t.Any,
        held_pass: HeldSeriesPass,
        mock_signer: MagicMock,
    ) -> None:
        """Should generate a complete .pkpass archive with the series barcode."""
        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)
        pkpass = generator.generate_series_pass(held_pass)

        with zipfile.ZipFile(io.BytesIO(pkpass), "r") as zf:
            namelist = zf.namelist()
            assert "pass.json" in namelist
            assert "manifest.json" in namelist
            assert "signature" in namelist
            assert "icon.png" in namelist
            assert "logo.png" in namelist

            pass_dict = json.loads(zf.read("pass.json"))
            assert pass_dict["serialNumber"] == str(held_pass.id)
            assert pass_dict["barcodes"][0]["message"] == f"series:{held_pass.id}"

    def test_raises_generator_error_on_failure(
        self,
        settings: t.Any,
        held_pass: HeldSeriesPass,
        mock_signer: MagicMock,
    ) -> None:
        """Should wrap unexpected failures in ApplePassGeneratorError."""
        from unittest.mock import patch

        from wallet.apple.generator import ApplePassGeneratorError

        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        generator = ApplePassGenerator(signer=mock_signer)

        with patch.object(generator, "_build_series_pass_data", side_effect=Exception("Build error")):
            with pytest.raises(ApplePassGeneratorError, match="Failed to generate pass"):
                generator.generate_series_pass(held_pass)

    def test_propagates_signer_error(
        self,
        settings: t.Any,
        held_pass: HeldSeriesPass,
    ) -> None:
        """Should propagate ApplePassSignerError untouched."""
        from wallet.apple.signer import ApplePassSignerError

        settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test"
        settings.APPLE_WALLET_TEAM_ID = "TEAM123"

        failing_signer = MagicMock()
        failing_signer.create_manifest.side_effect = ApplePassSignerError("Signing failed")

        generator = ApplePassGenerator(signer=failing_signer)

        with pytest.raises(ApplePassSignerError):
            generator.generate_series_pass(held_pass)
