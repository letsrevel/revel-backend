"""``python manage.py provision_stripe_webhooks --url <https://...>``.

Creates the two Stripe webhook endpoints Revel needs (platform + Connect),
both pinned to ``settings.STRIPE_API_VERSION``, and prints the resulting
``whsec_*`` signing secrets so the operator can paste them into
``STRIPE_WEBHOOK_SECRETS`` (CSV) on the deploy.

Both endpoints target the SAME URL. Stripe distinguishes them by the
``connect`` flag, so a single ``/api/stripe/webhook`` URL serves both
"Your account" platform deliveries and "Connected accounts" deliveries,
each with its own signing secret. ``verify_webhook`` tries each secret in
turn (see ``events/service/stripe_webhooks.py``).

The command refuses to run if any endpoint already targets the URL, unless
``--force`` is given (cutover: the old endpoint stays active alongside the
new ones until the operator disables it in the dashboard — the event-log
dedup makes the overlap harmless).
"""

import typing as t

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

# Must stay in sync with the dispatch map in
# events/service/stripe_webhooks.py::StripeEventHandler.handle — Stripe only
# delivers events you subscribe to.
PLATFORM_EVENTS: tuple[str, ...] = (
    "checkout.session.completed",
    "charge.refunded",
    "payment_intent.canceled",
)
CONNECT_EVENTS: tuple[str, ...] = PLATFORM_EVENTS + ("account.updated",)


class Command(BaseCommand):
    """Provision the platform + Connect webhook endpoints in Stripe."""

    help = (
        "Create two Stripe webhook endpoints (platform + Connect) at the given "
        "URL and print the whsec_* signing secrets for STRIPE_WEBHOOK_SECRETS."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Wire ``--url`` (required), ``--dry-run`` and ``--force`` flags."""
        parser.add_argument(
            "--url",
            required=True,
            help="Public URL of /api/stripe/webhook (e.g. https://api.letsrevel.io/api/stripe/webhook).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be created without calling the Stripe API.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Create even if endpoints already target this URL (cutover overlap).",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Validate ``--url``, then either preview or POST two endpoints to Stripe."""
        url: str = options["url"]
        if not url.startswith("https://"):
            raise CommandError(f"--url must be https:// (Stripe rejects http in live mode). Got: {url}")

        if options["dry_run"]:
            self.stdout.write("DRY RUN — no API calls.")
            self.stdout.write(f"Would create platform endpoint at {url}: {', '.join(PLATFORM_EVENTS)}")
            self.stdout.write(f"Would create Connect endpoint at {url}: {', '.join(CONNECT_EVENTS)}")
            return

        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.api_version = settings.STRIPE_API_VERSION

        existing = [ep for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter() if ep.url == url]
        if existing and not options["force"]:
            ids = ", ".join(ep.id for ep in existing)
            raise CommandError(
                f"Endpoint(s) already target {url}: {ids}. Re-run with --force to create "
                "the new pair alongside them (then disable the old one in the dashboard)."
            )

        platform = stripe.WebhookEndpoint.create(
            url=url,
            enabled_events=list(PLATFORM_EVENTS),
            connect=False,
            api_version=settings.STRIPE_API_VERSION,
            description="Revel platform events",
        )
        connect = stripe.WebhookEndpoint.create(
            url=url,
            enabled_events=list(CONNECT_EVENTS),
            connect=True,
            api_version=settings.STRIPE_API_VERSION,
            description="Revel Connect events",
        )

        self.stdout.write(self.style.SUCCESS("Created two webhook endpoints:"))
        self.stdout.write(f"  Platform: {platform.id}  secret: {platform.secret}")
        self.stdout.write(f"  Connect:  {connect.id}  secret: {connect.secret}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Set in .env (CSV, no spaces — keep the OLD secret during cutover):"))
        self.stdout.write(f"  STRIPE_WEBHOOK_SECRETS={platform.secret},{connect.secret},<old whsec_ during overlap>")
