"""Branding-specific tests for ticket PDF template and cancellation staff view."""

import typing as t

import pytest

from events.models.ticket import CancellationSource

pytestmark = pytest.mark.django_db


_CANCELLATION_CHANNELS: tuple[str, ...] = (
    "notifications/in_app/ticket_cancelled.md",
    "notifications/email/ticket_cancelled.html",
    "notifications/email/ticket_cancelled.txt",
    "notifications/telegram/ticket_cancelled.md",
)


class TestTicketPdfBranding:
    """Ticket PDF template carries the Bubble brand (2026 rebrand): brand palette, gradient wordmark, stub layout."""

    def _render_ticket_html(self, **overrides: t.Any) -> str:
        """Render the ticket HTML template with a minimal but sufficient context."""
        from django.conf import settings
        from django.template.loader import render_to_string

        ctx: dict[str, t.Any] = {
            # Brand assets injected by create_ticket_pdf
            "font_dir": str(settings.BASE_DIR / "fonts"),
            "brand_mark": str(settings.BASE_DIR / "assets" / "brand" / "revel-mark.svg"),
            "cover_art_url": None,
            # Event / ticket fields
            "event_name": "Test Event",
            "organization_name": "Test Org",
            "user_display_name": "Test User",
            "guest_name": "Test User",
            "tier_name": "General Admission",
            "start_datetime": "Saturday, July 5, 2025 at 8:00 PM CEST",
            "address": "Vienna, Austria",
            "qr_code_base64": "iVBORw0KGgo=",
            "ticket_id": "00000000-0000-0000-0000-000000000001",
            "ticket_id_short": "00000000",
            "venue_name": None,
            "sector_name": None,
            "seat_label": None,
            "seat_row": None,
            "seat_number": None,
        }
        ctx.update(overrides)
        return render_to_string("events/ticket.html", ctx)

    def test_ticket_html_contains_nata_sans(self) -> None:
        """Ticket template must declare Nata Sans as the body font."""
        html = self._render_ticket_html()
        assert "Nata Sans" in html, "Missing 'Nata Sans' in ticket HTML"

    def test_ticket_html_contains_powered_by_wordmark(self) -> None:
        """Ticket template must carry the 'Powered by revel.' footer lockup in <body>.

        WeasyPrint suppresses anything in <head>, so the lockup must render in <body>.
        """
        html = self._render_ticket_html()
        body_part = html.split("<body", 1)[1]
        assert "Powered by" in body_part, "Missing 'Powered by' in ticket HTML body"

    def test_ticket_html_contains_brand_mark_in_body(self) -> None:
        """Ticket template must reference revel-mark.svg in <body> for the footer lockup."""
        html = self._render_ticket_html()
        body_part = html.split("<body", 1)[1]
        assert "revel-mark.svg" in body_part, "Missing revel-mark.svg reference in ticket HTML body"

    def test_ticket_html_wordmark_gradient_endpoints(self) -> None:
        """The 'revel' wordmark must span the brand gradient: Hearty Purple → Light Crimson."""
        html = self._render_ticket_html()
        assert "#8C3CDD" in html, "Wordmark gradient start #8C3CDD (Hearty Purple) missing"
        assert "#E6332A" in html, "Wordmark gradient end #E6332A (Light Crimson) missing"

    def test_ticket_html_uses_brand_palette(self) -> None:
        """Ticket chrome must use the Bubble palette: lavender paper and ink."""
        html = self._render_ticket_html()
        assert "#F3EFFA" in html, "Lavender paper #F3EFFA missing from ticket HTML"
        assert "#0D1E1C" in html, "Ink #0D1E1C missing from ticket HTML"

    def test_ticket_html_has_no_legacy_accent_667eea(self) -> None:
        """Ticket template must not contain the legacy indigo accent #667eea."""
        html = self._render_ticket_html()
        assert "#667eea" not in html, "Legacy accent #667eea still present in ticket HTML"

    def test_ticket_html_has_no_uuid_derived_colors(self) -> None:
        """Per-event UUID-derived colours are gone: no template placeholders may leak unrendered."""
        html = self._render_ticket_html()
        assert "primary_color" not in html
        assert "hsl(" not in html, "UUID-derived hsl() colour still present in ticket HTML"

    def test_ticket_html_seat_renders_in_stub(self) -> None:
        """A seated ticket shows seat, row, and sector prominently in the stub."""
        html = self._render_ticket_html(
            venue_name="Revel Concert Hall", sector_name="Balcony", seat_label="C8", seat_row="C", seat_number=8
        )
        assert "Seat C8" in html, "Seat label missing from stub"
        assert "Row C" in html, "Seat row missing from stub"
        assert "Balcony" in html, "Sector missing from ticket"

    def test_ticket_html_sector_fallback_without_seat(self) -> None:
        """A sector-only ticket (GA within a sector) shows the sector in the stub."""
        html = self._render_ticket_html(venue_name="Revel Concert Hall", sector_name="Standing Room")
        assert "Standing Room" in html
        assert "Seat " not in html

    def test_ticket_html_tier_fallback_without_seating(self) -> None:
        """With no seat or sector, the stub falls back to the tier name."""
        html = self._render_ticket_html()
        assert html.count("General Admission") >= 2, "Tier name fallback missing from stub"


class TestTicketCancelledStaffTemplateBranching:
    """Staff/owner audience must not see holder-addressed phrasing (issue: organizer received "you cancelled...")."""

    def _render(
        self,
        template: str,
        *,
        source: str,
        holder_name: str = "Alice Holder",
        holder_email: str = "alice@example.com",
        event_name: str = "Test Fest",
    ) -> str:
        from django.template.loader import render_to_string

        return render_to_string(
            template,
            {
                "user": {"display_name": "Org Owner"},
                "context": {
                    "event_name": event_name,
                    "event_start_formatted": "TBD",
                    "event_location": "",
                    "tier_name": "GA",
                    "ticket_id": "abc",
                    "event_url": "https://example.com",
                    "cancellation_source": source,
                    "cancellation_reason": "",
                    "ticket_holder_name": holder_name,
                    "ticket_holder_email": holder_email,
                },
            },
        )

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_user_source_uses_third_person_for_staff(self, template: str) -> None:
        """When holder cancels, staff sees '<holder> cancelled their ticket' — never 'You cancelled'."""
        rendered = self._render(template, source=CancellationSource.USER.value)
        assert "Alice Holder" in rendered
        assert "cancelled their ticket" in rendered
        assert "You cancelled your ticket" not in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_stripe_dashboard_source_mentions_holder_for_staff(self, template: str) -> None:
        rendered = self._render(template, source=CancellationSource.STRIPE_DASHBOARD.value)
        assert "Alice Holder" in rendered
        assert "Stripe dashboard" in rendered
        assert "Your ticket" not in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_organizer_source_addresses_staff_in_third_person(self, template: str) -> None:
        rendered = self._render(template, source=CancellationSource.ORGANIZER.value)
        assert "Alice Holder" in rendered
        assert "has been cancelled" in rendered
        assert "Your ticket" not in rendered
        assert "You cancelled" not in rendered

    @pytest.mark.parametrize("template", _CANCELLATION_CHANNELS)
    def test_staff_view_includes_holder_email(self, template: str) -> None:
        """Staff need the holder's email to identify the user; ensure it's surfaced."""
        rendered = self._render(template, source=CancellationSource.USER.value)
        assert "alice@example.com" in rendered
