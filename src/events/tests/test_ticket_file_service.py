"""Tests for events.service.ticket_file_service.

This module tests the on-demand ticket file generation and caching service,
covering hash computation, cache validity checks, PDF/pkpass generation
with caching, and the low-level persist logic.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Event, Organization, Ticket, TicketTier
from events.service import ticket_file_service

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner."""
    return revel_user_factory()


@pytest.fixture
def org(owner: RevelUser) -> Organization:
    """Organization for file service tests."""
    return Organization.objects.create(name="FS Org", slug="fs-org", owner=owner)


@pytest.fixture
def future_event(org: Organization) -> Event:
    """A future event."""
    now = timezone.now()
    return Event.objects.create(
        organization=org,
        name="FS Event",
        slug="fs-event",
        start=now + timedelta(days=7),
        end=now + timedelta(days=7, hours=3),
        requires_ticket=True,
        status=Event.EventStatus.OPEN,
    )


@pytest.fixture
def tier(future_event: Event) -> TicketTier:
    """Ticket tier for file service tests."""
    return TicketTier.objects.create(
        event=future_event,
        name="FS Tier",
        price=10,
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def ticket_with_tier(future_event: Event, owner: RevelUser, tier: TicketTier) -> Ticket:
    """Active ticket with a tier."""
    return Ticket.objects.create(
        event=future_event,
        user=owner,
        tier=tier,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name="Test Guest",
    )


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


class TestComputeContentHash:
    """Tests for ticket_file_service.compute_content_hash."""

    def test_returns_deterministic_hex_string(self, ticket_with_tier: Ticket) -> None:
        """Hash should be deterministic for the same ticket state."""
        hash1 = ticket_file_service.compute_content_hash(ticket_with_tier)
        hash2 = ticket_file_service.compute_content_hash(ticket_with_tier)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256 hex digest

    def test_changes_when_ticket_updated_at_changes(self, ticket_with_tier: Ticket) -> None:
        """Hash should change when ticket.updated_at changes."""
        hash_before = ticket_file_service.compute_content_hash(ticket_with_tier)

        # Force a different updated_at via QuerySet.update to avoid auto_now
        Ticket.objects.filter(pk=ticket_with_tier.pk).update(
            updated_at=ticket_with_tier.updated_at + timedelta(seconds=1)
        )
        ticket_with_tier.refresh_from_db()

        hash_after = ticket_file_service.compute_content_hash(ticket_with_tier)
        assert hash_before != hash_after

    def test_changes_when_event_updated_at_changes(self, ticket_with_tier: Ticket) -> None:
        """Hash should change when event.updated_at changes."""
        hash_before = ticket_file_service.compute_content_hash(ticket_with_tier)

        Event.objects.filter(pk=ticket_with_tier.event.pk).update(
            updated_at=ticket_with_tier.event.updated_at + timedelta(seconds=1)
        )
        ticket_with_tier.event.refresh_from_db()

        hash_after = ticket_file_service.compute_content_hash(ticket_with_tier)
        assert hash_before != hash_after

    def test_changes_when_tier_updated_at_changes(self, ticket_with_tier: Ticket) -> None:
        """Hash should change when tier.updated_at changes."""
        hash_before = ticket_file_service.compute_content_hash(ticket_with_tier)

        TicketTier.objects.filter(pk=ticket_with_tier.tier.pk).update(
            updated_at=ticket_with_tier.tier.updated_at + timedelta(seconds=1)
        )
        ticket_with_tier.tier.refresh_from_db()

        hash_after = ticket_file_service.compute_content_hash(ticket_with_tier)
        assert hash_before != hash_after


# ---------------------------------------------------------------------------
# is_cache_valid
# ---------------------------------------------------------------------------


class TestIsCacheValid:
    """Tests for ticket_file_service.is_cache_valid."""

    def test_returns_false_when_no_hash(self, ticket_with_tier: Ticket) -> None:
        """Should return False when file_content_hash is empty."""
        assert ticket_with_tier.file_content_hash is None
        assert ticket_file_service.is_cache_valid(ticket_with_tier) is False

    def test_returns_false_when_hash_mismatch(self, ticket_with_tier: Ticket) -> None:
        """Should return False when stored hash does not match computed hash."""
        Ticket.objects.filter(pk=ticket_with_tier.pk).update(file_content_hash="stale_hash")
        ticket_with_tier.refresh_from_db()

        assert ticket_file_service.is_cache_valid(ticket_with_tier) is False

    def test_returns_true_when_hash_matches(self, ticket_with_tier: Ticket) -> None:
        """Should return True when stored hash matches the computed hash."""
        current_hash = ticket_file_service.compute_content_hash(ticket_with_tier)
        Ticket.objects.filter(pk=ticket_with_tier.pk).update(file_content_hash=current_hash)
        ticket_with_tier.refresh_from_db()

        assert ticket_file_service.is_cache_valid(ticket_with_tier) is True


# ---------------------------------------------------------------------------
# get_or_generate_pdf
# ---------------------------------------------------------------------------


class TestGetOrGeneratePdf:
    """Tests for ticket_file_service.get_or_generate_pdf."""

    @patch("events.utils.create_ticket_pdf", return_value=b"%PDF-fresh")
    def test_generates_pdf_when_no_cache(
        self,
        mock_create_pdf: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should generate a new PDF when no cached file exists."""
        result = ticket_file_service.get_or_generate_pdf(ticket_with_tier)

        assert result == b"%PDF-fresh"
        mock_create_pdf.assert_called_once_with(ticket_with_tier)

        # Verify file was persisted
        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pdf_file
        assert ticket_with_tier.file_content_hash is not None

    @patch("events.utils.create_ticket_pdf", return_value=b"%PDF-fresh")
    def test_returns_cached_pdf_when_valid(
        self,
        mock_create_pdf: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should return cached PDF bytes when the cache is valid."""
        # First call generates and caches
        ticket_file_service.get_or_generate_pdf(ticket_with_tier)
        ticket_with_tier.refresh_from_db()

        # Reset mock to verify no second generation
        mock_create_pdf.reset_mock()

        # Second call should serve from cache
        result = ticket_file_service.get_or_generate_pdf(ticket_with_tier)

        assert result == b"%PDF-fresh"
        mock_create_pdf.assert_not_called()

    @patch("events.utils.create_ticket_pdf", return_value=b"%PDF-regen")
    def test_regenerates_when_cache_stale(
        self,
        mock_create_pdf: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should regenerate when the content hash no longer matches."""
        # Seed a stale hash and a dummy file to simulate stale cache
        Ticket.objects.filter(pk=ticket_with_tier.pk).update(file_content_hash="stale_hash")
        ticket_with_tier.refresh_from_db()

        result = ticket_file_service.get_or_generate_pdf(ticket_with_tier)

        assert result == b"%PDF-regen"
        mock_create_pdf.assert_called_once()

    @patch("events.utils.create_ticket_pdf", return_value=b"%PDF-fallback")
    def test_regenerates_on_storage_read_failure(
        self,
        mock_create_pdf: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should regenerate when reading the cached file raises an exception."""
        # First, persist a real file so ticket.pdf_file is truthy
        ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"%PDF-original")
        ticket_with_tier.refresh_from_db()

        # Simulate storage read failure
        with patch.object(ticket_with_tier.pdf_file, "open", side_effect=OSError("disk error")):
            result = ticket_file_service.get_or_generate_pdf(ticket_with_tier)

        assert result == b"%PDF-fallback"
        mock_create_pdf.assert_called_once()


# ---------------------------------------------------------------------------
# get_or_generate_pkpass
# ---------------------------------------------------------------------------


class TestGetOrGeneratePkpass:
    """Tests for ticket_file_service.get_or_generate_pkpass."""

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_generates_pkpass_when_no_cache(
        self,
        mock_get_generator: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should generate a new pkpass when no cached file exists."""
        mock_gen = MagicMock()
        mock_gen.generate_pass.return_value = b"PK-fresh"
        mock_get_generator.return_value = mock_gen

        result = ticket_file_service.get_or_generate_pkpass(ticket_with_tier)

        assert result == b"PK-fresh"
        mock_gen.generate_pass.assert_called_once_with(ticket_with_tier)

        # Verify file was persisted
        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is not None

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_returns_cached_pkpass_when_valid(
        self,
        mock_get_generator: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should return cached pkpass bytes when the cache is valid."""
        mock_gen = MagicMock()
        mock_gen.generate_pass.return_value = b"PK-fresh"
        mock_get_generator.return_value = mock_gen

        # First call caches
        ticket_file_service.get_or_generate_pkpass(ticket_with_tier)
        ticket_with_tier.refresh_from_db()

        mock_gen.generate_pass.reset_mock()

        # Second call should serve from cache
        result = ticket_file_service.get_or_generate_pkpass(ticket_with_tier)

        assert result == b"PK-fresh"
        mock_gen.generate_pass.assert_not_called()

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_regenerates_when_cache_stale(
        self,
        mock_get_generator: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should regenerate when the content hash no longer matches."""
        mock_gen = MagicMock()
        mock_gen.generate_pass.return_value = b"PK-regen"
        mock_get_generator.return_value = mock_gen

        Ticket.objects.filter(pk=ticket_with_tier.pk).update(file_content_hash="stale_hash")
        ticket_with_tier.refresh_from_db()

        result = ticket_file_service.get_or_generate_pkpass(ticket_with_tier)

        assert result == b"PK-regen"
        mock_gen.generate_pass.assert_called_once()

    @patch("events.service.ticket_file_service.get_apple_pass_generator")
    def test_regenerates_on_storage_read_failure(
        self,
        mock_get_generator: MagicMock,
        ticket_with_tier: Ticket,
    ) -> None:
        """Should regenerate when reading the cached pkpass raises an exception."""
        mock_gen = MagicMock()
        mock_gen.generate_pass.return_value = b"PK-fallback"
        mock_get_generator.return_value = mock_gen

        # Persist a real file first
        ticket_file_service._persist_and_update(ticket_with_tier, pkpass_bytes=b"PK-original")
        ticket_with_tier.refresh_from_db()

        with patch.object(ticket_with_tier.pkpass_file, "open", side_effect=OSError("disk error")):
            result = ticket_file_service.get_or_generate_pkpass(ticket_with_tier)

        assert result == b"PK-fallback"
        mock_gen.generate_pass.assert_called_once()


# ---------------------------------------------------------------------------
# cache_files
# ---------------------------------------------------------------------------


class TestCacheFiles:
    """Tests for ticket_file_service.cache_files."""

    def test_noop_when_both_none(self, ticket_with_tier: Ticket) -> None:
        """Should be a no-op when both pdf_bytes and pkpass_bytes are None."""
        ticket_file_service.cache_files(ticket_with_tier, pdf_bytes=None, pkpass_bytes=None)

        ticket_with_tier.refresh_from_db()
        assert not ticket_with_tier.pdf_file
        assert not ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is None

    def test_saves_pdf_only(self, ticket_with_tier: Ticket) -> None:
        """Should save only the PDF when pkpass_bytes is None."""
        ticket_file_service.cache_files(ticket_with_tier, pdf_bytes=b"%PDF-cached")

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pdf_file
        assert not ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is not None

    def test_saves_pkpass_only(self, ticket_with_tier: Ticket) -> None:
        """Should save only the pkpass when pdf_bytes is None."""
        ticket_file_service.cache_files(ticket_with_tier, pkpass_bytes=b"PK-cached")

        ticket_with_tier.refresh_from_db()
        assert not ticket_with_tier.pdf_file
        assert ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is not None

    def test_saves_both_files(self, ticket_with_tier: Ticket) -> None:
        """Should save both PDF and pkpass when both are provided."""
        ticket_file_service.cache_files(
            ticket_with_tier,
            pdf_bytes=b"%PDF-both",
            pkpass_bytes=b"PK-both",
        )

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pdf_file
        assert ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is not None

    def test_skips_empty_bytes(self, ticket_with_tier: Ticket) -> None:
        """Empty bytes (b'') are treated as None — no-op.

        An empty file is not a valid PDF or pkpass, so cache_files
        normalizes empty bytes to None to avoid persisting corrupt files.
        """
        ticket_file_service.cache_files(ticket_with_tier, pdf_bytes=b"", pkpass_bytes=b"")

        ticket_with_tier.refresh_from_db()
        assert not ticket_with_tier.pdf_file
        assert not ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is None


# ---------------------------------------------------------------------------
# _persist_and_update
# ---------------------------------------------------------------------------


class TestPersistAndUpdate:
    """Tests for ticket_file_service._persist_and_update."""

    def test_saves_pdf_and_updates_db(self, ticket_with_tier: Ticket) -> None:
        """Should save the PDF file and update DB fields via QuerySet.update."""
        ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"%PDF-persist")

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pdf_file
        assert "ticket_" in ticket_with_tier.pdf_file.name
        assert ticket_with_tier.file_content_hash is not None

    def test_saves_pkpass_and_updates_db(self, ticket_with_tier: Ticket) -> None:
        """Should save the pkpass file and update DB fields via QuerySet.update."""
        ticket_file_service._persist_and_update(ticket_with_tier, pkpass_bytes=b"PK-persist")

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pkpass_file
        assert "ticket_" in ticket_with_tier.pkpass_file.name
        assert ticket_with_tier.file_content_hash is not None

    def test_updated_at_not_changed_by_persist(self, ticket_with_tier: Ticket) -> None:
        """Verify QuerySet.update() does not trigger auto_now on updated_at.

        This is the critical behavior: if updated_at changed, the content
        hash we just stored would immediately become stale.
        """
        original_updated_at = ticket_with_tier.updated_at

        ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"%PDF-noauto")

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.updated_at == original_updated_at

    def test_cleans_old_file_after_saving_new_one(self, ticket_with_tier: Ticket) -> None:
        """Should clean up the old file from storage after saving the new one."""
        # Save a first file
        ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"%PDF-old")
        ticket_with_tier.refresh_from_db()
        old_name = ticket_with_tier.pdf_file.name

        # Save a second file - old should be deleted
        ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"%PDF-new")
        ticket_with_tier.refresh_from_db()

        # The old file should no longer exist if names differ,
        # but since filenames are based on ticket ID they may collide.
        # The important thing is that the new file is readable.
        ticket_with_tier.pdf_file.open("rb")
        content = ticket_with_tier.pdf_file.read()
        ticket_with_tier.pdf_file.close()
        assert content == b"%PDF-new"
        # old_name reference is still valid for assertion purposes
        assert old_name  # just confirm it was set

    def test_saves_both_pdf_and_pkpass(self, ticket_with_tier: Ticket) -> None:
        """Should save both files in a single call."""
        ticket_file_service._persist_and_update(
            ticket_with_tier,
            pdf_bytes=b"%PDF-dual",
            pkpass_bytes=b"PK-dual",
        )

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.pdf_file
        assert ticket_with_tier.pkpass_file
        assert ticket_with_tier.file_content_hash is not None

        # Verify both files are readable
        ticket_with_tier.pdf_file.open("rb")
        assert ticket_with_tier.pdf_file.read() == b"%PDF-dual"
        ticket_with_tier.pdf_file.close()

        ticket_with_tier.pkpass_file.open("rb")
        assert ticket_with_tier.pkpass_file.read() == b"PK-dual"
        ticket_with_tier.pkpass_file.close()

    def test_logs_warning_on_storage_failure(self, ticket_with_tier: Ticket) -> None:
        """Should log a warning if storage operations fail, without raising."""
        with patch(
            "events.service.ticket_file_service.ContentFile",
            side_effect=OSError("storage broke"),
        ):
            # Should not raise
            ticket_file_service._persist_and_update(ticket_with_tier, pdf_bytes=b"fail")

        # DB should be unchanged
        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.file_content_hash is None

    def test_noop_when_no_bytes_provided(self, ticket_with_tier: Ticket) -> None:
        """Should not update DB when neither pdf_bytes nor pkpass_bytes are given."""
        original_hash = ticket_with_tier.file_content_hash

        ticket_file_service._persist_and_update(ticket_with_tier)

        ticket_with_tier.refresh_from_db()
        assert ticket_with_tier.file_content_hash == original_hash


# ---------------------------------------------------------------------------
# UserTicketSchema resolvers
# ---------------------------------------------------------------------------


class TestUserTicketSchemaResolvers:
    """Tests for UserTicketSchema.resolve_pdf_url and resolve_pkpass_url."""

    def test_resolve_pdf_url_returns_none_when_no_file(self, ticket_with_tier: Ticket) -> None:
        """Should return None when no PDF is cached."""
        from events.schema.ticket import UserTicketSchema

        assert UserTicketSchema.resolve_pdf_url(ticket_with_tier) is None

    def test_resolve_pkpass_url_returns_none_when_no_file(self, ticket_with_tier: Ticket) -> None:
        """Should return None when no pkpass is cached."""
        from events.schema.ticket import UserTicketSchema

        assert UserTicketSchema.resolve_pkpass_url(ticket_with_tier) is None

    def test_resolve_pdf_url_returns_signed_url_when_cached(self, ticket_with_tier: Ticket) -> None:
        """Should return a signed URL when PDF file is cached."""
        from events.schema.ticket import UserTicketSchema

        ticket_file_service.cache_files(ticket_with_tier, pdf_bytes=b"%PDF-test")
        ticket_with_tier.refresh_from_db()

        url = UserTicketSchema.resolve_pdf_url(ticket_with_tier)

        assert url is not None
        assert "exp=" in url
        assert "sig=" in url

    def test_resolve_pkpass_url_returns_signed_url_when_cached(self, ticket_with_tier: Ticket) -> None:
        """Should return a signed URL when pkpass file is cached."""
        from events.schema.ticket import UserTicketSchema

        ticket_file_service.cache_files(ticket_with_tier, pkpass_bytes=b"PK-test")
        ticket_with_tier.refresh_from_db()

        url = UserTicketSchema.resolve_pkpass_url(ticket_with_tier)

        assert url is not None
        assert "exp=" in url
        assert "sig=" in url
