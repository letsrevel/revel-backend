"""Management command to render notification email templates with real data.

This command renders email templates and ticket PDFs using existing data from
the database to verify that markdown rendering works correctly with the new
nh3-based sanitization and markdown rendering.

Usage:
    python manage.py render_templates
    python manage.py render_templates --output-dir /path/to/output
    python manage.py render_templates --event-id <uuid>
"""

import typing as t
from pathlib import Path

from django.core.management.base import BaseCommand, CommandParser
from django.template.loader import render_to_string
from django.utils.dateformat import format as date_format

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Ticket
from events.utils import create_ticket_pdf


class Command(BaseCommand):
    """Render notification email templates with real database data."""

    help = "Render notification email templates and ticket PDFs using real data from the database"

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--output-dir",
            type=str,
            default="rendered_templates",
            help="Output directory for rendered templates (default: rendered_templates)",
        )
        parser.add_argument(
            "--event-id",
            type=str,
            help="Specific event ID to use (optional, uses first available event if not specified)",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the command."""
        output_dir = Path(options["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        # Get an event to use for rendering
        event = self._get_event(options.get("event_id"))
        if not event:
            self.stderr.write(self.style.ERROR("No events found in database. Please seed some data first."))
            return

        # Get a user for the notification context
        user = self._get_user()
        if not user:
            self.stderr.write(self.style.ERROR("No users found in database. Please seed some data first."))
            return

        self.stdout.write(f"Using event: {event.name} (org: {event.organization.name})")
        self.stdout.write(f"Using user: {user.display_name} ({user.email})")
        self.stdout.write(f"Output directory: {output_dir.absolute()}")
        self.stdout.write("")

        # Render event_open email template
        self._render_event_open(event, user, output_dir)

        # Render ticket PDF
        self._render_ticket_pdf(event, output_dir)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Templates rendered successfully!"))
        self.stdout.write(f"View email: open {output_dir}/event_open_email.html")
        self.stdout.write(f"View ticket: open {output_dir}/ticket.pdf")

    def _get_event(self, event_id: str | None) -> Event | None:
        """Get an event to use for rendering."""
        if event_id:
            try:
                return Event.objects.select_related("organization", "city").get(id=event_id)
            except Event.DoesNotExist:
                self.stderr.write(self.style.WARNING(f"Event {event_id} not found, using first available"))

        # Get any event with a description
        event = (
            Event.objects.select_related("organization", "city")
            .exclude(description__isnull=True)
            .exclude(description="")
            .first()
        )
        if event:
            return event

        # Fallback to any event
        return Event.objects.select_related("organization", "city").first()

    def _get_user(self) -> RevelUser | None:
        """Get a user for the notification context."""
        return RevelUser.objects.first()

    def _render_event_open(self, event: Event, user: RevelUser, output_dir: Path) -> None:
        """Render the event_open email template."""
        self.stdout.write("Rendering event_open_email.html...")

        # Build the context similar to notification_helpers.py
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        frontend_url = f"{frontend_base_url}/events/{event.id}"

        event_start_formatted = date_format(event.start, "l, F j, Y \\a\\t g:i A T") if event.start else ""
        event_end_formatted = date_format(event.end, "l, F j, Y \\a\\t g:i A T") if event.end else ""

        context = {
            "user": user,
            "context": {
                "event_id": str(event.id),
                "event_name": event.name,
                "event_description": event.description or "",
                "event_start": event.start.isoformat() if event.start else "",
                "event_start_formatted": event_start_formatted,
                "event_end_formatted": event_end_formatted,
                "event_location": event.full_address(),
                "event_url": frontend_url,
                "organization_id": str(event.organization.id),
                "organization_name": event.organization.name,
                "rsvp_required": not event.requires_ticket,
                "tickets_available": event.requires_ticket,
                "questionnaire_required": False,
                # Add unsubscribe link for footer
                "unsubscribe_url": f"{frontend_base_url}/unsubscribe?token=example-token",
            },
        }

        # Render the template
        html = render_to_string("notifications/email/event_open.html", context)

        # Write to file
        output_file = output_dir / "event_open_email.html"
        output_file.write_text(html)

        self.stdout.write(f"  - Description length: {len(event.description or '')} chars")
        self.stdout.write(f"  - Written to: {output_file}")

        # Also show a preview of the description
        if event.description:
            preview = event.description[:200] + "..." if len(event.description) > 200 else event.description
            self.stdout.write(f"  - Description preview: {preview}")

    def _render_ticket_pdf(self, event: Event, output_dir: Path) -> None:
        """Render a ticket PDF using real ticket data."""
        self.stdout.write("Rendering ticket.pdf...")

        # Find a ticket for this event, or any ticket
        ticket = (
            Ticket.objects.select_related(
                "event",
                "event__organization",
                "event__event_series",
                "event__city",
                "user",
                "tier",
                "venue",
                "sector",
                "seat",
            )
            .filter(event=event)
            .first()
        )

        if not ticket:
            # Try any ticket
            ticket = Ticket.objects.select_related(
                "event",
                "event__organization",
                "event__event_series",
                "event__city",
                "user",
                "tier",
                "venue",
                "sector",
                "seat",
            ).first()

        if not ticket:
            self.stdout.write(self.style.WARNING("  - No tickets found in database, skipping PDF generation"))
            return

        self.stdout.write(f"  - Using ticket: {ticket.id} for event: {ticket.event.name}")
        self.stdout.write(f"  - Ticket holder: {ticket.user.display_name}")
        self.stdout.write(f"  - Tier: {ticket.tier.name}")

        # Generate the PDF
        pdf_bytes = create_ticket_pdf(ticket)

        # Write to file
        output_file = output_dir / "ticket.pdf"
        output_file.write_bytes(pdf_bytes)

        self.stdout.write(f"  - Written to: {output_file} ({len(pdf_bytes)} bytes)")
