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
    """Ticket PDF template must carry Let's Revel branding while remaining organiser-centric."""

    def _render_ticket_html(self) -> str:
        """Render the ticket HTML template with a minimal but sufficient context."""
        from django.conf import settings
        from django.template.loader import render_to_string

        ctx: dict[str, t.Any] = {
            # Brand assets injected by create_ticket_pdf
            "font_dir": str(settings.BASE_DIR / "fonts"),
            "brand_logo": str(settings.BASE_DIR / "assets" / "brand" / "revel-logo.png"),
            # Org-centric fields (kept as leads)
            # Use a distinctive colour so we can assert org colour flows through
            # to accents and is not replaced by a hard-coded brand purple.
            "logo_url": None,
            "cover_art_url": None,
            "logo_initials": "TE",
            "primary_color": "#123456",
            "secondary_color": "#654321",
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
        return render_to_string("events/ticket.html", ctx)

    def test_ticket_html_contains_nata_sans(self) -> None:
        """Ticket template must declare Nata Sans as the body font."""
        html = self._render_ticket_html()
        assert "Nata Sans" in html, "Missing 'Nata Sans' in ticket HTML"

    def test_ticket_html_contains_powered_by_wordmark(self) -> None:
        """Ticket template must carry a 'Powered by let's revel.' footer."""
        html = self._render_ticket_html()
        assert "Powered by let's revel." in html, "Missing 'Powered by let's revel.' in ticket HTML"

    def test_ticket_html_contains_brand_logo_reference(self) -> None:
        """Ticket template must reference revel-logo.png for the brand footer."""
        html = self._render_ticket_html()
        assert "revel-logo.png" in html, "Missing revel-logo.png reference in ticket HTML"

    def test_ticket_brand_logo_is_in_body_not_head(self) -> None:
        """Brand logo must live in <body> — WeasyPrint suppresses anything in <head>."""
        html = self._render_ticket_html()
        body_part = html.split("<body", 1)[1]
        assert "revel-logo.png" in body_part, "brand logo is not inside <body>; WeasyPrint would suppress it"

    def test_ticket_powered_by_is_in_body_not_head(self) -> None:
        """'Powered by' text must live in <body> — WeasyPrint suppresses anything in <head>."""
        html = self._render_ticket_html()
        body_part = html.split("<body", 1)[1]
        assert "Powered by let's revel." in body_part, (
            "'Powered by let's revel.' is not inside <body>; WeasyPrint would suppress it"
        )

    def test_ticket_html_has_no_legacy_accent_667eea(self) -> None:
        """Ticket template must not contain the legacy indigo accent #667eea."""
        html = self._render_ticket_html()
        assert "#667eea" not in html, "Legacy accent #667eea still present in ticket HTML"

    def test_ticket_html_keeps_org_logo_section(self) -> None:
        """Org logo section (logo-section / logo-container) must still be present."""
        html = self._render_ticket_html()
        assert "logo-section" in html, "Org logo section was removed from ticket HTML"

    def test_ticket_html_accent_uses_org_primary_color(self) -> None:
        """Org primary_color must flow through to accent elements (not hard-coded brand purple)."""
        html = self._render_ticket_html()
        assert "#123456" in html, "Org primary_color (#123456) not found in ticket HTML — accents may be hard-coded"

    def test_ticket_html_accent_uses_org_secondary_color(self) -> None:
        """Org secondary_color must flow through to accent-bar gradient (not hard-coded brand lilac)."""
        html = self._render_ticket_html()
        assert "#654321" in html, (
            "Org secondary_color (#654321) not found in ticket HTML — accent-bar may be hard-coded"
        )

    def test_ticket_html_no_hardcoded_brand_purple_in_org_accents(self) -> None:
        """Brand purple #8C3CDD must not appear in the ticket when org colour is different.

        Let's Revel branding belongs only in the Powered-by footer
        (whose text is #aaa, not purple), so #8C3CDD must be absent entirely.
        """
        html = self._render_ticket_html()
        assert "#8C3CDD" not in html, "#8C3CDD (brand purple) found in ticket HTML — org accent colours are hard-coded"


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
