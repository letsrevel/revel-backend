# src/events/management/commands/bootstrap_perf_tests.py
"""Bootstrap test data for Locust performance tests.

Creates:
- 1 organization (perf-test-org)
- 5 test events (RSVP, ticketed, questionnaire scenarios)
- 100 pre-verified users
- 1 simple questionnaire with automatic evaluation

Usage:
    # Create test data
    python src/manage.py bootstrap_perf_tests

    # Reset and recreate
    python src/manage.py bootstrap_perf_tests --reset
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import structlog
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.utils import timezone

from accounts.models import RevelUser
from events import models as events_models
from geo.models import City
from questionnaires import models as questionnaires_models

logger = structlog.get_logger(__name__)

# Configuration constants
PERF_ORG_SLUG = "perf-test-org"
PERF_ADMIN_EMAIL = "perf-admin@test.com"
PERF_STAFF_EMAIL = "perf-staff@test.com"
DEFAULT_PASSWORD = "password123"
NUM_PRESEEDED_USERS = 100


class Command(BaseCommand):
    """Bootstrap test data for Locust performance tests."""

    help = "Bootstrap test data for Locust performance tests."

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing perf test data before creating new data.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Bootstrap or reset performance test data."""
        if options["reset"]:
            self._reset_data()

        logger.info("Starting performance test data bootstrap...")

        with transaction.atomic():
            # Load city
            self.city = City.objects.get(name="Vienna", country="Austria")

            # Create users
            self._create_admin_users()
            self._create_preseeded_users()

            # Create organization
            self._create_organization()

            # Create events
            self._create_events()

            # Create ticket tiers
            self._create_ticket_tiers()

            # Create questionnaire
            self._create_questionnaire()

        self._print_summary()

    def _reset_data(self) -> None:
        """Delete all perf test data."""
        logger.info("Resetting performance test data...")

        # Delete organization (cascades to events, tiers, etc.)
        events_models.Organization.objects.filter(slug=PERF_ORG_SLUG).delete()

        # Delete perf test users
        RevelUser.objects.filter(email__startswith="perf-").delete()
        RevelUser.objects.filter(email=PERF_ADMIN_EMAIL).delete()
        RevelUser.objects.filter(email=PERF_STAFF_EMAIL).delete()

        logger.info("Reset complete.")

    def _create_admin_users(self) -> None:
        """Create admin and staff users."""
        logger.info("Creating admin users...")

        self.admin_user, _ = RevelUser.objects.get_or_create(
            email=PERF_ADMIN_EMAIL,
            defaults={
                "username": PERF_ADMIN_EMAIL,
                "first_name": "Perf",
                "last_name": "Admin",
                "email_verified": True,
            },
        )
        self.admin_user.set_password(DEFAULT_PASSWORD)
        self.admin_user.save()

        self.staff_user, _ = RevelUser.objects.get_or_create(
            email=PERF_STAFF_EMAIL,
            defaults={
                "username": PERF_STAFF_EMAIL,
                "first_name": "Perf",
                "last_name": "Staff",
                "email_verified": True,
            },
        )
        self.staff_user.set_password(DEFAULT_PASSWORD)
        self.staff_user.save()

        logger.info("Created admin and staff users")

    def _create_preseeded_users(self) -> None:
        """Create pre-seeded users for load testing."""
        logger.info(f"Creating {NUM_PRESEEDED_USERS} pre-seeded users...")

        self.preseeded_users = []
        for i in range(NUM_PRESEEDED_USERS):
            email = f"perf-user-{i}@test.com"
            user, created = RevelUser.objects.get_or_create(
                email=email,
                defaults={
                    "username": email,
                    "first_name": f"PerfUser{i}",
                    "last_name": "Test",
                    "email_verified": True,  # Pre-verified!
                },
            )
            if created:
                user.set_password(DEFAULT_PASSWORD)
                user.save()
            self.preseeded_users.append(user)

        logger.info(f"Created {NUM_PRESEEDED_USERS} pre-seeded users")

    def _create_organization(self) -> None:
        """Create the performance test organization."""
        logger.info("Creating performance test organization...")

        self.org, _ = events_models.Organization.objects.get_or_create(
            slug=PERF_ORG_SLUG,
            defaults={
                "name": "Performance Test Organization",
                "owner": self.admin_user,
                "visibility": events_models.Organization.Visibility.PUBLIC,
                "description": """# Performance Test Organization

This organization is used for Locust performance testing.

**DO NOT MODIFY** - Used by automated tests.
""",
                "city": self.city,
                "contact_email": PERF_ADMIN_EMAIL,
                "contact_email_verified": True,
            },
        )

        # Add staff
        self.org.staff_members.add(self.staff_user)

        # Add some members (first 20 preseeded users)
        default_tier = events_models.MembershipTier.objects.get(organization=self.org, name="General membership")
        for user in self.preseeded_users[:20]:
            events_models.OrganizationMember.objects.get_or_create(
                organization=self.org,
                user=user,
                defaults={"tier": default_tier},
            )

        logger.info(f"Created organization: {self.org.name}")

    def _create_events(self) -> None:
        """Create test events for different scenarios."""
        logger.info("Creating performance test events...")

        now = timezone.now()

        self.events: dict[str, events_models.Event] = {}

        # Event 1: RSVP event (unlimited capacity)
        self.events["rsvp"], _ = events_models.Event.objects.get_or_create(
            organization=self.org,
            slug="perf-rsvp-event",
            defaults={
                "name": "Performance Test - RSVP Event",
                "event_type": events_models.Event.EventType.PUBLIC,
                "visibility": events_models.Event.Visibility.PUBLIC,
                "status": events_models.Event.EventStatus.OPEN,
                "city": self.city,
                "requires_ticket": False,
                "start": now + timedelta(days=30),
                "end": now + timedelta(days=30, hours=3),
                "max_attendees": 0,  # Unlimited
                "description": "Performance test event for RSVP flow testing.",
                "address": "Test Venue, Vienna",
            },
        )

        # Event 2: RSVP event (limited capacity for stress testing)
        self.events["rsvp_limited"], _ = events_models.Event.objects.get_or_create(
            organization=self.org,
            slug="perf-rsvp-limited-event",
            defaults={
                "name": "Performance Test - Limited RSVP Event",
                "event_type": events_models.Event.EventType.PUBLIC,
                "visibility": events_models.Event.Visibility.PUBLIC,
                "status": events_models.Event.EventStatus.OPEN,
                "city": self.city,
                "requires_ticket": False,
                "start": now + timedelta(days=35),
                "end": now + timedelta(days=35, hours=3),
                "max_attendees": 50,  # Limited capacity
                "waitlist_open": True,
                "description": "Performance test event with limited capacity.",
                "address": "Test Venue, Vienna",
            },
        )

        # Event 3: Free ticket event
        self.events["ticket_free"], _ = events_models.Event.objects.get_or_create(
            organization=self.org,
            slug="perf-ticket-free-event",
            defaults={
                "name": "Performance Test - Free Ticket Event",
                "event_type": events_models.Event.EventType.PUBLIC,
                "visibility": events_models.Event.Visibility.PUBLIC,
                "status": events_models.Event.EventStatus.OPEN,
                "city": self.city,
                "requires_ticket": True,
                "start": now + timedelta(days=40),
                "end": now + timedelta(days=40, hours=3),
                "max_attendees": 500,
                "description": "Performance test event for free ticket checkout.",
                "address": "Test Venue, Vienna",
            },
        )

        # Event 4: PWYC ticket event
        self.events["ticket_pwyc"], _ = events_models.Event.objects.get_or_create(
            organization=self.org,
            slug="perf-ticket-pwyc-event",
            defaults={
                "name": "Performance Test - PWYC Ticket Event",
                "event_type": events_models.Event.EventType.PUBLIC,
                "visibility": events_models.Event.Visibility.PUBLIC,
                "status": events_models.Event.EventStatus.OPEN,
                "city": self.city,
                "requires_ticket": True,
                "start": now + timedelta(days=45),
                "end": now + timedelta(days=45, hours=3),
                "max_attendees": 500,
                "description": "Performance test event for PWYC checkout.",
                "address": "Test Venue, Vienna",
            },
        )

        # Event 5: Questionnaire event
        self.events["questionnaire"], _ = events_models.Event.objects.get_or_create(
            organization=self.org,
            slug="perf-questionnaire-event",
            defaults={
                "name": "Performance Test - Questionnaire Event",
                "event_type": events_models.Event.EventType.PUBLIC,
                "visibility": events_models.Event.Visibility.PUBLIC,
                "status": events_models.Event.EventStatus.OPEN,
                "city": self.city,
                "requires_ticket": False,
                "start": now + timedelta(days=50),
                "end": now + timedelta(days=50, hours=3),
                "max_attendees": 0,
                "description": "Performance test event with questionnaire requirement.",
                "address": "Test Venue, Vienna",
            },
        )

        logger.info(f"Created {len(self.events)} test events")

    def _create_ticket_tiers(self) -> None:
        """Create ticket tiers for ticketed events."""
        logger.info("Creating ticket tiers...")

        now = timezone.now()

        # Free ticket tier
        events_models.TicketTier.objects.filter(
            event=self.events["ticket_free"],
            name="General Admission",
        ).update(
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.FREE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("0.00"),
            currency="EUR",
            total_quantity=500,
            quantity_sold=0,
            sales_start_at=now - timedelta(days=1),
            sales_end_at=now + timedelta(days=39),
            description="Free performance test ticket",
        )

        # Delete default tier and create PWYC tier
        events_models.TicketTier.objects.filter(
            event=self.events["ticket_pwyc"],
            name="General Admission",
        ).delete()

        events_models.TicketTier.objects.get_or_create(
            event=self.events["ticket_pwyc"],
            name="PWYC Admission",
            defaults={
                "visibility": events_models.TicketTier.Visibility.PUBLIC,
                "payment_method": events_models.TicketTier.PaymentMethod.ONLINE,
                "purchasable_by": events_models.TicketTier.PurchasableBy.PUBLIC,
                "price_type": events_models.TicketTier.PriceType.PWYC,
                "price": Decimal("10.00"),
                "pwyc_min": Decimal("5.00"),
                "pwyc_max": Decimal("50.00"),
                "currency": "EUR",
                "total_quantity": 500,
                "quantity_sold": 0,
                "sales_start_at": now - timedelta(days=1),
                "sales_end_at": now + timedelta(days=44),
                "description": "Pay what you can - performance test ticket",
            },
        )

        logger.info("Created ticket tiers")

    def _create_questionnaire(self) -> None:
        """Create a simple questionnaire for testing."""
        logger.info("Creating performance test questionnaire...")

        # Check if questionnaire already exists
        existing = events_models.OrganizationQuestionnaire.objects.filter(
            organization=self.org,
            questionnaire__name="Performance Test Questionnaire",
        ).first()

        if existing:
            self.org_questionnaire = existing
            logger.info("Questionnaire already exists, skipping creation")
            return

        # Create questionnaire with MANUAL evaluation (no LLM during tests)
        questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Performance Test Questionnaire",
            status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            shuffle_questions=False,
            max_attempts=999,  # Unlimited for testing
            min_score=Decimal("0.00"),  # Always pass (manual evaluation)
        )

        # Create section
        section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Performance Test Section",
            order=1,
        )

        # Create a simple multiple choice question
        question = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Do you want to attend this performance test event?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=1,
        )

        # Create options (first one is "correct" for predictable testing)
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=question,
            option="Yes, I want to attend",
            is_correct=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=question,
            option="No, I do not want to attend",
            is_correct=False,
            order=2,
        )

        # Create OrganizationQuestionnaire and link to event
        self.org_questionnaire = events_models.OrganizationQuestionnaire.objects.create(
            organization=self.org,
            questionnaire=questionnaire,
            questionnaire_type=events_models.OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        # Link to questionnaire event
        self.org_questionnaire.events.add(self.events["questionnaire"])

        logger.info("Created performance test questionnaire")

    def _print_summary(self) -> None:
        """Print summary of created data."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("Performance Test Data Bootstrap Complete!"))
        self.stdout.write("=" * 60 + "\n")

        self.stdout.write(self.style.NOTICE("Organization:"))
        self.stdout.write(f"  Slug: {PERF_ORG_SLUG}")
        self.stdout.write(f"  Admin: {PERF_ADMIN_EMAIL} / {DEFAULT_PASSWORD}")
        self.stdout.write(f"  Staff: {PERF_STAFF_EMAIL} / {DEFAULT_PASSWORD}")

        self.stdout.write(self.style.NOTICE("\nPre-seeded Users:"))
        self.stdout.write(f"  Count: {NUM_PRESEEDED_USERS}")
        self.stdout.write(f"  Pattern: perf-user-{{0..{NUM_PRESEEDED_USERS - 1}}}@test.com")
        self.stdout.write(f"  Password: {DEFAULT_PASSWORD}")

        self.stdout.write(self.style.NOTICE("\nTest Events:"))
        for key, event in self.events.items():
            self.stdout.write(f"  [{key}] {event.slug}")

        self.stdout.write(self.style.NOTICE("\nUsage:"))
        self.stdout.write("  cd tests/performance")
        self.stdout.write("  locust -f locustfile.py --host=http://localhost:8000/api")
        self.stdout.write("")
