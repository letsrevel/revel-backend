"""Tests for events.service.series_pass_file_service.

Mirrors ``events/tests/test_ticket_file_service.py``'s patterns for
compute_content_hash, is_cache_valid, get_or_generate_pass_pdf/pkpass, and
_persist_and_update, plus a context test for ``create_series_pass_pdf``
(mirroring ``test_create_ticket_pdf_context_data`` in ``test_utils.py``).
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Event,
    EventSeries,
    HeldSeriesPass,
    Organization,
    SeriesPass,
    SeriesPassTierLink,
    TicketTier,
)
from events.service import series_pass_file_service

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Series pass holder."""
    return revel_user_factory()


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Organization for series pass file service tests."""
    return Organization.objects.create(name="FS Series Org", slug="fs-series-org", owner=owner)


@pytest.fixture
def series(org: Organization) -> EventSeries:
    """EventSeries for series pass file service tests."""
    return EventSeries.objects.create(organization=org, name="FS Series", slug="fs-series")


@pytest.fixture
def fs_series_pass(series: EventSeries) -> SeriesPass:
    """SeriesPass for series pass file service tests."""
    return SeriesPass.objects.create(
        event_series=series,
        name="FS Season Pass",
        price=Decimal("50.00"),
        pro_rata_discount=Decimal("10.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def covered_event_1(org: Organization, series: EventSeries) -> Event:
    """First covered event, further out in the future."""
    now = timezone.now()
    return Event.objects.create(
        organization=org,
        event_series=series,
        name="FS Covered Event 1",
        slug="fs-covered-event-1",
        start=now + timedelta(days=7),
        end=now + timedelta(days=7, hours=2),
        requires_ticket=True,
        status=Event.EventStatus.OPEN,
    )


@pytest.fixture
def covered_event_2(org: Organization, series: EventSeries) -> Event:
    """Second covered event, latest to end."""
    now = timezone.now()
    return Event.objects.create(
        organization=org,
        event_series=series,
        name="FS Covered Event 2",
        slug="fs-covered-event-2",
        start=now + timedelta(days=14),
        end=now + timedelta(days=14, hours=2),
        requires_ticket=True,
        status=Event.EventStatus.OPEN,
    )


@pytest.fixture
def tier_1(covered_event_1: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=covered_event_1, name="FS Tier 1", price=10, currency="EUR", payment_method=TicketTier.PaymentMethod.FREE
    )


@pytest.fixture
def tier_2(covered_event_2: Event) -> TicketTier:
    return TicketTier.objects.create(
        event=covered_event_2, name="FS Tier 2", price=10, currency="EUR", payment_method=TicketTier.PaymentMethod.FREE
    )


@pytest.fixture
def tier_links(
    fs_series_pass: SeriesPass,
    covered_event_1: Event,
    tier_1: TicketTier,
    covered_event_2: Event,
    tier_2: TicketTier,
) -> list[SeriesPassTierLink]:
    link1 = SeriesPassTierLink.objects.create(series_pass=fs_series_pass, event=covered_event_1, tier=tier_1)
    link2 = SeriesPassTierLink.objects.create(series_pass=fs_series_pass, event=covered_event_2, tier=tier_2)
    return [link1, link2]


@pytest.fixture
def held_pass(fs_series_pass: SeriesPass, owner: RevelUser, tier_links: list[SeriesPassTierLink]) -> HeldSeriesPass:
    """Active held series pass covering two future events."""
    return HeldSeriesPass.objects.create(
        series_pass=fs_series_pass,
        user=owner,
        status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE,
        price_paid=Decimal("40.00"),
    )


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


class TestComputeContentHash:
    """Tests for series_pass_file_service.compute_content_hash."""

    def test_returns_deterministic_hex_string(self, held_pass: HeldSeriesPass) -> None:
        """Hash should be deterministic for the same held pass state."""
        hash1 = series_pass_file_service.compute_content_hash(held_pass)
        hash2 = series_pass_file_service.compute_content_hash(held_pass)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex digest

    def test_changes_when_held_pass_updated_at_changes(self, held_pass: HeldSeriesPass) -> None:
        """Hash should change when held_pass.updated_at changes."""
        hash_before = series_pass_file_service.compute_content_hash(held_pass)

        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(updated_at=held_pass.updated_at + timedelta(seconds=1))
        held_pass.refresh_from_db()

        hash_after = series_pass_file_service.compute_content_hash(held_pass)
        assert hash_before != hash_after

    def test_changes_when_series_pass_updated_at_changes(
        self, held_pass: HeldSeriesPass, fs_series_pass: SeriesPass
    ) -> None:
        """Hash should change when series_pass.updated_at changes."""
        hash_before = series_pass_file_service.compute_content_hash(held_pass)

        SeriesPass.objects.filter(pk=fs_series_pass.pk).update(
            updated_at=fs_series_pass.updated_at + timedelta(seconds=1)
        )
        held_pass.series_pass.refresh_from_db()

        hash_after = series_pass_file_service.compute_content_hash(held_pass)
        assert hash_before != hash_after

    def test_changes_when_covered_event_updated_at_changes(
        self, held_pass: HeldSeriesPass, covered_event_1: Event
    ) -> None:
        """Hash should change when a covered event's updated_at changes."""
        hash_before = series_pass_file_service.compute_content_hash(held_pass)

        Event.objects.filter(pk=covered_event_1.pk).update(updated_at=covered_event_1.updated_at + timedelta(seconds=1))

        hash_after = series_pass_file_service.compute_content_hash(held_pass)
        assert hash_before != hash_after

    def test_changes_when_event_added(
        self,
        held_pass: HeldSeriesPass,
        fs_series_pass: SeriesPass,
        org: Organization,
        series: EventSeries,
    ) -> None:
        """Hash should change when a new event is added to the series pass coverage."""
        hash_before = series_pass_file_service.compute_content_hash(held_pass)

        now = timezone.now()
        new_event = Event.objects.create(
            organization=org,
            event_series=series,
            name="FS Newly Added Event",
            slug="fs-newly-added-event",
            start=now + timedelta(days=21),
            end=now + timedelta(days=21, hours=2),
            requires_ticket=True,
            status=Event.EventStatus.OPEN,
        )
        new_tier = TicketTier.objects.create(
            event=new_event, name="FS New Tier", price=10, currency="EUR", payment_method=TicketTier.PaymentMethod.FREE
        )
        SeriesPassTierLink.objects.create(series_pass=fs_series_pass, event=new_event, tier=new_tier)

        hash_after = series_pass_file_service.compute_content_hash(held_pass)
        assert hash_before != hash_after


# ---------------------------------------------------------------------------
# is_cache_valid
# ---------------------------------------------------------------------------


class TestIsCacheValid:
    """Tests for series_pass_file_service.is_cache_valid."""

    def test_returns_false_when_no_hash(self, held_pass: HeldSeriesPass) -> None:
        assert held_pass.file_content_hash is None
        assert series_pass_file_service.is_cache_valid(held_pass) is False

    def test_returns_false_when_hash_mismatch(self, held_pass: HeldSeriesPass) -> None:
        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(file_content_hash="stale_hash")
        held_pass.refresh_from_db()

        assert series_pass_file_service.is_cache_valid(held_pass) is False

    def test_returns_true_when_hash_matches(self, held_pass: HeldSeriesPass) -> None:
        current_hash = series_pass_file_service.compute_content_hash(held_pass)
        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(file_content_hash=current_hash)
        held_pass.refresh_from_db()

        assert series_pass_file_service.is_cache_valid(held_pass) is True


# ---------------------------------------------------------------------------
# create_series_pass_pdf (events.utils) — content/context
# ---------------------------------------------------------------------------


class TestCreateSeriesPassPdf:
    """Tests for events.utils.create_series_pass_pdf: QR payload and context content."""

    @patch("qrcode.QRCode")
    @patch("weasyprint.HTML")
    @patch("events.utils.render_to_string")
    def test_qr_payload_is_series_prefixed_uuid(
        self,
        mock_render: MagicMock,
        mock_html: MagicMock,
        mock_qr: MagicMock,
        held_pass: HeldSeriesPass,
    ) -> None:
        """QR payload must be exactly ``series:<held_pass.id>`` — the check-in contract."""
        mock_qr_instance = MagicMock()
        mock_qr.return_value = mock_qr_instance
        mock_qr_instance.make_image.return_value = MagicMock()
        mock_html_instance = MagicMock()
        mock_html.return_value = mock_html_instance
        mock_html_instance.write_pdf.return_value = b"%PDF-fake"
        mock_render.return_value = "<html></html>"

        from events.utils import create_series_pass_pdf

        create_series_pass_pdf(held_pass)

        mock_qr_instance.add_data.assert_called_once_with(f"series:{held_pass.id}")

    @patch("qrcode.QRCode")
    @patch("weasyprint.HTML")
    @patch("events.utils.render_to_string")
    def test_context_contains_series_name_and_covered_events(
        self,
        mock_render: MagicMock,
        mock_html: MagicMock,
        mock_qr: MagicMock,
        held_pass: HeldSeriesPass,
        covered_event_1: Event,
        covered_event_2: Event,
    ) -> None:
        """Rendered context must include the series name and every covered event's name."""
        mock_qr_instance = MagicMock()
        mock_qr.return_value = mock_qr_instance
        mock_qr_instance.make_image.return_value = MagicMock()
        mock_html_instance = MagicMock()
        mock_html.return_value = mock_html_instance
        mock_html_instance.write_pdf.return_value = b"%PDF-fake"
        mock_render.return_value = "<html></html>"

        from events.utils import create_series_pass_pdf

        result = create_series_pass_pdf(held_pass)

        assert result == b"%PDF-fake"
        mock_render.assert_called_once()
        args, kwargs = mock_render.call_args
        assert args[0] == "events/series_pass.html"

        context = kwargs["context"]
        assert context["series_name"] == held_pass.series_pass.event_series.name
        assert context["pass_name"] == held_pass.series_pass.name
        assert context["pass_id"] == str(held_pass.id)

        covered_event_names = {covered_event["name"] for covered_event in context["covered_events"]}
        assert covered_event_names == {covered_event_1.name, covered_event_2.name}


# ---------------------------------------------------------------------------
# get_or_generate_pass_pdf
# ---------------------------------------------------------------------------


class TestGetOrGeneratePassPdf:
    """Tests for series_pass_file_service.get_or_generate_pass_pdf."""

    @patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-fresh")
    def test_generates_pdf_when_no_cache(self, mock_create_pdf: MagicMock, held_pass: HeldSeriesPass) -> None:
        result = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        assert result == b"%PDF-fresh"
        mock_create_pdf.assert_called_once_with(held_pass)

        held_pass.refresh_from_db()
        assert held_pass.pdf_file
        assert held_pass.file_content_hash is not None

    @patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-fresh")
    def test_returns_cached_pdf_when_valid(self, mock_create_pdf: MagicMock, held_pass: HeldSeriesPass) -> None:
        series_pass_file_service.get_or_generate_pass_pdf(held_pass)
        held_pass.refresh_from_db()

        mock_create_pdf.reset_mock()

        result = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        assert result == b"%PDF-fresh"
        mock_create_pdf.assert_not_called()

    @patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-regen")
    def test_regenerates_when_cache_stale(self, mock_create_pdf: MagicMock, held_pass: HeldSeriesPass) -> None:
        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(file_content_hash="stale_hash")
        held_pass.refresh_from_db()

        result = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        assert result == b"%PDF-regen"
        mock_create_pdf.assert_called_once()

    @patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-regen")
    def test_regenerates_when_event_added(
        self,
        mock_create_pdf: MagicMock,
        held_pass: HeldSeriesPass,
        fs_series_pass: SeriesPass,
        org: Organization,
        series: EventSeries,
    ) -> None:
        """Adding a newly-covered event must invalidate the cache, not just field edits."""
        series_pass_file_service.get_or_generate_pass_pdf(held_pass)
        held_pass.refresh_from_db()
        mock_create_pdf.reset_mock()

        now = timezone.now()
        new_event = Event.objects.create(
            organization=org,
            event_series=series,
            name="FS Extended Event",
            slug="fs-extended-event",
            start=now + timedelta(days=28),
            end=now + timedelta(days=28, hours=2),
            requires_ticket=True,
            status=Event.EventStatus.OPEN,
        )
        new_tier = TicketTier.objects.create(
            event=new_event,
            name="FS Extended Tier",
            price=10,
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        SeriesPassTierLink.objects.create(series_pass=fs_series_pass, event=new_event, tier=new_tier)

        result = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        assert result == b"%PDF-regen"
        mock_create_pdf.assert_called_once()

    @patch("events.utils.create_series_pass_pdf", return_value=b"%PDF-fallback")
    def test_regenerates_on_storage_read_failure(self, mock_create_pdf: MagicMock, held_pass: HeldSeriesPass) -> None:
        series_pass_file_service._persist_and_update(held_pass, pdf_bytes=b"%PDF-original")
        held_pass.refresh_from_db()

        with patch.object(held_pass.pdf_file, "open", side_effect=OSError("disk error")):
            result = series_pass_file_service.get_or_generate_pass_pdf(held_pass)

        assert result == b"%PDF-fallback"
        mock_create_pdf.assert_called_once()


# ---------------------------------------------------------------------------
# get_or_generate_pass_pkpass
# ---------------------------------------------------------------------------


class TestGetOrGeneratePassPkpass:
    """Tests for series_pass_file_service.get_or_generate_pass_pkpass."""

    @patch("events.service.series_pass_file_service.get_apple_pass_generator")
    def test_generates_pkpass_when_no_cache(self, mock_get_generator: MagicMock, held_pass: HeldSeriesPass) -> None:
        mock_gen = MagicMock()
        mock_gen.generate_series_pass.return_value = b"PK-fresh"
        mock_get_generator.return_value = mock_gen

        result = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        assert result == b"PK-fresh"
        mock_gen.generate_series_pass.assert_called_once_with(held_pass)

        held_pass.refresh_from_db()
        assert held_pass.pkpass_file
        assert held_pass.file_content_hash is not None

    @patch("events.service.series_pass_file_service.get_apple_pass_generator")
    def test_returns_cached_pkpass_when_valid(self, mock_get_generator: MagicMock, held_pass: HeldSeriesPass) -> None:
        mock_gen = MagicMock()
        mock_gen.generate_series_pass.return_value = b"PK-fresh"
        mock_get_generator.return_value = mock_gen

        series_pass_file_service.get_or_generate_pass_pkpass(held_pass)
        held_pass.refresh_from_db()

        mock_gen.generate_series_pass.reset_mock()

        result = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        assert result == b"PK-fresh"
        mock_gen.generate_series_pass.assert_not_called()

    @patch("events.service.series_pass_file_service.get_apple_pass_generator")
    def test_regenerates_when_cache_stale(self, mock_get_generator: MagicMock, held_pass: HeldSeriesPass) -> None:
        mock_gen = MagicMock()
        mock_gen.generate_series_pass.return_value = b"PK-regen"
        mock_get_generator.return_value = mock_gen

        HeldSeriesPass.objects.filter(pk=held_pass.pk).update(file_content_hash="stale_hash")
        held_pass.refresh_from_db()

        result = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        assert result == b"PK-regen"
        mock_gen.generate_series_pass.assert_called_once()

    @patch("events.service.series_pass_file_service.get_apple_pass_generator")
    def test_regenerates_on_storage_read_failure(
        self, mock_get_generator: MagicMock, held_pass: HeldSeriesPass
    ) -> None:
        mock_gen = MagicMock()
        mock_gen.generate_series_pass.return_value = b"PK-fallback"
        mock_get_generator.return_value = mock_gen

        series_pass_file_service._persist_and_update(held_pass, pkpass_bytes=b"PK-original")
        held_pass.refresh_from_db()

        with patch.object(held_pass.pkpass_file, "open", side_effect=OSError("disk error")):
            result = series_pass_file_service.get_or_generate_pass_pkpass(held_pass)

        assert result == b"PK-fallback"
        mock_gen.generate_series_pass.assert_called_once()


# ---------------------------------------------------------------------------
# _persist_and_update
# ---------------------------------------------------------------------------


class TestPersistAndUpdate:
    """Tests for series_pass_file_service._persist_and_update."""

    def test_saves_pdf_and_updates_db(self, held_pass: HeldSeriesPass) -> None:
        series_pass_file_service._persist_and_update(held_pass, pdf_bytes=b"%PDF-persist")

        held_pass.refresh_from_db()
        assert held_pass.pdf_file
        assert "series_pass_" in held_pass.pdf_file.name
        assert held_pass.file_content_hash is not None

    def test_saves_pkpass_and_updates_db(self, held_pass: HeldSeriesPass) -> None:
        series_pass_file_service._persist_and_update(held_pass, pkpass_bytes=b"PK-persist")

        held_pass.refresh_from_db()
        assert held_pass.pkpass_file
        assert "series_pass_" in held_pass.pkpass_file.name
        assert held_pass.file_content_hash is not None

    def test_updated_at_not_changed_by_persist(self, held_pass: HeldSeriesPass) -> None:
        """QuerySet.update() must not trigger auto_now, or the stored hash would be stale immediately."""
        original_updated_at = held_pass.updated_at

        series_pass_file_service._persist_and_update(held_pass, pdf_bytes=b"%PDF-noauto")

        held_pass.refresh_from_db()
        assert held_pass.updated_at == original_updated_at

    def test_saves_both_pdf_and_pkpass(self, held_pass: HeldSeriesPass) -> None:
        series_pass_file_service._persist_and_update(held_pass, pdf_bytes=b"%PDF-dual", pkpass_bytes=b"PK-dual")

        held_pass.refresh_from_db()
        assert held_pass.pdf_file
        assert held_pass.pkpass_file
        assert held_pass.file_content_hash is not None

    def test_logs_warning_on_storage_failure(self, held_pass: HeldSeriesPass) -> None:
        with patch(
            "events.service.series_pass_file_service.ContentFile",
            side_effect=OSError("storage broke"),
        ):
            series_pass_file_service._persist_and_update(held_pass, pdf_bytes=b"fail")

        held_pass.refresh_from_db()
        assert held_pass.file_content_hash is None

    def test_noop_when_no_bytes_provided(self, held_pass: HeldSeriesPass) -> None:
        original_hash = held_pass.file_content_hash

        series_pass_file_service._persist_and_update(held_pass)

        held_pass.refresh_from_db()
        assert held_pass.file_content_hash == original_hash
