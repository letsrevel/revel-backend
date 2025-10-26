# src/events/management/commands/bootstrap_test_events.py

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import RevelUser
from common.models import Tag
from events import models as events_models
from geo.models import City
from questionnaires import models as questionnaires_models

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Bootstrap test events covering all eligibility gates for frontend testing.

    Creates a third organization with events that test every possible eligibility
    scenario: questionnaires, membership requirements, invitations, capacity limits,
    RSVP deadlines, ticket sales windows, and event status gates.
    """

    help = "Bootstrap test events covering all eligibility gates for frontend testing."

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        """Bootstrap test events data."""
        logger.info("Starting eligibility test events bootstrap...")

        self.now = timezone.now()

        # Load city
        self.city = City.objects.get(name="Vienna", country="Austria")

        # Create users
        self._create_users()

        # Create organization
        self._create_organization()

        # Create questionnaire
        self._create_questionnaire()

        # Create test events
        self._create_test_events()

        # Create ticket tiers
        self._create_ticket_tiers()

        # Create some relationships to simulate sold-out scenarios
        self._create_relationships()

        logger.info("Eligibility test events bootstrap complete!")
        logger.info("\n=== Test Users Created ===")
        logger.info(f"Random User (no org): {self.random_user.email} / password123")
        logger.info(f"Admin User: {self.admin_user.email} / password123")
        logger.info(f"Staff User: {self.staff_user.email} / password123")
        logger.info(f"Member User: {self.member_user.email} / password123")
        logger.info(f"\nOrganization: {self.org.name} (slug: {self.org.slug})")

    def _create_users(self) -> None:
        """Create test users for eligibility testing."""
        logger.info("Creating test users...")

        # Random user with no organization memberships
        self.random_user = RevelUser.objects.create_user(
            username="test.random@example.com",
            password="password123",
            email="test.random@example.com",
            first_name="Random",
            last_name="Tester",
        )

        # Organization-specific users
        self.admin_user = RevelUser.objects.create_user(
            username="test.admin@example.com",
            password="password123",
            email="test.admin@example.com",
            first_name="Test",
            last_name="Admin",
        )

        self.staff_user = RevelUser.objects.create_user(
            username="test.staff@example.com",
            password="password123",
            email="test.staff@example.com",
            first_name="Test",
            last_name="Staff",
        )

        self.member_user = RevelUser.objects.create_user(
            username="test.member@example.com",
            password="password123",
            email="test.member@example.com",
            first_name="Test",
            last_name="Member",
        )

        logger.info("Created 4 test users")

    def _create_organization(self) -> None:
        """Create test organization with members."""
        logger.info("Creating test organization...")

        self.org = events_models.Organization.objects.create(
            name="Eligibility Test Organization",
            slug="eligibility-test-org",
            owner=self.admin_user,
            visibility=events_models.Organization.Visibility.PUBLIC,
            description="""# Eligibility Test Organization

This organization contains test events designed to showcase every eligibility gate
and access control scenario in the Revel platform. Perfect for frontend testing!

## Purpose
- Test all NextStep scenarios
- Validate eligibility checks
- Test user experience flows
- Frontend development and QA
""",
            city=self.city,
        )

        # Add staff and members
        self.org.staff_members.add(self.staff_user)
        self.org.members.add(self.member_user)

        # Add some tags
        Tag.objects.get_or_create(name="tech", defaults={"color": "#6C5CE7"})
        Tag.objects.get_or_create(name="test", defaults={"color": "#FF6B6B"})
        self.org.add_tags("tech", "test")

        # Update organization settings
        self.org.accept_membership_requests = True
        self.org.contact_email = "test@eligibility.example.com"
        self.org.contact_email_verified = True
        self.org.save()

        logger.info(f"Created organization: {self.org.name}")

    def _create_questionnaire(self) -> None:
        """Create a simple questionnaire for testing."""
        logger.info("Creating test questionnaire...")

        # Simple automatic questionnaire
        self.questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Eligibility Test Questionnaire",
            status=questionnaires_models.Questionnaire.Status.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.EvaluationMode.AUTOMATIC,
            shuffle_questions=False,
            llm_backend=questionnaires_models.Questionnaire.LLMBackend.MOCK,
            max_attempts=3,
            min_score=Decimal("100.00"),
        )

        section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=self.questionnaire,
            name="Agreement",
            order=1,
        )

        question = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=self.questionnaire,
            section=section,
            question="Do you agree to participate in this test event?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=question,
            option="Yes, I agree",
            is_correct=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=question,
            option="No, I decline",
            is_correct=False,
            order=2,
        )

        # Add a free text question
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=self.questionnaire,
            section=section,
            question="Why are you interested in attending this event? (This helps us understand our attendees better)",
            llm_guidelines="Look for genuine interest and enthusiasm. Any thoughtful response should be accepted.",
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=2,
        )

        # Create OrganizationQuestionnaire
        self.org_questionnaire = events_models.OrganizationQuestionnaire.objects.create(
            organization=self.org,
            questionnaire=self.questionnaire,
            questionnaire_type=events_models.OrganizationQuestionnaire.Types.ADMISSION,
        )

        logger.info("Created test questionnaire")

    def _create_test_events(self) -> None:
        """Create events testing each eligibility gate."""
        logger.info("Creating test events for each eligibility scenario...")

        self.events = {}

        # Event 1: Perfect event - random user should be able to access
        self.events["accessible"] = events_models.Event.objects.create(
            organization=self.org,
            name="✅ Accessible Public Event",
            slug="test-accessible-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=30),
            end=self.now + timedelta(days=30, hours=3),
            max_attendees=0,  # Unlimited
            description="""# ✅ Accessible Public Event

**Test Scenario:** No restrictions - any user can RSVP

This event has:
- ✅ Public visibility
- ✅ Public event type
- ✅ Status: OPEN
- ✅ No ticket required
- ✅ No questionnaire
- ✅ No RSVP deadline
- ✅ Unlimited capacity

**Expected NextStep:** RSVP
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 2: Requires questionnaire completion
        event_with_questionnaire = events_models.Event.objects.create(
            organization=self.org,
            name="📋 Event Requires Questionnaire",
            slug="test-event-with-questionnaire",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=35),
            end=self.now + timedelta(days=35, hours=3),
            description="""# 📋 Event Requires Questionnaire

**Test Scenario:** QuestionnaireGate - Must complete questionnaire first

This event has:
- ✅ Public visibility
- ❌ Requires questionnaire completion

**Expected NextStep:** COMPLETE_QUESTIONNAIRE
**Expected Reason:** Questionnaire has not been filled
""",
            address="Test Venue, Vienna, Austria",
        )
        self.org_questionnaire.events.add(event_with_questionnaire)
        self.events["questionnaire"] = event_with_questionnaire

        # Event 3: Members-only event
        self.events["members_only"] = events_models.Event.objects.create(
            organization=self.org,
            name="👥 Members-Only Event",
            slug="test-members-only-event",
            event_type=events_models.Event.Types.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.PUBLIC,  # Visible but requires membership
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=40),
            end=self.now + timedelta(days=40, hours=3),
            description="""# 👥 Members-Only Event

**Test Scenario:** MembershipGate - Must be organization member

This event has:
- ✅ Public visibility (visible to all)
- ❌ Members-only event type

**Expected NextStep:** BECOME_MEMBER
**Expected Reason:** Only members are allowed
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 4: Private event (requires invitation)
        self.events["private"] = events_models.Event.objects.create(
            organization=self.org,
            name="🔒 Private Event (Invitation Required)",
            slug="test-private-event",
            event_type=events_models.Event.Types.PRIVATE,
            visibility=events_models.Event.Visibility.PUBLIC,  # Visible but requires invitation
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=45),
            end=self.now + timedelta(days=45, hours=3),
            description="""# 🔒 Private Event (Invitation Required)

**Test Scenario:** InvitationGate - Must have valid invitation

This event has:
- ✅ Public visibility (visible to all)
- ❌ Private event type (invitation required)

**Expected NextStep:** REQUEST_INVITATION
**Expected Reason:** Requires invitation
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 5: Event at capacity (full)
        self.events["full"] = events_models.Event.objects.create(
            organization=self.org,
            name="🚫 Event at Full Capacity",
            slug="test-full-capacity-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=50),
            end=self.now + timedelta(days=50, hours=3),
            max_attendees=10,  # Will be filled in _create_relationships
            waitlist_open=True,
            description="""# 🚫 Event at Full Capacity

**Test Scenario:** AvailabilityGate - Event is full

This event has:
- ✅ Public visibility
- ❌ Max attendees reached (10/10)
- ✅ Waitlist available

**Expected NextStep:** JOIN_WAITLIST
**Expected Reason:** Event is full
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 6: RSVP deadline passed
        self.events["rsvp_deadline"] = events_models.Event.objects.create(
            organization=self.org,
            name="⏰ RSVP Deadline Passed",
            slug="test-rsvp-deadline-passed",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=10),
            end=self.now + timedelta(days=10, hours=3),
            rsvp_before=self.now - timedelta(hours=1),  # Deadline already passed
            description="""# ⏰ RSVP Deadline Passed

**Test Scenario:** RSVPDeadlineGate - RSVP period ended

This event has:
- ✅ Public visibility
- ❌ RSVP deadline has passed

**Expected NextStep:** None
**Expected Reason:** The RSVP deadline has passed
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 7: Tickets not yet on sale
        self.events["tickets_not_on_sale"] = events_models.Event.objects.create(
            organization=self.org,
            name="🎟️ Tickets Not Yet On Sale",
            slug="test-tickets-not-on-sale",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=True,
            start=self.now + timedelta(days=60),
            end=self.now + timedelta(days=60, hours=3),
            description="""# 🎟️ Tickets Not Yet On Sale

**Test Scenario:** TicketSalesGate - Sales haven't started

This event has:
- ✅ Public visibility
- ❌ Ticket sales start in the future

**Expected NextStep:** None
**Expected Reason:** Tickets are not currently on sale
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 8: Draft event (not open)
        self.events["draft"] = events_models.Event.objects.create(
            organization=self.org,
            name="📝 Draft Event (Not Yet Open)",
            slug="test-draft-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.DRAFT,
            city=self.city,
            requires_ticket=False,
            start=self.now + timedelta(days=90),
            end=self.now + timedelta(days=90, hours=3),
            description="""# 📝 Draft Event (Not Yet Open)

**Test Scenario:** EventStatusGate - Event not yet open

This event has:
- ✅ Public visibility
- ❌ Status: DRAFT

**Expected NextStep:** WAIT_FOR_EVENT_TO_OPEN
**Expected Reason:** Event is not open
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 9: Past event (finished)
        self.events["finished"] = events_models.Event.objects.create(
            organization=self.org,
            name="⏹️ Past Event (Finished)",
            slug="test-finished-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.CLOSED,
            city=self.city,
            requires_ticket=False,
            start=self.now - timedelta(days=7),
            end=self.now - timedelta(days=6),
            description="""# ⏹️ Past Event (Finished)

**Test Scenario:** EventStatusGate - Event already ended

This event has:
- ✅ Public visibility
- ❌ Event end time in the past

**Expected NextStep:** None
**Expected Reason:** Event has finished
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 10: Requires ticket purchase
        self.events["requires_ticket"] = events_models.Event.objects.create(
            organization=self.org,
            name="🎫 Event Requires Ticket Purchase",
            slug="test-requires-ticket",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=True,
            start=self.now + timedelta(days=55),
            end=self.now + timedelta(days=55, hours=3),
            description="""# 🎫 Event Requires Ticket Purchase

**Test Scenario:** User must purchase ticket to attend

This event has:
- ✅ Public visibility
- ✅ Tickets on sale
- ❌ Requires ticket purchase

**Expected NextStep:** PURCHASE_TICKET
**Expected Reason:** Requires purchase
""",
            address="Test Venue, Vienna, Austria",
        )

        # Event 11: Sold out ticketed event
        self.events["sold_out"] = events_models.Event.objects.create(
            organization=self.org,
            name="💸 Sold Out Event",
            slug="test-sold-out-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=self.city,
            requires_ticket=True,
            start=self.now + timedelta(days=65),
            end=self.now + timedelta(days=65, hours=3),
            max_attendees=5,  # Small capacity
            waitlist_open=True,
            description="""# 💸 Sold Out Event

**Test Scenario:** AvailabilityGate - All tickets sold

This event has:
- ✅ Public visibility
- ❌ All tickets sold (5/5)
- ✅ Waitlist available

**Expected NextStep:** JOIN_WAITLIST
**Expected Reason:** Sold out
""",
            address="Test Venue, Vienna, Austria",
        )

        logger.info(f"Created {len(self.events)} test events")

    def _create_ticket_tiers(self) -> None:
        """Create ticket tiers for ticketed events."""
        logger.info("Creating ticket tiers...")

        # Tickets not yet on sale event
        events_models.TicketTier.objects.filter(
            event=self.events["tickets_not_on_sale"], name="General Admission"
        ).update(
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("50.00"),
            currency="EUR",
            total_quantity=100,
            quantity_sold=0,
            sales_start_at=self.now + timedelta(days=30),  # Future start date
            sales_end_at=self.now + timedelta(days=59),
            description="Tickets on sale starting in 30 days",
        )

        # Requires ticket event (tickets currently on sale)
        events_models.TicketTier.objects.filter(event=self.events["requires_ticket"], name="General Admission").update(
            name="General Admission",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("25.00"),
            currency="EUR",
            total_quantity=50,
            quantity_sold=10,
            sales_start_at=self.now - timedelta(days=1),
            sales_end_at=self.now + timedelta(days=54),
            description="Currently on sale",
        )

        # Sold out event
        events_models.TicketTier.objects.filter(event=self.events["sold_out"], name="General Admission").update(
            name="General Admission",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("30.00"),
            currency="EUR",
            total_quantity=5,
            quantity_sold=5,  # Sold out
            sales_start_at=self.now - timedelta(days=5),
            sales_end_at=self.now + timedelta(days=64),
            description="SOLD OUT",
        )

        logger.info("Created ticket tiers")

    def _create_relationships(self) -> None:
        """Create relationships to simulate full events."""
        logger.info("Creating test relationships...")

        # Fill up the "full" event with RSVPs
        # Create dummy users and RSVP them
        for i in range(10):
            dummy_user = RevelUser.objects.create_user(
                username=f"dummy{i}@test.com",
                password="password123",
                email=f"dummy{i}@test.com",
            )
            events_models.EventRSVP.objects.create(
                event=self.events["full"],
                user=dummy_user,
                status=events_models.EventRSVP.Status.YES,
            )

        # Create tickets for sold out event
        sold_out_tier = events_models.TicketTier.objects.get(event=self.events["sold_out"], name="General Admission")

        for i in range(5):
            ticket_user = RevelUser.objects.create_user(
                username=f"ticketholder{i}@test.com",
                password="password123",
                email=f"ticketholder{i}@test.com",
            )
            events_models.Ticket.objects.create(
                event=self.events["sold_out"],
                user=ticket_user,
                tier=sold_out_tier,
                status=events_models.Ticket.Status.ACTIVE,
            )

        # Give the member user an invitation to the private event
        # (so they can see the difference in eligibility)
        events_models.EventInvitation.objects.create(
            event=self.events["private"],
            user=self.member_user,
            waives_questionnaire=False,
            waives_purchase=False,
            custom_message="You're invited to test the invitation gate!",
        )

        logger.info("Created test relationships")
