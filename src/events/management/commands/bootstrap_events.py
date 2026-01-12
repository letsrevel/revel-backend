# src/events/management/commands/bootstrap_events.py

import typing as t
from datetime import timedelta
from decimal import Decimal

import structlog
from decouple import config
from django.core.management.base import BaseCommand
from django.utils import timezone
from faker import Faker

from accounts.models import (
    DietaryPreference,
    DietaryRestriction,
    FoodItem,
    RevelUser,
    UserDietaryPreference,
)
from common.models import Tag
from events import models as events_models
from geo.models import City
from questionnaires import models as questionnaires_models

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Bootstrap comprehensive example data for events, organizations, and questionnaires.

    This command creates a realistic dataset for frontend development and testing,
    including multiple organizations, diverse events, users with various relationships,
    tags, potluck items, questionnaires, and more.
    """

    help = "Bootstrap comprehensive example data for events, organizations, and questionnaires."

    def __init__(self) -> None:
        """Initialize the command with storage for created objects."""
        super().__init__()
        self.fake = Faker("en_US")
        self.users: dict[str, RevelUser] = {}
        self.orgs: dict[str, events_models.Organization] = {}
        self.venues: dict[str, events_models.Venue] = {}
        self.events: dict[str, events_models.Event] = {}
        self.tags: dict[str, Tag] = {}
        self.cities: dict[str, City] = {}

    def handle(self, *args: t.Any, **options: t.Any) -> None:  # pragma: no cover
        """Bootstrap example data."""
        logger.info("Starting bootstrap process...")

        # Load cities
        self._load_cities()

        # Create tags
        self._create_tags()

        # Create users
        self._create_users()

        # Create organizations
        self._create_organizations()

        # Create venues
        self._create_venues()

        # Create event series
        self._create_event_series()

        # Create events
        self._create_events()

        # Create ticket tiers
        self._create_ticket_tiers()

        # Create potluck items
        self._create_potluck_items()

        # Create user relationships
        self._create_user_relationships()

        # Create dietary data
        self._create_dietary_data()

        # Create questionnaires
        self._create_questionnaires()

        logger.info("Bootstrap complete! See README.md in this directory for details.")

    def _load_cities(self) -> None:
        """Load cities for events."""
        self.cities["vienna"] = City.objects.get(name="Vienna", country="Austria")
        new_york = City.objects.filter(name="New York", country="United States").first()
        london = City.objects.filter(name="London", country="United Kingdom").first()
        berlin = City.objects.filter(name="Berlin", country="Germany").first()
        tokyo = City.objects.filter(name="Tokyo", country="Japan").first()

        assert new_york is not None, "New York city not found"
        assert london is not None, "London city not found"
        assert berlin is not None, "Berlin city not found"
        assert tokyo is not None, "Tokyo city not found"

        self.cities["new_york"] = new_york
        self.cities["london"] = london
        self.cities["berlin"] = berlin
        self.cities["tokyo"] = tokyo

    def _fake_address(self) -> str:
        """Generate a clean fake address."""
        return " ".join(self.fake.address().split())

    def _create_tags(self) -> None:
        """Create a comprehensive tag taxonomy."""
        logger.info("Creating tags...")

        # Category tags
        category_tags = {
            "music": {"description": "Music-related events", "color": "#FF6B6B"},
            "food": {"description": "Food and dining events", "color": "#4ECDC4"},
            "workshop": {"description": "Educational workshops", "color": "#45B7D1"},
            "conference": {"description": "Professional conferences", "color": "#96CEB4"},
            "networking": {"description": "Networking events", "color": "#FFEAA7"},
            "arts": {"description": "Arts and culture", "color": "#DDA15E"},
            "tech": {"description": "Technology events", "color": "#6C5CE7"},
            "sports": {"description": "Sports and fitness", "color": "#00B894"},
            "wellness": {"description": "Health and wellness", "color": "#FDCB6E"},
            "community": {"description": "Community gatherings", "color": "#E17055"},
        }

        # Vibe tags
        vibe_tags = {
            "casual": {"description": "Relaxed atmosphere", "color": "#74B9FF"},
            "formal": {"description": "Formal dress code", "color": "#2D3436"},
            "educational": {"description": "Learning focused", "color": "#00CEC9"},
            "social": {"description": "Social interaction", "color": "#FD79A8"},
            "professional": {"description": "Professional setting", "color": "#636E72"},
        }

        all_tags = {**category_tags, **vibe_tags}

        for tag_name, tag_data in all_tags.items():
            tag, created = Tag.objects.get_or_create(
                name=tag_name,
                defaults={
                    "description": tag_data["description"],
                    "color": tag_data["color"],
                },
            )
            self.tags[tag_name] = tag

        logger.info(f"Created {len(self.tags)} tags")

    def _create_users(self) -> None:
        """Create a diverse pool of users with different roles."""
        logger.info("Creating users...")

        user_data = [
            # Organization Alpha
            ("alice.owner@example.com", "Alice Owner", "org_alpha_owner"),
            ("bob.staff@example.com", "Bob Staff", "org_alpha_staff"),
            ("charlie.member@example.com", "Charlie Member", "org_alpha_member"),
            # Organization Beta
            ("diana.owner@example.com", "Diana Owner", "org_beta_owner"),
            ("eve.staff@example.com", "Eve Staff", "org_beta_staff"),
            ("frank.member@example.com", "Frank Member", "org_beta_member"),
            # Regular attendees
            ("george.attendee@example.com", "George Attendee", "attendee_1"),
            ("hannah.attendee@example.com", "Hannah Attendee", "attendee_2"),
            ("ivan.attendee@example.com", "Ivan Attendee", "attendee_3"),
            ("julia.attendee@example.com", "Julia Attendee", "attendee_4"),
            # Multi-org user
            ("karen.multiorg@example.com", "Karen Multi", "multi_org_user"),
            # Pending/invited users
            ("leo.pending@example.com", "Leo Pending", "pending_user"),
            ("maria.invited@example.com", "Maria Invited", "invited_user"),
        ]

        for email, full_name, key in user_data:
            user = RevelUser.objects.create_user(
                username=email,
                password="password123",
                email=email,
                email_verified=True,
            )
            # Set first/last name if possible
            name_parts = full_name.split()
            if len(name_parts) >= 2:
                user.first_name = name_parts[0]
                user.last_name = " ".join(name_parts[1:])
                user.save()

            self.users[key] = user

        logger.info(f"Created {len(self.users)} users")

    def _create_organizations(self) -> None:
        """Create multiple organizations with varied configurations."""
        logger.info("Creating organizations...")

        # Organization Alpha - Public organization with Stripe Connect
        org_alpha = events_models.Organization.objects.create(
            name="Revel Events Collective",
            slug="revel-events-collective",
            owner=self.users["org_alpha_owner"],
            visibility=events_models.Organization.Visibility.PUBLIC,
            description="""# Revel Events Collective

We're a vibrant community dedicated to bringing people together through unforgettable experiences.
From intimate gatherings to large-scale celebrations, we create events that spark joy, foster
connections, and celebrate life's special moments.

## Our Mission
To transform ordinary moments into extraordinary memories through thoughtfully curated events
that bring communities together.

## What We Do
- Music and cultural events
- Community workshops
- Seasonal celebrations
- Private gatherings
""",
            city=self.cities["vienna"],
            stripe_account_id=config("CONNECTED_TEST_STRIPE_ID", default=None),
            stripe_charges_enabled=True,
            stripe_details_submitted=True,
        )
        org_alpha.staff_members.add(self.users["org_alpha_staff"])

        # Add members with default tier
        default_tier_alpha = events_models.MembershipTier.objects.get(organization=org_alpha, name="General membership")
        for user in [self.users["org_alpha_member"], self.users["multi_org_user"]]:
            events_models.OrganizationMember.objects.create(organization=org_alpha, user=user, tier=default_tier_alpha)

        org_alpha.add_tags("community", "music", "arts")

        # Update organization settings
        org_alpha.accept_membership_requests = True
        org_alpha.contact_email = "hello@revelcollective.example.com"
        org_alpha.contact_email_verified = True
        org_alpha.save()

        self.orgs["alpha"] = org_alpha

        # Organization Beta - Members-only organization
        org_beta = events_models.Organization.objects.create(
            name="Tech Innovators Network",
            slug="tech-innovators-network",
            owner=self.users["org_beta_owner"],
            visibility=events_models.Organization.Visibility.PUBLIC,
            description="""# Tech Innovators Network

An exclusive community for tech professionals, entrepreneurs, and innovators. Join us for
cutting-edge workshops, networking events, and knowledge-sharing sessions.

## Membership Benefits
- Access to exclusive tech workshops and conferences
- Networking with industry leaders
- Early access to product launches and beta programs
- Members-only online resources and forums

## Join Us
Membership is by invitation or application review. We're looking for passionate technologists
who want to shape the future.
""",
            city=self.cities["berlin"],
        )
        org_beta.staff_members.add(self.users["org_beta_staff"])

        # Add members with default tier
        default_tier_beta = events_models.MembershipTier.objects.get(organization=org_beta, name="General membership")
        for user in [self.users["org_beta_member"], self.users["multi_org_user"], self.users["attendee_1"]]:
            events_models.OrganizationMember.objects.create(organization=org_beta, user=user, tier=default_tier_beta)

        org_beta.add_tags("tech", "professional", "networking")

        # Update organization settings
        org_beta.accept_membership_requests = True
        org_beta.contact_email = "info@techinnovators.example.com"
        org_beta.contact_email_verified = True
        org_beta.save()

        self.orgs["beta"] = org_beta

        logger.info(f"Created {len(self.orgs)} organizations")

    def _create_venues(self) -> None:
        """Create venues with sectors and seats for seated events."""
        logger.info("Creating venues...")

        # Create a venue for Revel Events Collective (org_alpha)
        concert_hall = events_models.Venue.objects.create(
            organization=self.orgs["alpha"],
            name="Revel Concert Hall",
            slug="revel-concert-hall",
            description="A modern concert venue with flexible seating arrangements.",
            city=self.cities["vienna"],
            address="Musikvereinsplatz 1, 1010 Vienna, Austria",
            capacity=100,
        )
        self.venues["concert_hall"] = concert_hall

        # Create a sector for the main floor
        main_floor = events_models.VenueSector.objects.create(
            venue=concert_hall,
            name="Main Floor",
            code="MF",
            capacity=100,
            display_order=1,
        )

        # Create 100 seats in a 10x10 grid
        # Rows A-J (10 rows), seats 1-10 per row
        # Positions are simple incrementals: (row, col) -> (0,0), (0,1)...(9,9)
        seats_to_create = []
        row_labels = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
        for row_idx, row_label in enumerate(row_labels):
            for seat_num in range(1, 11):  # Seats 1-10
                col_idx = seat_num - 1
                seats_to_create.append(
                    events_models.VenueSeat(
                        sector=main_floor,
                        label=f"{row_label}{seat_num}",
                        row=row_label,
                        number=seat_num,
                        position={"x": col_idx, "y": row_idx},
                        is_accessible=(row_label == "A"),  # First row is accessible
                        is_obstructed_view=False,
                        is_active=True,
                    )
                )
        events_models.VenueSeat.objects.bulk_create(seats_to_create)

        logger.info(f"Created {len(self.venues)} venues with sectors and seats")

    def _create_event_series(self) -> None:
        """Create event series for recurring events."""
        logger.info("Creating event series...")

        # Monthly Tech Meetup Series
        tech_series = events_models.EventSeries.objects.create(
            organization=self.orgs["beta"],
            name="Monthly Tech Talks",
            slug="monthly-tech-talks",
            description="""# Monthly Tech Talks

Join us every month for inspiring talks from industry leaders, hands-on workshops,
and networking with fellow tech enthusiasts.

Each session features:
- 1-2 keynote presentations
- Lightning talks from community members
- Networking session with refreshments
- Q&A with speakers
""",
        )
        tech_series.add_tags("tech", "educational", "networking")

        # Community Potluck Series
        potluck_series = events_models.EventSeries.objects.create(
            organization=self.orgs["alpha"],
            name="Seasonal Community Gatherings",
            slug="seasonal-community-gatherings",
            description="""# Seasonal Community Gatherings

Celebrating the seasons together with potluck dinners, music, and community bonding.
Bring a dish to share and join us for an evening of connection and celebration!
""",
        )
        potluck_series.add_tags("food", "community", "casual")

        self.event_series = {
            "tech_talks": tech_series,
            "potlucks": potluck_series,
        }

        logger.info(f"Created {len(self.event_series)} event series")

    def _create_events(self) -> None:
        """Create diverse, realistic events."""
        logger.info("Creating events...")

        now = timezone.now()

        # Event 1: Summer Music Festival (Public, Ticketed, Open, Future)
        summer_festival = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Summer Sunset Music Festival",
            slug="summer-sunset-music-festival",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["vienna"],
            requires_ticket=True,
            start=now + timedelta(days=45),
            end=now + timedelta(days=45, hours=8),
            max_attendees=500,
            waitlist_open=True,
            description="""# Summer Sunset Music Festival 🎵

Join us for an unforgettable evening of music under the stars! Experience the magic of live
performances from local and touring artists as the sun sets over the city.

## Featured Artists
- **The Midnight Riders** - Indie Rock
- **Sarah Chen & The Harmonics** - Jazz Fusion
- **DJ Nova** - Electronic/Dance
- **Acoustic Soul Collective** - Soul/R&B

## Event Highlights
- 🎪 Multiple stages with diverse music genres
- 🍔 Gourmet food trucks and local vendors
- 🍺 Craft beer and cocktail garden
- 🎨 Live art installations and performances
- 📸 Instagram-worthy sunset views

## Schedule
- **5:00 PM** - Gates open, food & vendors
- **6:00 PM** - Opening acts begin
- **7:30 PM** - Sunset session with DJ Nova
- **9:00 PM** - Headliner performances
- **11:00 PM** - After-party with DJ set
- **1:00 AM** - Event concludes

## What to Bring
- Blanket or lawn chair for seating
- Valid ID for alcohol service
- Weather-appropriate clothing
- Good vibes and dancing shoes!

## Venue Info
Donauinsel - Stunning views along the Danube, accessible via U1 (Donauinsel station).
Limited parking available at Copa Cagrana.

**Rain or Shine Event** - Event will proceed in light rain. Covered areas available.
""",
            address="Donauinsel, 1220 Vienna, Austria",
            check_in_starts_at=now + timedelta(days=45, hours=-1),
            check_in_ends_at=now + timedelta(days=45, hours=7),
        )
        summer_festival.add_tags("music", "casual", "community")
        self.events["summer_festival"] = summer_festival

        # Event 2: Exclusive Wine Tasting (Private, Ticketed, Open, Future)
        wine_tasting = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Exclusive Wine Tasting & Pairing Dinner",
            slug="exclusive-wine-tasting-dinner",
            event_type=events_models.Event.EventType.PRIVATE,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["vienna"],
            requires_ticket=True,
            start=now + timedelta(days=30),
            end=now + timedelta(days=30, hours=4),
            max_attendees=40,
            accept_invitation_requests=True,
            apply_before=now + timedelta(days=27),  # Application deadline: 3 days before event
            description="""# Exclusive Wine Tasting & Pairing Dinner

An intimate evening curated for wine enthusiasts. Join acclaimed sommelier Marcus Rodriguez
for a journey through rare vintages paired with a five-course tasting menu by Chef Elena Martinez.

## Wine Selection
**Featured Regions:** Bordeaux, Tuscany, Wachau Valley, Burgenland

Each course features carefully selected wines paired with seasonal ingredients:

1. **Amuse-Bouche** - Austrian Sekt Brut Nature
2. **First Course** - Grüner Veltliner with Seared Scallops
3. **Second Course** - Blaufränkisch with Duck Confit
4. **Main Course** - Super Tuscan with Herb-Crusted Lamb
5. **Dessert** - Eiswein with Chocolate Torte

## Your Sommelier
Marcus Rodriguez brings 20 years of experience from Michelin-starred restaurants. His passion
for storytelling brings each wine's journey from vineyard to glass to life.

## Dress Code
Smart casual to business casual

## Exclusive Perks
- Take-home tasting notes
- 20% discount on featured wines
- Access to private wine club membership
- Recipe cards from Chef Martinez

*Limited to 40 guests for an intimate experience*
""",
            address="Steirereck, Am Heumarkt 2A, 1030 Vienna, Austria",
        )
        wine_tasting.add_tags("food", "formal", "social")
        self.events["wine_tasting"] = wine_tasting

        # Event 3: Tech Workshop (Members-only, Free, Open, Future)
        tech_workshop = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="Hands-on Workshop: Building with AI APIs",
            slug="ai-apis-workshop",
            event_type=events_models.Event.EventType.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["berlin"],
            requires_ticket=False,
            start=now + timedelta(days=20),
            end=now + timedelta(days=20, hours=3),
            max_attendees=30,
            rsvp_before=now + timedelta(days=18),
            description="""# Hands-on Workshop: Building with AI APIs

Learn to integrate cutting-edge AI capabilities into your applications. This practical workshop
covers modern AI APIs including OpenAI, Anthropic Claude, and open-source alternatives.

## What You'll Learn
- **API Integration Basics** - Authentication, rate limiting, error handling
- **Prompt Engineering** - Writing effective prompts for different use cases
- **Streaming Responses** - Real-time AI interactions
- **Cost Optimization** - Managing API usage and costs
- **Best Practices** - Security, testing, and production considerations

## Prerequisites
- Intermediate programming experience (Python or JavaScript)
- Laptop with development environment set up
- API keys (we'll provide test credits)

## Schedule
- **10:00 AM** - Introduction & Setup
- **10:30 AM** - API Integration Deep Dive
- **12:00 PM** - Lunch Break (provided)
- **1:00 PM** - Hands-on Project Work
- **2:30 PM** - Show & Tell + Q&A

## Your Instructor
**Dr. Sarah Chen** - AI Research Lead with 10+ years in ML/AI. Published researcher and
consultant for Fortune 500 companies.

## What We Provide
- Lunch and refreshments
- Sample code and templates
- API credits for practice
- Certificate of completion

**Members only** - Not a member yet? [Join Tech Innovators Network](/join)
""",
            address="TechHub Berlin, Mehringdamm 33, 10961 Berlin, Germany",
        )
        tech_workshop.add_tags("tech", "workshop", "educational")
        self.events["tech_workshop"] = tech_workshop

        # Event 4: Community Potluck (Public, No ticket, RSVP, Open, Future)
        spring_potluck = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Spring Community Potluck & Garden Party",
            slug="spring-community-potluck",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            event_series=self.event_series["potlucks"],
            city=self.cities["vienna"],
            requires_ticket=False,
            potluck_open=True,
            start=now + timedelta(days=15),
            end=now + timedelta(days=15, hours=5),
            rsvp_before=now + timedelta(days=13),
            max_attendees=80,
            description="""# Spring Community Potluck & Garden Party 🌸

Celebrate the arrival of spring with neighbors, friends, and community members! Bring a dish
to share and enjoy an afternoon of food, games, and connection in the garden.

## Event Activities
- 🍽️ **Potluck Feast** - International dishes from our diverse community
- 🎵 **Live Music** - Acoustic performances throughout the afternoon
- 🎮 **Lawn Games** - Kubb, frisbee, badminton
- 👶 **Kids Corner** - Face painting, crafts, and activities
- 🌱 **Plant Swap** - Bring cuttings to share!

## Potluck Guidelines
Please bring a dish that serves 6-8 people. We need:
- Main courses & side dishes
- Salads and appetizers
- Desserts and beverages
- Vegetarian/vegan options especially welcome!

Sign up for what you'll bring using the potluck signup below.

## What to Bring
- Your potluck contribution (with serving utensils)
- Blanket or folding chairs
- Reusable plates/utensils (eco-friendly event!)
- Plant cuttings for the swap table
- Outdoor games welcome!

## Accessibility
The park is wheelchair accessible. Restrooms and covered seating available.
Service animals welcome.

**Family-Friendly** - All ages welcome! Alcohol-free event.

*RSVP required for headcount planning*
""",
            address="Augarten, Obere Augartenstraße, 1020 Vienna, Austria",
            location_maps_url="https://maps.app.goo.gl/HLZE9e3mzrxZBoyR8",
            location_maps_embed=(
                "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d9387.125669096846"
                "!2d16.374930297504203!3d48.22007689713033!2m3!1f0!2f0!3f0!3m2!1i1024!2i768"
                "!4f13.1!3m3!1m2!1s0x476d07a8bcc2f5cf%3A0x1cf8f8c0a86e2304!2sAugartenspitz"
                "!5e1!3m2!1sen!2sat!4v1768233815811!5m2!1sen!2sat"
            ),
        )
        spring_potluck.add_tags("food", "community", "casual")
        self.events["spring_potluck"] = spring_potluck

        # Event 5: Tech Conference (Public, Ticketed, Open, Future)
        tech_conference = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="FutureStack 2025: AI & Web3 Conference",
            slug="futurestack-2025",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["berlin"],
            requires_ticket=True,
            start=now + timedelta(days=60),
            end=now + timedelta(days=62),
            max_attendees=1000,
            waitlist_open=True,
            apply_before=now + timedelta(days=57),  # Application deadline: 3 days before event
            description="""# FutureStack 2025: AI & Web3 Conference

Three days of cutting-edge tech insights, hands-on workshops, and networking with 1000+
developers, founders, and tech leaders from around the world.

## Conference Themes
🤖 **Artificial Intelligence** - LLMs, ML Ops, AI Safety
🔗 **Web3 & Blockchain** - DeFi, NFTs, DAOs
☁️ **Cloud Native** - Kubernetes, Serverless, Edge Computing
🔒 **Security** - Zero Trust, Supply Chain Security

## Keynote Speakers
- **Dr. Yuna Kim** - VP of AI Research, TechCorp
- **Marcus Johnson** - Founder & CEO, BlockChain Ventures
- **Sofia Rodriguez** - CISO, Fortune 500 Company
- **+ 40 more speakers**

## Event Format
- **Day 1** - Keynotes & Track Sessions
- **Day 2** - Workshops & Unconference
- **Day 3** - Hackathon & Closing Party

## Tracks
1. AI/ML Engineering
2. Blockchain & Decentralization
3. DevOps & Infrastructure
4. Security & Privacy
5. Product & Startup

## Workshops (Separate Registration)
- Building Production LLM Apps
- Smart Contract Development
- Kubernetes Deep Dive
- Penetration Testing Fundamentals

## Networking Events
- Opening reception with local craft beer
- Sponsor expo hall
- Evening social events
- Lightning talks & demos

## Conference Perks
✅ All meals and refreshments
✅ Conference swag bag
✅ Access to recordings (60 days)
✅ Workshop materials
✅ Networking app access

**Early Bird pricing ends in 2 weeks!**
""",
            address="Berlin Congress Center, Alexanderplatz 5, 10178 Berlin, Germany",
            check_in_starts_at=now + timedelta(days=60, hours=-2),
            check_in_ends_at=now + timedelta(days=62, hours=10),
        )
        tech_conference.add_tags("tech", "conference", "professional", "networking")
        self.events["tech_conference"] = tech_conference

        # Event 6: Yoga & Wellness (Public, Ticketed, Open, Future)
        wellness_retreat = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Weekend Wellness Retreat",
            slug="weekend-wellness-retreat",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["vienna"],
            requires_ticket=True,
            start=now + timedelta(days=35),
            end=now + timedelta(days=37),
            max_attendees=25,
            description="""# Weekend Wellness Retreat 🧘‍♀️

Escape the city for a transformative weekend of yoga, meditation, nourishing food,
and deep relaxation in the beautiful Austrian countryside.

## Daily Schedule

### Friday Evening
- 6:00 PM - Arrival & Welcome Tea
- 7:00 PM - Light Dinner
- 8:30 PM - Opening Circle & Intention Setting
- 9:30 PM - Restorative Yoga & Meditation

### Saturday
- 7:00 AM - Sunrise Meditation
- 8:00 AM - Energizing Vinyasa Flow
- 9:30 AM - Wholesome Breakfast
- 11:00 AM - Nature Walk & Mindfulness Practice
- 1:00 PM - Farm-to-Table Lunch
- 3:00 PM - Workshop: Breathwork & Stress Release
- 5:00 PM - Yin Yoga & Sound Bath
- 7:00 PM - Dinner
- 8:30 PM - Evening Circle & Journaling

### Sunday
- 7:00 AM - Meditation & Gentle Flow
- 8:30 AM - Farewell Brunch
- 10:00 AM - Closing Circle
- 11:00 AM - Departure

## What's Included
✨ 2 nights accommodation (shared rooms)
✨ All meals (vegetarian/vegan)
✨ Yoga & meditation sessions
✨ Workshops & activities
✨ Welcome gift bag

## What to Bring
- Yoga mat (or rent on-site for €5)
- Comfortable clothing
- Journal & pen
- Hiking shoes
- Reusable water bottle
- Open heart & mind

## Your Guides
**Lisa Hartmann** - 500hr RYT, Meditation Teacher
**Stefan Mueller** - Breathwork Facilitator, Sound Healer

**Space is limited to 25 participants** for an intimate experience.
""",
            address="Wachau Valley Retreat Center, Dürnstein, 3601 Lower Austria",
        )
        wellness_retreat.add_tags("wellness", "casual", "social")
        self.events["wellness_retreat"] = wellness_retreat

        # Event 7: Networking Happy Hour (Members-only, Free, Open, Future)
        networking_event = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="Tech Founders Networking Happy Hour",
            slug="tech-founders-happy-hour",
            event_type=events_models.Event.EventType.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["berlin"],
            requires_ticket=False,
            start=now + timedelta(days=10),
            end=now + timedelta(days=10, hours=3),
            rsvp_before=now + timedelta(days=9),
            max_attendees=50,
            description="""# Tech Founders Networking Happy Hour

Connect with fellow founders, share challenges, celebrate wins, and build meaningful
relationships over drinks and appetizers.

## Who Should Attend
- Startup founders (pre-seed to Series B)
- Technical co-founders
- Solo founders & indie hackers
- Aspiring entrepreneurs

## Agenda
- **6:00 PM** - Doors open, mingle & drinks
- **6:30 PM** - Welcome & quick intros
- **6:45 PM** - Structured networking (speed networking rounds)
- **7:30 PM** - Open networking & socializing
- **8:30 PM** - Lightning pitches (optional, 1 min each)
- **9:00 PM** - Wind down

## Discussion Topics
- Fundraising experiences
- Co-founder dynamics
- Product-market fit
- Work-life balance as a founder
- Technical challenges

## Format
This is a casual, supportive environment. No sales pitches, just genuine connection and
knowledge sharing among peers who understand the founder journey.

**Complimentary drinks and appetizers provided.**

*Members only - Invitation required*
""",
            address="Founders Loft, Rosenthaler Str. 40, 10178 Berlin, Germany",
        )
        networking_event.add_tags("networking", "professional", "tech")
        self.events["networking_event"] = networking_event

        # Event 8: Art Gallery Opening (Public, Free, Open, Future)
        art_opening = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Contemporary Art Exhibition Opening",
            slug="contemporary-art-exhibition",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["london"],
            requires_ticket=False,
            start=now + timedelta(days=25),
            end=now + timedelta(days=25, hours=4),
            rsvp_before=now + timedelta(days=23),
            description="""# Contemporary Art Exhibition Opening

**"Metamorphosis: Digital Meets Traditional"**

Celebrate the opening of our latest exhibition featuring emerging artists who blend digital
technology with classical techniques.

## Featured Artists
- **Amara Johnson** - Interactive light installations
- **Kenji Tanaka** - AI-generated traditional paintings
- **Sofia Ramirez** - Mixed media sculptures
- **+ 8 more artists**

## Opening Night Program
- **6:00 PM** - Gallery doors open
- **7:00 PM** - Artist talks & Q&A
- **8:00 PM** - Live performance art piece
- **9:00 PM** - DJ set & socializing
- **10:00 PM** - Event concludes

## Exhibition Details
The exhibition runs for 6 weeks following the opening. Gallery hours: Tue-Sun, 11am-7pm.

## Refreshments
Wine, beer, and canapés served throughout the evening.

## Accessibility
The gallery is fully wheelchair accessible. ASL interpreter available upon request
(please email 48hrs in advance).

**Free admission - RSVP appreciated for catering purposes**
""",
            address="Shoreditch Gallery Space, 88 Brick Lane, London E1 6RL, UK",
        )
        art_opening.add_tags("arts", "social", "casual")
        self.events["art_opening"] = art_opening

        # Event 9: Past event (for testing)
        past_event = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="New Year's Eve Gala 2024",
            slug="nye-gala-2024",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.CLOSED,
            city=self.cities["vienna"],
            requires_ticket=True,
            start=now - timedelta(days=90),
            end=now - timedelta(days=89),
            max_attendees=200,
            description="""# New Year's Eve Gala 2024

A magical night to remember! Ring in the new year with elegance, entertainment, and celebration.

Thank you to everyone who joined us for this unforgettable evening!
""",
            address="Palais Ferstel, Strauchgasse 4, 1010 Vienna, Austria",
        )
        past_event.add_tags("social", "formal")
        self.events["past_event"] = past_event

        # Event 10: Draft event (for testing staff/owner views)
        draft_event = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="Future Tech Summit (Planning Phase)",
            slug="future-tech-summit-draft",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.DRAFT,
            city=self.cities["tokyo"],
            requires_ticket=True,
            start=now + timedelta(days=180),
            end=now + timedelta(days=182),
            description="""# Future Tech Summit - Coming Soon!

We're planning something big. Stay tuned for details!

*This event is currently in draft status*
""",
            address="Tokyo International Forum, Tokyo, Japan",
        )
        draft_event.add_tags("tech", "conference")
        self.events["draft_event"] = draft_event

        # Event 11: Monthly tech talk (part of series)
        tech_talk_may = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="Tech Talk May: Scaling Microservices",
            slug="tech-talk-may-2025",
            event_type=events_models.Event.EventType.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            event_series=self.event_series["tech_talks"],
            city=self.cities["berlin"],
            requires_ticket=False,
            start=now + timedelta(days=40),
            end=now + timedelta(days=40, hours=2),
            rsvp_before=now + timedelta(days=38),
            max_attendees=60,
            description="""# Tech Talk May: Scaling Microservices

**Speaker:** James Chen, Senior Architect at CloudScale Inc.

## Talk Overview
Learn practical strategies for scaling microservices architectures from someone who's been
there. James will share lessons learned from scaling systems that handle millions of
requests per second.

## Topics Covered
- Service mesh architecture
- Database sharding strategies
- Caching layers and strategies
- Observability at scale
- Cost optimization

## Format
- 45 min presentation
- 30 min Q&A
- 45 min networking with pizza & drinks

**Members only** - Part of our Monthly Tech Talks series
""",
            address="TechHub Berlin, Mehringdamm 33, 10961 Berlin, Germany",
        )
        tech_talk_may.add_tags("tech", "educational", "networking")
        self.events["tech_talk_may"] = tech_talk_may

        # Event 12: Sold out event with waitlist
        sold_out_workshop = events_models.Event.objects.create(
            organization=self.orgs["beta"],
            name="Advanced Machine Learning Workshop",
            slug="advanced-ml-workshop",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["berlin"],
            requires_ticket=True,
            start=now + timedelta(days=28),
            end=now + timedelta(days=28, hours=6),
            max_attendees=20,
            waitlist_open=True,
            description="""# Advanced Machine Learning Workshop

**⚠️ SOLD OUT - Join Waitlist**

An intensive hands-on workshop covering advanced ML techniques including neural networks,
ensemble methods, and model optimization.

## Prerequisites
- Strong Python programming skills
- Understanding of basic ML concepts
- Laptop with 16GB+ RAM

## What You'll Build
A complete ML pipeline from data preprocessing to model deployment.

**Currently sold out - join the waitlist for cancellations or future dates**
""",
            address="TechHub Berlin, Mehringdamm 33, 10961 Berlin, Germany",
        )
        sold_out_workshop.add_tags("tech", "workshop", "educational")
        self.events["sold_out_workshop"] = sold_out_workshop

        # Event 13: Seated concert event (with venue and reserved seating)
        seated_concert = events_models.Event.objects.create(
            organization=self.orgs["alpha"],
            name="Classical Music Evening",
            slug="classical-music-evening",
            event_type=events_models.Event.EventType.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.EventStatus.OPEN,
            city=self.cities["vienna"],
            venue=self.venues["concert_hall"],
            requires_ticket=True,
            start=now + timedelta(days=50),
            end=now + timedelta(days=50, hours=3),
            max_attendees=100,
            description="""# Classical Music Evening 🎻

Join us for an enchanting evening of classical music at the Revel Concert Hall.
Experience masterpieces from Mozart, Beethoven, and Strauss performed by the
Vienna Chamber Orchestra.

## Program
- **Mozart** - Eine kleine Nachtmusik
- **Beethoven** - Symphony No. 5 (1st Movement)
- **Strauss** - The Blue Danube Waltz

## Venue
The Revel Concert Hall features reserved seating in a 10x10 grid layout.
Choose your preferred seat during checkout for the best viewing experience.

## Dress Code
Smart casual to formal attire recommended.

## Intermission
Wine and refreshments available during the 20-minute intermission.

*All seats are reserved - select your seat when purchasing tickets*
""",
            address="Musikvereinsplatz 1, 1010 Vienna, Austria",
            check_in_starts_at=now + timedelta(days=50, hours=-1),
            check_in_ends_at=now + timedelta(days=50, hours=1),
        )
        seated_concert.add_tags("music", "arts", "formal")
        self.events["seated_concert"] = seated_concert

        logger.info(f"Created {len(self.events)} events")

    def _create_ticket_tiers(self) -> None:
        """Create diverse ticket tiers for events."""
        logger.info("Creating ticket tiers...")

        now = timezone.now()

        # Delete auto-created default tiers for events that will have custom tiers
        # (except summer_festival which uses the default "General Admission" tier)
        events_with_custom_tiers = [
            self.events["wine_tasting"],
            self.events["tech_conference"],
            self.events["wellness_retreat"],
            self.events["past_event"],
            self.events["sold_out_workshop"],
            self.events["draft_event"],
            self.events["seated_concert"],
        ]
        events_models.TicketTier.objects.filter(
            event__in=events_with_custom_tiers,
            name=events_models.DEFAULT_TICKET_TIER_NAME,
        ).delete()

        # Summer Festival - Multiple tiers
        events_models.TicketTier.objects.create(
            event=self.events["summer_festival"],
            name="Early Bird General Admission",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("45.00"),
            currency="USD",
            total_quantity=200,
            quantity_sold=180,
            sales_start_at=now - timedelta(days=30),
            sales_end_at=now + timedelta(days=15),
            description="Early bird pricing - save $20!",
        )

        events_models.TicketTier.objects.filter(name="General Admission", event=self.events["summer_festival"]).update(
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("65.00"),
            currency="USD",
            total_quantity=250,
            quantity_sold=45,
            sales_start_at=now + timedelta(days=15),
            sales_end_at=now + timedelta(days=44),
            description="Standard admission ticket",
        )

        events_models.TicketTier.objects.create(
            event=self.events["summer_festival"],
            name="VIP Experience",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("150.00"),
            currency="USD",
            total_quantity=50,
            quantity_sold=12,
            sales_start_at=now - timedelta(days=30),
            sales_end_at=now + timedelta(days=44),
            description="""VIP perks include:
- Priority entry
- VIP lounge access with premium bar
- Meet & greet with artists
- Exclusive merchandise
- Premium viewing area
""",
        )

        # Wine Tasting - Invitation only tier
        events_models.TicketTier.objects.create(
            event=self.events["wine_tasting"],
            name="Exclusive Seating",
            visibility=events_models.TicketTier.Visibility.PRIVATE,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.INVITED,
            price=Decimal("200.00"),
            currency="USD",
            total_quantity=40,
            quantity_sold=8,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=29),
            description="Invitation-only exclusive wine tasting dinner",
        )

        # Tech Conference - Multiple tiers with different access levels
        events_models.TicketTier.objects.create(
            event=self.events["tech_conference"],
            name="Early Bird - Full Access",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("399.00"),
            currency="EUR",
            total_quantity=300,
            quantity_sold=295,
            sales_start_at=now - timedelta(days=60),
            sales_end_at=now + timedelta(days=5),
            description="Early bird rate - ends soon! Full 3-day access.",
        )

        events_models.TicketTier.objects.create(
            event=self.events["tech_conference"],
            name="Standard - Full Access",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("599.00"),
            currency="EUR",
            total_quantity=500,
            quantity_sold=120,
            sales_start_at=now + timedelta(days=5),
            sales_end_at=now + timedelta(days=59),
            description="Full 3-day conference access with all meals included.",
        )

        events_models.TicketTier.objects.create(
            event=self.events["tech_conference"],
            name="Workshop Bundle",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("899.00"),
            currency="EUR",
            total_quantity=100,
            quantity_sold=34,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=59),
            description="Conference + 2 workshops of your choice. Best value!",
        )

        events_models.TicketTier.objects.create(
            event=self.events["tech_conference"],
            name="Member Discount",
            visibility=events_models.TicketTier.Visibility.MEMBERS_ONLY,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.MEMBERS,
            price=Decimal("299.00"),
            currency="EUR",
            total_quantity=100,
            quantity_sold=23,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=59),
            description="Special member-only pricing - 50% off!",
        )

        # Wellness Retreat - PWYC tier
        events_models.TicketTier.objects.create(
            event=self.events["wellness_retreat"],
            name="Shared Room",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("250.00"),
            currency="EUR",
            total_quantity=20,
            quantity_sold=14,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=34),
            description="Shared accommodation (2 per room)",
        )

        events_models.TicketTier.objects.create(
            event=self.events["wellness_retreat"],
            name="Community Support Rate",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price_type=events_models.TicketTier.PriceType.PWYC,
            price=Decimal("150.00"),
            pwyc_min=Decimal("100.00"),
            pwyc_max=Decimal("250.00"),
            currency="EUR",
            total_quantity=5,
            quantity_sold=3,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=34),
            description="Pay what you can - making wellness accessible to all. Shared rooms.",
        )

        # Past event tier
        events_models.TicketTier.objects.create(
            event=self.events["past_event"],
            name="Gala Ticket",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("250.00"),
            currency="USD",
            total_quantity=200,
            quantity_sold=200,
            sales_start_at=now - timedelta(days=120),
            sales_end_at=now - timedelta(days=91),
            description="Sold out - event has passed",
        )

        # ML Workshop - Sold out
        events_models.TicketTier.objects.create(
            event=self.events["sold_out_workshop"],
            name="Workshop Seat",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("299.00"),
            currency="EUR",
            total_quantity=20,
            quantity_sold=20,
            sales_start_at=now - timedelta(days=10),
            sales_end_at=now + timedelta(days=27),
            description="Intensive workshop - materials included",
        )

        # Seated Concert - Reserved seating with user seat selection
        # Get the sector for the concert hall
        concert_sector = events_models.VenueSector.objects.get(venue=self.venues["concert_hall"], name="Main Floor")
        events_models.TicketTier.objects.create(
            event=self.events["seated_concert"],
            name="Reserved Seat",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.ONLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("75.00"),
            currency="EUR",
            total_quantity=100,
            quantity_sold=0,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=49),
            description="Reserved seating - select your seat during checkout.",
            venue=self.venues["concert_hall"],
            sector=concert_sector,
            seat_assignment_mode=events_models.TicketTier.SeatAssignmentMode.USER_CHOICE,
        )

        # Seated Concert - Standing Room with offline payment (for payment tracking demo)
        events_models.TicketTier.objects.create(
            event=self.events["seated_concert"],
            name="Standing Room",
            visibility=events_models.TicketTier.Visibility.PUBLIC,
            payment_method=events_models.TicketTier.PaymentMethod.OFFLINE,
            purchasable_by=events_models.TicketTier.PurchasableBy.PUBLIC,
            price=Decimal("35.00"),
            currency="EUR",
            total_quantity=50,
            quantity_sold=8,
            sales_start_at=now,
            sales_end_at=now + timedelta(days=49),
            description="Standing room at the back of the venue. Pay via bank transfer.",
            manual_payment_instructions="""## Payment Instructions

Please transfer **€35.00** to the following account:

- **Bank**: Revel Events Bank
- **IBAN**: AT12 3456 7890 1234 5678
- **BIC**: REVELAT2X
- **Reference**: Your ticket confirmation number

Once your payment is received, your ticket will be activated within 24 hours.
You will receive an email confirmation when your ticket is ready.

**Questions?** Contact us at tickets@revelcollective.example.com
""",
        )

        logger.info("Created ticket tiers for events with tickets")

    def _create_potluck_items(self) -> None:
        """Create potluck items for potluck-enabled events."""
        logger.info("Creating potluck items...")

        # Spring Potluck items
        potluck_items = [
            # Host-suggested items (unassigned)
            {
                "name": "Main Course (pasta, casserole, etc)",
                "quantity": "Serves 8-10",
                "item_type": events_models.PotluckItem.ItemTypes.MAIN_COURSE,
                "is_suggested": True,
                "note": "We need 3-4 main dishes. Please label ingredients for allergies!",
            },
            {
                "name": "Fresh Garden Salad",
                "quantity": "Large bowl",
                "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
                "is_suggested": True,
                "note": "Fresh, seasonal veggies appreciated",
            },
            {
                "name": "Dessert",
                "quantity": "Serves 8-10",
                "item_type": events_models.PotluckItem.ItemTypes.DESSERT,
                "is_suggested": True,
                "note": "Sweet treats welcome! Cookies, cake, pie, etc.",
            },
            {
                "name": "Beverages (non-alcoholic)",
                "quantity": "2-3 liters",
                "item_type": events_models.PotluckItem.ItemTypes.NON_ALCOHOLIC,
                "is_suggested": True,
                "note": "Juice, lemonade, iced tea, etc.",
            },
            {
                "name": "Paper plates, cups, napkins",
                "quantity": "For 80 people",
                "item_type": events_models.PotluckItem.ItemTypes.SUPPLIES,
                "is_suggested": True,
                "note": "Compostable/recyclable preferred!",
            },
            {
                "name": "Setup Help",
                "quantity": "2-3 volunteers",
                "item_type": events_models.PotluckItem.ItemTypes.LABOR,
                "is_suggested": True,
                "note": "Arrive 30 min early to help set up tables",
            },
            # User-contributed items (assigned)
            {
                "name": "Homemade Lasagna",
                "quantity": "Serves 10",
                "item_type": events_models.PotluckItem.ItemTypes.MAIN_COURSE,
                "is_suggested": False,
                "assignee": self.users["attendee_1"],
                "created_by": self.users["attendee_1"],
                "note": "Vegetarian option with spinach and ricotta",
            },
            {
                "name": "Mediterranean Mezze Platter",
                "quantity": "Large platter",
                "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
                "is_suggested": False,
                "assignee": self.users["attendee_2"],
                "created_by": self.users["attendee_2"],
                "note": "Hummus, falafel, olives, pita bread - all vegan!",
            },
            {
                "name": "Fresh Fruit Salad",
                "quantity": "Serves 12",
                "item_type": events_models.PotluckItem.ItemTypes.SIDE_DISH,
                "is_suggested": False,
                "assignee": self.users["multi_org_user"],
                "created_by": self.users["multi_org_user"],
                "note": "Seasonal berries and melons",
            },
            {
                "name": "Chocolate Brownies",
                "quantity": "2 dozen",
                "item_type": events_models.PotluckItem.ItemTypes.DESSERT,
                "is_suggested": False,
                "assignee": self.users["attendee_3"],
                "created_by": self.users["attendee_3"],
                "note": "Homemade fudgy brownies!",
            },
            {
                "name": "Fresh Lemonade",
                "quantity": "3 liters",
                "item_type": events_models.PotluckItem.ItemTypes.NON_ALCOHOLIC,
                "is_suggested": False,
                "assignee": self.users["org_alpha_member"],
                "created_by": self.users["org_alpha_member"],
                "note": "Freshly squeezed with mint",
            },
            {
                "name": "Acoustic Guitar Performance",
                "quantity": "30 min set",
                "item_type": events_models.PotluckItem.ItemTypes.ENTERTAINMENT,
                "is_suggested": False,
                "assignee": self.users["attendee_4"],
                "created_by": self.users["attendee_4"],
                "note": "Folk and acoustic covers - let me know preferred time!",
            },
        ]

        for item_data in potluck_items:
            assignee = t.cast(RevelUser | None, item_data.pop("assignee", None))
            created_by = t.cast(RevelUser | None, item_data.pop("created_by", None))

            events_models.PotluckItem.objects.create(
                event=self.events["spring_potluck"],
                assignee=assignee,
                created_by=created_by,
                **item_data,
            )

        logger.info(f"Created {len(potluck_items)} potluck items")

    def _create_user_relationships(self) -> None:
        """Create comprehensive user relationships: invitations, tickets, RSVPs, waitlists."""
        logger.info("Creating user relationships...")

        now = timezone.now()

        # --- Invitations ---

        # Wine tasting - private event with specific invitations
        wine_tier = events_models.TicketTier.objects.get(event=self.events["wine_tasting"], name="Exclusive Seating")

        events_models.EventInvitation.objects.create(
            event=self.events["wine_tasting"],
            user=self.users["multi_org_user"],
            waives_questionnaire=True,
            waives_purchase=False,
            tier=wine_tier,
            custom_message="You're invited to our exclusive wine tasting dinner!",
        )

        events_models.EventInvitation.objects.create(
            event=self.events["wine_tasting"],
            user=self.users["attendee_1"],
            waives_questionnaire=True,
            waives_purchase=True,  # Complimentary
            tier=wine_tier,
            custom_message="As a valued member, please join us as our guest!",
        )

        # Pending invitation (email not yet registered)
        events_models.PendingEventInvitation.objects.create(
            event=self.events["wine_tasting"],
            email="vip.guest@example.com",
            waives_questionnaire=True,
            waives_purchase=False,
            tier=wine_tier,
            custom_message="We'd love for you to join our exclusive wine tasting event!",
        )

        # --- Tickets ---

        # Summer festival tickets
        festival_early_bird = events_models.TicketTier.objects.get(
            event=self.events["summer_festival"], name="Early Bird General Admission"
        )
        festival_general = events_models.TicketTier.objects.get(
            event=self.events["summer_festival"], name="General Admission"
        )
        festival_vip = events_models.TicketTier.objects.get(event=self.events["summer_festival"], name="VIP Experience")

        # Active tickets
        for user_key in ["attendee_1", "attendee_2", "attendee_3", "multi_org_user"]:
            user = self.users[user_key]
            events_models.Ticket.objects.create(
                guest_name=user.get_display_name(),
                event=self.events["summer_festival"],
                user=user,
                tier=festival_early_bird,
                status=events_models.Ticket.TicketStatus.ACTIVE,
            )

        # VIP tickets
        events_models.Ticket.objects.create(
            guest_name=self.users["org_alpha_owner"].get_display_name(),
            event=self.events["summer_festival"],
            user=self.users["org_alpha_owner"],
            tier=festival_vip,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

        # Pending ticket (payment not completed)
        pending_ticket = events_models.Ticket.objects.create(
            guest_name=self.users["attendee_4"].get_display_name(),
            event=self.events["summer_festival"],
            user=self.users["attendee_4"],
            tier=festival_general,
            status=events_models.Ticket.TicketStatus.PENDING,
        )

        # Payment for pending ticket
        events_models.Payment.objects.create(
            ticket=pending_ticket,
            user=self.users["attendee_4"],
            stripe_session_id=f"cs_test_{self.fake.uuid4()}",
            status=events_models.Payment.PaymentStatus.PENDING,
            amount=Decimal("65.00"),
            platform_fee=Decimal("6.50"),
            currency="USD",
            expires_at=now + timedelta(minutes=30),
        )

        # Past event - checked in tickets
        past_tier = events_models.TicketTier.objects.get(event=self.events["past_event"], name="Gala Ticket")

        checked_in_ticket = events_models.Ticket.objects.create(
            guest_name=self.users["attendee_1"].get_display_name(),
            event=self.events["past_event"],
            user=self.users["attendee_1"],
            tier=past_tier,
            status=events_models.Ticket.TicketStatus.CHECKED_IN,
            checked_in_at=now - timedelta(days=89, hours=2),
            checked_in_by=self.users["org_alpha_staff"],
        )

        # Payment for past event
        events_models.Payment.objects.create(
            ticket=checked_in_ticket,
            user=self.users["attendee_1"],
            stripe_session_id=f"cs_test_{self.fake.uuid4()}",
            status=events_models.Payment.PaymentStatus.SUCCEEDED,
            amount=Decimal("250.00"),
            platform_fee=Decimal("25.00"),
            currency="USD",
            expires_at=now - timedelta(days=120),
        )

        # Cancelled ticket
        cancelled_ticket = events_models.Ticket.objects.create(
            guest_name=self.users["pending_user"].get_display_name(),
            event=self.events["summer_festival"],
            user=self.users["pending_user"],
            tier=festival_early_bird,
            status=events_models.Ticket.TicketStatus.CANCELLED,
        )

        events_models.Payment.objects.create(
            ticket=cancelled_ticket,
            user=self.users["pending_user"],
            stripe_session_id=f"cs_test_{self.fake.uuid4()}",
            status=events_models.Payment.PaymentStatus.REFUNDED,
            amount=Decimal("45.00"),
            platform_fee=Decimal("4.50"),
            currency="USD",
            expires_at=now - timedelta(days=5),
        )

        # Wellness retreat tickets
        wellness_tier = events_models.TicketTier.objects.get(event=self.events["wellness_retreat"], name="Shared Room")

        events_models.Ticket.objects.create(
            guest_name=self.users["attendee_2"].get_display_name(),
            event=self.events["wellness_retreat"],
            user=self.users["attendee_2"],
            tier=wellness_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

        # Tech conference tickets
        conf_member_tier = events_models.TicketTier.objects.get(
            event=self.events["tech_conference"], name="Member Discount"
        )

        events_models.Ticket.objects.create(
            guest_name=self.users["org_beta_member"].get_display_name(),
            event=self.events["tech_conference"],
            user=self.users["org_beta_member"],
            tier=conf_member_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

        # --- Classical Music Evening (Offline payment demo) ---
        # This demonstrates payment tracking for offline payments
        standing_tier = events_models.TicketTier.objects.get(event=self.events["seated_concert"], name="Standing Room")

        # Tickets with confirmed payment (ACTIVE)
        events_models.Ticket.objects.create(
            guest_name=self.users["attendee_3"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["attendee_3"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["org_alpha_member"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["org_alpha_member"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["multi_org_user"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["multi_org_user"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.ACTIVE,
        )

        # Tickets awaiting payment confirmation (PENDING)
        events_models.Ticket.objects.create(
            guest_name=self.users["attendee_1"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["attendee_1"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["attendee_2"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["attendee_2"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["attendee_4"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["attendee_4"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["pending_user"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["pending_user"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )
        events_models.Ticket.objects.create(
            guest_name=self.users["invited_user"].get_display_name(),
            event=self.events["seated_concert"],
            user=self.users["invited_user"],
            tier=standing_tier,
            status=events_models.Ticket.TicketStatus.PENDING,
        )

        # --- RSVPs (for events without tickets) ---

        # Spring potluck RSVPs
        rsvp_users_yes = ["attendee_1", "attendee_2", "attendee_3", "attendee_4", "multi_org_user", "org_alpha_member"]
        for user_key in rsvp_users_yes:
            events_models.EventRSVP.objects.create(
                event=self.events["spring_potluck"],
                user=self.users[user_key],
                status=events_models.EventRSVP.RsvpStatus.YES,
            )

        # Maybe RSVPs
        events_models.EventRSVP.objects.create(
            event=self.events["spring_potluck"],
            user=self.users["org_alpha_staff"],
            status=events_models.EventRSVP.RsvpStatus.MAYBE,
        )

        # No RSVP
        events_models.EventRSVP.objects.create(
            event=self.events["spring_potluck"],
            user=self.users["pending_user"],
            status=events_models.EventRSVP.RsvpStatus.NO,
        )

        # Tech workshop RSVPs (members only)
        for user_key in ["org_beta_member", "org_beta_staff", "multi_org_user"]:
            events_models.EventRSVP.objects.create(
                event=self.events["tech_workshop"],
                user=self.users[user_key],
                status=events_models.EventRSVP.RsvpStatus.YES,
            )

        # Tech talk RSVPs
        events_models.EventRSVP.objects.create(
            event=self.events["tech_talk_may"],
            user=self.users["org_beta_member"],
            status=events_models.EventRSVP.RsvpStatus.YES,
        )

        # Networking event RSVPs
        for user_key in ["org_beta_member", "org_beta_staff", "multi_org_user", "attendee_1"]:
            events_models.EventRSVP.objects.create(
                event=self.events["networking_event"],
                user=self.users[user_key],
                status=events_models.EventRSVP.RsvpStatus.YES,
            )

        # Art opening RSVPs
        for user_key in ["attendee_2", "attendee_3", "org_alpha_member"]:
            events_models.EventRSVP.objects.create(
                event=self.events["art_opening"],
                user=self.users[user_key],
                status=events_models.EventRSVP.RsvpStatus.YES,
            )

        # --- Waitlists ---

        # ML Workshop waitlist (sold out)
        for user_key in ["attendee_3", "attendee_4", "invited_user"]:
            events_models.EventWaitList.objects.create(
                event=self.events["sold_out_workshop"],
                user=self.users[user_key],
            )

        # Summer festival waitlist (near capacity)
        events_models.EventWaitList.objects.create(
            event=self.events["summer_festival"],
            user=self.users["invited_user"],
        )

        logger.info("Created user relationships (invitations, tickets, RSVPs, waitlists)")

    def _create_dietary_data(self) -> None:
        """Create dietary preferences and restrictions for users."""
        logger.info("Creating dietary preferences and restrictions...")

        # Get dietary preferences (seeded in migration)
        vegan = DietaryPreference.objects.get(name="Vegan")
        vegetarian = DietaryPreference.objects.get(name="Vegetarian")
        gluten_free = DietaryPreference.objects.get(name="Gluten-Free")
        dairy_free = DietaryPreference.objects.get(name="Dairy-Free")
        pescatarian = DietaryPreference.objects.get(name="Pescatarian")
        halal = DietaryPreference.objects.get(name="Halal")
        kosher = DietaryPreference.objects.get(name="Kosher")

        # Get common food items (seeded in migration)
        peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
        shellfish, _ = FoodItem.objects.get_or_create(name="Shellfish")
        tree_nuts, _ = FoodItem.objects.get_or_create(name="Tree nuts")
        gluten, _ = FoodItem.objects.get_or_create(name="Gluten")
        milk, _ = FoodItem.objects.get_or_create(name="Milk")
        eggs, _ = FoodItem.objects.get_or_create(name="Eggs")
        soy, _ = FoodItem.objects.get_or_create(name="Soy")
        sesame, _ = FoodItem.objects.get_or_create(name="Sesame")
        fish, _ = FoodItem.objects.get_or_create(name="Fish")
        celery, _ = FoodItem.objects.get_or_create(name="Celery")

        # Alice (org_alpha_owner) - Vegetarian with mild lactose intolerance
        UserDietaryPreference.objects.create(
            user=self.users["org_alpha_owner"],
            preference=vegetarian,
            comment="Vegetarian for 5 years, prefer organic when possible",
            is_public=True,
        )
        DietaryRestriction.objects.create(
            user=self.users["org_alpha_owner"],
            food_item=milk,
            restriction_type=DietaryRestriction.RestrictionType.INTOLERANT,
            notes="Mild lactose intolerance, can handle small amounts in cooked food",
            is_public=True,
        )

        # Bob (org_alpha_staff) - Vegan
        UserDietaryPreference.objects.create(
            user=self.users["org_alpha_staff"],
            preference=vegan,
            comment="Strict vegan, no animal products including honey",
            is_public=True,
        )

        # Charlie (org_alpha_member) - Gluten-Free due to celiac
        UserDietaryPreference.objects.create(
            user=self.users["org_alpha_member"],
            preference=gluten_free,
            comment="Celiac disease, need strict gluten-free options",
            is_public=True,
        )
        DietaryRestriction.objects.create(
            user=self.users["org_alpha_member"],
            food_item=gluten,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Celiac disease - even cross-contamination is an issue",
            is_public=True,
        )

        # Diana (org_beta_owner) - Severe peanut allergy
        DietaryRestriction.objects.create(
            user=self.users["org_beta_owner"],
            food_item=peanuts,
            restriction_type=DietaryRestriction.RestrictionType.SEVERE_ALLERGY,
            notes="Anaphylaxis risk, carries EpiPen",
            is_public=True,
        )

        # Eve (org_beta_staff) - Pescatarian with shellfish allergy
        UserDietaryPreference.objects.create(
            user=self.users["org_beta_staff"],
            preference=pescatarian,
            comment="Eat fish but no other meat",
            is_public=True,
        )
        DietaryRestriction.objects.create(
            user=self.users["org_beta_staff"],
            food_item=shellfish,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Allergic to all crustaceans and mollusks",
            is_public=True,
        )

        # Frank (org_beta_member) - Halal
        UserDietaryPreference.objects.create(
            user=self.users["org_beta_member"],
            preference=halal,
            comment="Halal meat only, no pork or alcohol",
            is_public=True,
        )

        # George (attendee_1) - Tree nut allergy
        DietaryRestriction.objects.create(
            user=self.users["attendee_1"],
            food_item=tree_nuts,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Allergic to almonds, walnuts, cashews - all tree nuts",
            is_public=True,
        )

        # Hannah (attendee_2) - Dairy-Free and egg allergy
        UserDietaryPreference.objects.create(
            user=self.users["attendee_2"],
            preference=dairy_free,
            comment="Dairy-free diet",
            is_public=True,
        )
        DietaryRestriction.objects.create(
            user=self.users["attendee_2"],
            food_item=eggs,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Allergic reaction to eggs",
            is_public=True,
        )

        # Ivan (attendee_3) - Soy intolerance
        DietaryRestriction.objects.create(
            user=self.users["attendee_3"],
            food_item=soy,
            restriction_type=DietaryRestriction.RestrictionType.INTOLERANT,
            notes="Digestive issues with soy products",
            is_public=True,
        )

        # Julia (attendee_4) - Vegetarian and sesame allergy
        UserDietaryPreference.objects.create(
            user=self.users["attendee_4"],
            preference=vegetarian,
            comment="Vegetarian, okay with dairy and eggs",
            is_public=True,
        )
        DietaryRestriction.objects.create(
            user=self.users["attendee_4"],
            food_item=sesame,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Allergic to sesame seeds and tahini",
            is_public=True,
        )

        # Karen (multi_org_user) - Kosher
        UserDietaryPreference.objects.create(
            user=self.users["multi_org_user"],
            preference=kosher,
            comment="Keep kosher, need separate meat and dairy",
            is_public=True,
        )

        # Leo (pending_user) - Celery allergy
        DietaryRestriction.objects.create(
            user=self.users["pending_user"],
            food_item=celery,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="Allergic to celery and celeriac",
            is_public=True,
        )

        # Maria (invited_user) - Pescatarian and gluten-free
        UserDietaryPreference.objects.create(
            user=self.users["invited_user"],
            preference=pescatarian,
            comment="Pescatarian lifestyle",
            is_public=True,
        )
        UserDietaryPreference.objects.create(
            user=self.users["invited_user"],
            preference=gluten_free,
            comment="Gluten sensitivity, not celiac",
            is_public=True,
        )

        logger.info("Created dietary preferences and restrictions for users")

    def _create_questionnaires(self) -> None:
        """Create varied questionnaires with different evaluation modes."""
        logger.info("Creating questionnaires...")

        # Questionnaire 1: Simple Code of Conduct (for tech conference)
        coc_questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Code of Conduct Agreement",
            status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
            shuffle_questions=False,
            llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
            max_attempts=3,
            min_score=Decimal("100.00"),
        )

        coc_section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=coc_questionnaire,
            name="Community Guidelines",
            order=1,
        )

        coc_question = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=coc_questionnaire,
            section=coc_section,
            question=(
                "Do you agree to abide by our Code of Conduct, which includes treating all attendees "
                "with respect, refraining from harassment, and creating an inclusive environment?"
            ),
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=coc_question,
            option="Yes, I agree to the Code of Conduct",
            is_correct=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=coc_question,
            option="No, I do not agree",
            is_correct=False,
            order=2,
        )

        # Link to tech conference
        org_quest_coc = events_models.OrganizationQuestionnaire.objects.create(
            organization=self.orgs["beta"],
            questionnaire=coc_questionnaire,
        )
        org_quest_coc.events.add(self.events["tech_conference"])

        # Questionnaire 2: Wine Tasting Application (for private event)
        wine_questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Wine Tasting Dinner Application",
            status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            shuffle_questions=False,
            llm_guidelines="Evaluate applicants based on genuine interest in wine and culinary experiences.",
            llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
            max_attempts=1,
            min_score=Decimal("60.00"),
        )

        wine_section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=wine_questionnaire,
            name="About You",
            order=1,
        )

        # CoC for wine event
        wine_coc = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=wine_questionnaire,
            section=wine_section,
            question="Do you agree to our Code of Conduct?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=wine_coc,
            option="Yes",
            is_correct=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=wine_coc,
            option="No",
            is_correct=False,
            order=2,
        )

        # Interest question
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=wine_questionnaire,
            section=wine_section,
            question="What draws you to this wine tasting experience? Share your interest in wine or culinary arts.",
            llm_guidelines=(
                "Look for genuine enthusiasm and interest. Sophistication is not required - "
                "curiosity and appreciation matter most."
            ),
            positive_weight=3,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=2,
        )

        # Experience level
        experience_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=wine_questionnaire,
            section=wine_section,
            question="How would you describe your wine knowledge?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=3,
        )

        experience_options: dict[str, questionnaires_models.MultipleChoiceOption] = {}
        for idx, option in enumerate(
            [
                "Beginner - I'm curious to learn",
                "Intermediate - I enjoy wine regularly",
                "Advanced - I'm a serious enthusiast",
            ],
            1,
        ):
            opt = questionnaires_models.MultipleChoiceOption.objects.create(
                question=experience_q,
                option=option,
                is_correct=False,  # Doesnt matter
                order=idx,
            )
            experience_options[option] = opt

        # Conditional question: shown only if "Advanced" is selected
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=wine_questionnaire,
            section=wine_section,
            question="As an advanced wine enthusiast, which regions or varietals do you specialize in?",
            hint="Share your areas of expertise - this helps us tailor the experience for you.",
            llm_guidelines=(
                "Look for genuine expertise and passion. The answer should demonstrate "
                "real knowledge of wine regions, grape varieties, or winemaking techniques."
            ),
            positive_weight=2,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,  # Mandatory IF shown (condition met)
            order=4,
            depends_on_option=experience_options["Advanced - I'm a serious enthusiast"],
        )

        # Link to wine tasting
        org_quest_wine = events_models.OrganizationQuestionnaire.objects.create(
            organization=self.orgs["alpha"],
            questionnaire=wine_questionnaire,
        )
        org_quest_wine.events.add(self.events["wine_tasting"])

        # Questionnaire 3: Community Membership Application (org-level)
        membership_questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Tech Innovators Network Membership Application",
            status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.HYBRID,
            shuffle_questions=False,
            llm_guidelines=(
                "Evaluate based on genuine interest in technology, community contribution mindset, "
                "and professional background. We want diverse perspectives and skill levels."
            ),
            llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
            max_attempts=2,
            can_retake_after=timedelta(days=30),
            min_score=Decimal("70.00"),
        )

        member_section1 = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=membership_questionnaire,
            name="Professional Background",
            order=1,
        )

        member_section2 = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=membership_questionnaire,
            name="Community Fit",
            order=2,
        )

        # Section 1 questions
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=member_section1,
            question="Tell us about your professional background and current work in tech.",
            llm_guidelines=(
                "Look for clear communication and genuine tech involvement. "
                "All experience levels welcome - from students to seniors."
            ),
            positive_weight=2,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=1,
        )

        tech_areas = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=member_section1,
            question="Which tech areas are you most interested in? (Select all that apply)",
            allow_multiple_answers=True,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=2,
        )

        tech_area_options: dict[str, questionnaires_models.MultipleChoiceOption] = {}
        for idx, area in enumerate(
            [
                "AI/Machine Learning",
                "Web Development",
                "Mobile Development",
                "DevOps/Infrastructure",
                "Security",
                "Blockchain/Web3",
                "Data Science",
                "Other",
            ],
            1,
        ):
            opt = questionnaires_models.MultipleChoiceOption.objects.create(
                question=tech_areas,
                option=area,
                is_correct=True,
                order=idx,
            )
            tech_area_options[area] = opt

        # Conditional question: shown only if "AI/Machine Learning" is selected
        ai_followup_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=member_section1,
            question="Which AI/ML areas interest you most? (Select all that apply)",
            hint="This helps us connect you with relevant community members and events.",
            allow_multiple_answers=True,
            shuffle_options=True,
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,  # Mandatory IF shown (condition met)
            order=3,
            depends_on_option=tech_area_options["AI/Machine Learning"],
        )

        for idx, ai_area in enumerate(
            [
                "Large Language Models (LLMs)",
                "Computer Vision",
                "Reinforcement Learning",
                "MLOps & Model Deployment",
                "AI Ethics & Safety",
                "Generative AI (images, music, etc.)",
            ],
            1,
        ):
            questionnaires_models.MultipleChoiceOption.objects.create(
                question=ai_followup_q,
                option=ai_area,
                is_correct=True,
                order=idx,
            )

        # Conditional section: shown only if "Blockchain/Web3" is selected
        web3_section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=membership_questionnaire,
            name="Web3 Experience",
            description="Tell us more about your blockchain/Web3 background.",
            order=3,
            depends_on_option=tech_area_options["Blockchain/Web3"],
        )

        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=web3_section,
            question="Describe your experience with blockchain or Web3 technologies.",
            llm_guidelines="Look for genuine interest or experience. Beginners are welcome too.",
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=1,
        )

        web3_chains_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=web3_section,
            question="Which blockchains have you worked with or are interested in?",
            allow_multiple_answers=True,
            shuffle_options=True,
            positive_weight=1,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=False,
            order=2,
        )

        for idx, chain in enumerate(["Ethereum", "Solana", "Polygon", "Bitcoin", "Other L1/L2"], 1):
            questionnaires_models.MultipleChoiceOption.objects.create(
                question=web3_chains_q,
                option=chain,
                is_correct=True,
                order=idx,
            )

        # Section 2 questions
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=member_section2,
            question="What would you like to contribute to our community? (e.g., skills, knowledge, time, ideas)",
            llm_guidelines="Look for willingness to participate and contribute. Community is about give-and-take.",
            positive_weight=3,
            negative_weight=0,
            is_fatal=False,
            is_mandatory=True,
            order=1,
        )

        coc_member = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=membership_questionnaire,
            section=member_section2,
            question="Do you commit to fostering an inclusive, respectful community?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=2,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=coc_member,
            option="Yes, I commit to these values",
            is_correct=True,
            order=1,
        )

        questionnaires_models.MultipleChoiceOption.objects.create(
            question=coc_member,
            option="No",
            is_correct=False,
            order=2,
        )

        # Link to organization (not specific events)
        events_models.OrganizationQuestionnaire.objects.create(
            organization=self.orgs["beta"],
            questionnaire=membership_questionnaire,
        )

        logger.info("Created 3 questionnaires with different evaluation modes")
