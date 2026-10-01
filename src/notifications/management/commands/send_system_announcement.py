"""Send a system announcement from the command line.

Same code path as the admin "Send System Announcement" view
(``notifications.service.system_announcement``): same notification type, context,
HTML sanitization, recipients and delivery channels.

The body file holds the HTML the admin's Trix editor produces. It is sanitized with
``common.sanitizers.sanitize_html``, which keeps only: p, br, hr, strong, em, b, i, u,
s, code, pre, h1-h6, ul, ol, li, a (href, title; http/https/mailto only), blockquote
and table/thead/tbody/tfoot/tr/th/td. Anything else (img, div, span, style, scripts)
is stripped, keeping its text. Separate paragraphs with ``<p>`` or ``<br>``: plain
newlines are not line breaks in the HTML email.

Usage:
    # Preview: recipient count and rendered email, nothing created
    python manage.py send_system_announcement --title "..." --body-file body.html --dry-run

    # Send to specific existing users only (e.g. to check the email in mailpit)
    python manage.py send_system_announcement --title "..." --body-file - --to-email me@example.com

    # Send to every active non-guest user
    python manage.py send_system_announcement --title "..." --body-file body.html --confirm
"""

import sys
import typing as t

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import QuerySet

from accounts.models import RevelUser
from notifications.context_schemas import SystemAnnouncementContext
from notifications.service import system_announcement


class Command(BaseCommand):
    """Send a SYSTEM_ANNOUNCEMENT notification to users."""

    help = "Send a system announcement (same path as the admin view). Broadcasting requires --confirm."

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument("--title", required=True, help="Announcement title (email subject, in-app title).")
        parser.add_argument(
            "--body-file",
            required=True,
            help="Path to the HTML body, or '-' to read stdin. See the module docstring for allowed tags.",
        )
        parser.add_argument("--url", default="", help="Optional 'Read more' link.")
        parser.add_argument(
            "--include-guests", action="store_true", help="Also send to guest users (ignored with --to-email)."
        )
        parser.add_argument(
            "--to-email",
            action="append",
            default=None,
            metavar="ADDRESS",
            help="Send only to this existing active user. Repeatable.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show recipient count and rendered email; create nothing."
        )
        parser.add_argument("--confirm", action="store_true", help="Required to send to all matching users.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the command."""
        title: str = options["title"].strip()
        if not title:
            raise CommandError("--title must not be empty.")
        if len(title) > 200:  # same limit as the admin form
            raise CommandError("--title must be at most 200 characters.")

        body = self._read_body(options["body_file"])
        context = system_announcement.build_context(title=title, body=body, url=options["url"])
        if not context["announcement_body"].strip():
            raise CommandError("The body is empty after sanitization.")

        emails: list[str] | None = options["to_email"]
        if emails is not None:
            emails = [email.strip() for email in emails]
            if not all(emails):  # e.g. an unset shell variable; "" would match users with no email
                raise CommandError("--to-email must not be blank.")
        recipients = system_announcement.get_recipients(include_guests=options["include_guests"], emails=emails)
        if emails is not None:
            self._check_all_emails_found(emails, list(recipients.values_list("email", flat=True)))

        count = recipients.count()
        audience = "the given addresses" if emails is not None else "ALL matching users"
        self.stdout.write(f"Recipients ({audience}): {count}")

        if options["dry_run"]:
            self._print_preview(context, recipients)
            self.stdout.write(self.style.WARNING("DRY RUN - nothing was created or sent."))
            return

        if emails is None and not options["confirm"]:
            raise CommandError(f"Refusing to send to {count} users without --confirm (or use --to-email / --dry-run).")
        if count == 0:
            self.stdout.write(self.style.WARNING("No recipients; nothing sent."))
            return

        sent = system_announcement.send(context, recipients)
        self.stdout.write(self.style.SUCCESS(f"System announcement queued for {sent} users."))

    def _read_body(self, path: str) -> str:
        """Read the body from a file or stdin, refusing empty input."""
        if path == "-":
            body = sys.stdin.read()
        else:
            try:
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
            except OSError as e:
                raise CommandError(f"Cannot read {path}: {e}") from e
        if not body.strip():
            raise CommandError("The body file is empty.")
        return body

    def _check_all_emails_found(self, requested: list[str], found: list[str]) -> None:
        """Fail if any requested address is not an existing active user."""
        found_lower = {email.lower() for email in found}
        missing = [email for email in requested if email.lower() not in found_lower]
        if missing:
            raise CommandError(f"No active user for: {', '.join(missing)}")

    def _print_preview(
        self,
        context: SystemAnnouncementContext,
        recipients: QuerySet[RevelUser],
    ) -> None:
        """Print the email as the first recipient would get it."""
        first = recipients.first()
        if first is None:
            return
        preview = system_announcement.render_email_preview(context, first)
        self.stdout.write(f"\n--- Email preview (rendered for {first.email}) ---")
        self.stdout.write(f"Subject: {preview.subject}\n")
        self.stdout.write(preview.text_body)
        self.stdout.write("--- end preview ---\n")
