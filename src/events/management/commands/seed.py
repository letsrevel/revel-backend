"""Database seeding command for comprehensive test data generation.

This command creates a large, realistic, reproducible dataset for testing and development.
It uses faker and seeded random for reproducibility and supports configurable scale.

Usage:
    python manage.py seed --seed 42
    python manage.py seed --seed 42 --users 500 --organizations 10 --events 25
    python manage.py seed --seed 42 --clear  # Clear existing data first
"""

import gc
import time
import typing as t

from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    RevelUser,
    UserDietaryPreference,
)
from common.models import Tag, TagAssignment
from events.management.commands.seeder.config import SeederConfig
from events.management.commands.seeder.events import EventSeeder
from events.management.commands.seeder.files import FileSeeder
from events.management.commands.seeder.interactions import InteractionSeeder
from events.management.commands.seeder.notifications import NotificationSeeder
from events.management.commands.seeder.organizations import OrganizationSeeder
from events.management.commands.seeder.preferences import PreferencesSeeder
from events.management.commands.seeder.questionnaires import QuestionnaireSeeder
from events.management.commands.seeder.social import SocialSeeder
from events.management.commands.seeder.state import SeederState
from events.management.commands.seeder.telegram import TelegramSeeder
from events.management.commands.seeder.tickets import TicketSeeder
from events.management.commands.seeder.users import UserSeeder
from events.management.commands.seeder.venues import VenueSeeder
from events.models import (
    AdditionalResource,
    AttendeeVisibilityFlag,
    Blacklist,
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventQuestionnaireSubmission,
    EventRSVP,
    EventSeries,
    EventSeriesFollow,
    EventToken,
    EventWaitList,
    GeneralUserPreferences,
    MembershipTier,
    Organization,
    OrganizationFollow,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    OrganizationStaff,
    OrganizationToken,
    Payment,
    PendingEventInvitation,
    PotluckItem,
    Ticket,
    TicketTier,
    Venue,
    VenueSeat,
    VenueSector,
    WhitelistRequest,
)
from notifications.models import Notification, NotificationDelivery, NotificationPreference
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from telegram.models import AccountOTP, TelegramUser


class Command(BaseCommand):
    """Seeds the database with comprehensive, realistic test data."""

    help = "Seeds the database with a large, realistic set of reproducible data."

    def add_arguments(self, parser: t.Any) -> None:
        """Add CLI arguments."""
        parser.add_argument(
            "--seed",
            type=int,
            required=True,
            help="Random seed for reproducibility",
        )
        parser.add_argument(
            "--users",
            type=int,
            default=1000,
            help="Number of users to create (default: 1000)",
        )
        parser.add_argument(
            "--organizations",
            type=int,
            default=20,
            help="Number of organizations to create (default: 20)",
        )
        parser.add_argument(
            "--events",
            type=int,
            default=50,
            help="Number of events per organization (default: 50)",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing data before seeding (requires confirmation)",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip confirmation prompts",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Execute the seeding process."""
        seed = options["seed"]
        num_users = options["users"]
        num_organizations = options["organizations"]
        num_events = options["events"]
        clear_data = options["clear"]
        skip_confirm = options["yes"]

        self.stdout.write(self.style.WARNING("Seeding Configuration:"))
        self.stdout.write(f"  Seed: {seed}")
        self.stdout.write(f"  Users: {num_users}")
        self.stdout.write(f"  Organizations: {num_organizations}")
        self.stdout.write(f"  Events per org: {num_events}")
        self.stdout.write(f"  Total events: ~{num_organizations * num_events}")
        self.stdout.write(f"  Clear data: {clear_data}")

        if clear_data and not skip_confirm:
            confirmation = input(
                self.style.ERROR("\nThis will DELETE ALL existing data. Are you sure you want to continue? (yes/no): ")
            )
            if confirmation.lower() != "yes":
                raise CommandError("Seeding cancelled by user.")

        # Create configuration
        seeder_config = SeederConfig(
            seed=seed,
            num_users=num_users,
            num_organizations=num_organizations,
            num_events_per_org=num_events,
            clear_data=clear_data,
        )

        # Create shared state
        state = SeederState()

        start_time = time.time()

        with transaction.atomic():
            if clear_data:
                self._clear_data()

            self._create_superuser()

            # Run seeders in dependency order
            self._run_seeders(seeder_config, state)

        end_time = time.time()
        elapsed = end_time - start_time

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("Database seeding complete!"))
        self.stdout.write(self.style.SUCCESS(f"Time elapsed: {elapsed:.2f} seconds"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self._print_summary(state)

    def _clear_data(self) -> None:
        """Delete all existing data in proper FK order."""
        self.stdout.write(self.style.WARNING("\nClearing existing data..."))

        # Order matters - delete dependents first
        deletion_order = [
            # Telegram
            (AccountOTP, "AccountOTP"),
            (TelegramUser, "TelegramUser"),
            # Notifications
            (NotificationDelivery, "NotificationDelivery"),
            (Notification, "Notification"),
            (NotificationPreference, "NotificationPreference"),
            # Questionnaire submissions and evaluations
            (QuestionnaireEvaluation, "QuestionnaireEvaluation"),
            (EventQuestionnaireSubmission, "EventQuestionnaireSubmission"),
            (FreeTextAnswer, "FreeTextAnswer"),
            (MultipleChoiceAnswer, "MultipleChoiceAnswer"),
            (QuestionnaireFile, "QuestionnaireFile"),
            (QuestionnaireSubmission, "QuestionnaireSubmission"),
            # Questionnaire structure
            (MultipleChoiceOption, "MultipleChoiceOption"),
            (MultipleChoiceQuestion, "MultipleChoiceQuestion"),
            (FreeTextQuestion, "FreeTextQuestion"),
            (QuestionnaireSection, "QuestionnaireSection"),
            (OrganizationQuestionnaire, "OrganizationQuestionnaire"),
            (Questionnaire, "Questionnaire"),
            # Event interactions
            (WhitelistRequest, "WhitelistRequest"),
            (Blacklist, "Blacklist"),
            (PotluckItem, "PotluckItem"),
            (EventWaitList, "EventWaitList"),
            (EventRSVP, "EventRSVP"),
            (EventInvitationRequest, "EventInvitationRequest"),
            (PendingEventInvitation, "PendingEventInvitation"),
            (EventInvitation, "EventInvitation"),
            (EventToken, "EventToken"),
            # Tickets and payments
            (Payment, "Payment"),
            (Ticket, "Ticket"),
            (TicketTier, "TicketTier"),
            # Venues
            (VenueSeat, "VenueSeat"),
            (VenueSector, "VenueSector"),
            (Venue, "Venue"),
            # Events
            (AdditionalResource, "AdditionalResource"),
            (Event, "Event"),
            (EventSeries, "EventSeries"),
            # Social
            (TagAssignment, "TagAssignment"),
            (Tag, "Tag"),
            (EventSeriesFollow, "EventSeriesFollow"),
            (OrganizationFollow, "OrganizationFollow"),
            # Preferences
            (AttendeeVisibilityFlag, "AttendeeVisibilityFlag"),
            (GeneralUserPreferences, "GeneralUserPreferences"),
            # Organization
            (OrganizationMembershipRequest, "OrganizationMembershipRequest"),
            (OrganizationMember, "OrganizationMember"),
            (OrganizationStaff, "OrganizationStaff"),
            (OrganizationToken, "OrganizationToken"),
            (MembershipTier, "MembershipTier"),
            (Organization, "Organization"),
            # Users and dietary
            (UserDietaryPreference, "UserDietaryPreference"),
            (DietaryPreference, "DietaryPreference"),
            (DietaryRestriction, "DietaryRestriction"),
            (FoodItem, "FoodItem"),
            (RevelUser, "RevelUser"),
        ]

        for model, name in deletion_order:
            count = model.objects.count()  # type: ignore[attr-defined]
            if count > 0:
                model.objects.all().delete()  # type: ignore[attr-defined]
                self.stdout.write(f"  Deleted {count} {name} records")

        self.stdout.write(self.style.SUCCESS("Data cleared.\n"))

    def _create_superuser(self) -> None:
        """Create or verify superuser exists."""
        default_username = "admin@revel.io"
        default_password = "password"
        default_email = "admin@revel.io"

        username = config("DEFAULT_SUPERUSER_USERNAME", default=default_username)
        password = config("DEFAULT_SUPERUSER_PASSWORD", default=default_password)
        email = config("DEFAULT_SUPERUSER_EMAIL", default=default_email)

        if RevelUser.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f"Superuser '{username}' already exists."))
        else:
            RevelUser.objects.create_superuser(username=username, password=password, email=email)
            self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' created successfully."))

            if password == default_password:
                self.stdout.write(self.style.WARNING("Using default password. Change it for production!"))

    def _run_seeders(self, seeder_config: SeederConfig, state: SeederState) -> None:
        """Run all seeders in dependency order."""
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Starting seeding process..."))
        self.stdout.write("")

        # Phase 1: Files (placeholders for uploads)
        self.stdout.write(self.style.HTTP_INFO("Phase 1: Generating placeholder files"))
        FileSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 2: Users and dietary data
        self.stdout.write(self.style.HTTP_INFO("\nPhase 2: Creating users"))
        UserSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 3: Organizations
        self.stdout.write(self.style.HTTP_INFO("\nPhase 3: Creating organizations"))
        OrganizationSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 4: Venues
        self.stdout.write(self.style.HTTP_INFO("\nPhase 4: Creating venues"))
        VenueSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 5: Events
        self.stdout.write(self.style.HTTP_INFO("\nPhase 5: Creating events"))
        EventSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 6: Questionnaires (foundation)
        self.stdout.write(self.style.HTTP_INFO("\nPhase 6: Creating questionnaires"))
        QuestionnaireSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 7: Tickets (depends on events, questionnaires)
        self.stdout.write(self.style.HTTP_INFO("\nPhase 7: Creating tickets"))
        TicketSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 8: Interactions (invitations, RSVPs, etc.)
        self.stdout.write(self.style.HTTP_INFO("\nPhase 8: Creating interactions"))
        InteractionSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 9: Notifications (preferences only - actual notifications via signals)
        self.stdout.write(self.style.HTTP_INFO("\nPhase 9: Creating notification preferences"))
        NotificationSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 10: Social (tags, follows)
        self.stdout.write(self.style.HTTP_INFO("\nPhase 10: Creating social features"))
        SocialSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 11: User preferences
        self.stdout.write(self.style.HTTP_INFO("\nPhase 11: Creating user preferences"))
        PreferencesSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

        # Phase 12: Telegram
        self.stdout.write(self.style.HTTP_INFO("\nPhase 12: Creating Telegram links"))
        TelegramSeeder(seeder_config, state, self.stdout).seed()
        gc.collect()

    def _print_summary(self, state: SeederState) -> None:
        """Print summary of created data."""
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Summary:"))
        self.stdout.write(f"  Users: {len(state.users)}")
        self.stdout.write(f"    - Owners: {len(state.owner_users)}")
        self.stdout.write(f"    - Staff: {len(state.staff_users)}")
        self.stdout.write(f"    - Members: {len(state.member_users)}")
        self.stdout.write(f"    - Regular: {len(state.regular_users)}")
        self.stdout.write(f"  Organizations: {len(state.organizations)}")
        self.stdout.write(f"  Events: {len(state.events)}")
        self.stdout.write(f"    - Ticketed: {len(state.ticketed_events)}")
        self.stdout.write(f"    - Non-ticketed: {len(state.non_ticketed_events)}")
        self.stdout.write(f"    - Private: {len(state.private_events)}")
        self.stdout.write(f"    - With waitlist: {len(state.waitlist_events)}")
        self.stdout.write(f"    - With potluck: {len(state.potluck_events)}")
        self.stdout.write(f"    - Past: {len(state.past_events)}")
        self.stdout.write(f"    - Sold out: {len(state.sold_out_events)}")
        self.stdout.write(f"  Event Series: {len(state.event_series)}")
        self.stdout.write(f"  Questionnaires: {len(state.questionnaires)}")
        self.stdout.write(f"  Tags: {len(state.tags)}")
