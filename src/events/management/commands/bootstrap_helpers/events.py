# src/events/management/commands/bootstrap_helpers/events.py
"""Event and event series creation for bootstrap process."""

import datetime
from datetime import timedelta

import structlog
from django.utils import timezone

from events import models as events_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_event_series(state: BootstrapState) -> None:
    """Create event series for recurring events."""
    logger.info("Creating event series...")

    # Monthly Tech Meetup Series
    tech_series = events_models.EventSeries.objects.create(
        organization=state.orgs["beta"],
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
        organization=state.orgs["alpha"],
        name="Seasonal Community Gatherings",
        slug="seasonal-community-gatherings",
        description="""# Seasonal Community Gatherings

Celebrating the seasons together with potluck dinners, music, and community bonding.
Bring a dish to share and join us for an evening of connection and celebration!
""",
    )
    potluck_series.add_tags("food", "community", "casual")

    state.event_series = {
        "tech_talks": tech_series,
        "potlucks": potluck_series,
    }

    logger.info(f"Created {len(state.event_series)} event series")


def create_events(state: BootstrapState) -> None:
    """Create diverse, realistic events."""
    logger.info("Creating events...")

    now = timezone.now()

    _create_summer_festival(state, now)
    _create_wine_tasting(state, now)
    _create_tech_workshop(state, now)
    _create_spring_potluck(state, now)
    _create_tech_conference(state, now)
    _create_wellness_retreat(state, now)
    _create_networking_event(state, now)
    _create_art_opening(state, now)
    _create_past_event(state, now)
    _create_draft_event(state, now)
    _create_tech_talk_may(state, now)
    _create_sold_out_workshop(state, now)
    _create_seated_concert(state, now)

    logger.info(f"Created {len(state.events)} events")


def _create_summer_festival(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 1: Summer Music Festival (Public, Ticketed, Open, Future)."""
    summer_festival = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Summer Sunset Music Festival",
        slug="summer-sunset-music-festival",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["vienna"],
        requires_ticket=True,
        start=now + timedelta(days=45),
        end=now + timedelta(days=45, hours=8),
        max_attendees=500,
        waitlist_open=True,
        description="""# Summer Sunset Music Festival

Join us for an unforgettable evening of music under the stars! Experience the magic of live
performances from local and touring artists as the sun sets over the city.

## Featured Artists
- **The Midnight Riders** - Indie Rock
- **Sarah Chen & The Harmonics** - Jazz Fusion
- **DJ Nova** - Electronic/Dance
- **Acoustic Soul Collective** - Soul/R&B

## Event Highlights
- Multiple stages with diverse music genres
- Gourmet food trucks and local vendors
- Craft beer and cocktail garden
- Live art installations and performances
- Instagram-worthy sunset views

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
    state.events["summer_festival"] = summer_festival


def _create_wine_tasting(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 2: Exclusive Wine Tasting (Private, Ticketed, Open, Future)."""
    wine_tasting = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Exclusive Wine Tasting & Pairing Dinner",
        slug="exclusive-wine-tasting-dinner",
        event_type=events_models.Event.EventType.PRIVATE,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["vienna"],
        requires_ticket=True,
        start=now + timedelta(days=30),
        end=now + timedelta(days=30, hours=4),
        max_attendees=40,
        accept_invitation_requests=True,
        apply_before=now + timedelta(days=27),
        description="""# Exclusive Wine Tasting & Pairing Dinner

An intimate evening curated for wine enthusiasts. Join acclaimed sommelier Marcus Rodriguez
for a journey through rare vintages paired with a five-course tasting menu by Chef Elena Martinez.

## Wine Selection
**Featured Regions:** Bordeaux, Tuscany, Wachau Valley, Burgenland

Each course features carefully selected wines paired with seasonal ingredients:

1. **Amuse-Bouche** - Austrian Sekt Brut Nature
2. **First Course** - Gruner Veltliner with Seared Scallops
3. **Second Course** - Blaufrankisch with Duck Confit
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
    state.events["wine_tasting"] = wine_tasting


def _create_tech_workshop(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 3: Tech Workshop (Members-only, Free, Open, Future)."""
    tech_workshop = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="Hands-on Workshop: Building with AI APIs",
        slug="ai-apis-workshop",
        event_type=events_models.Event.EventType.MEMBERS_ONLY,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["berlin"],
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
    state.events["tech_workshop"] = tech_workshop


def _create_spring_potluck(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 4: Community Potluck (Public, No ticket, RSVP, Open, Future)."""
    spring_potluck = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Spring Community Potluck & Garden Party",
        slug="spring-community-potluck",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        event_series=state.event_series["potlucks"],
        city=state.cities["vienna"],
        requires_ticket=False,
        potluck_open=True,
        start=now + timedelta(days=15),
        end=now + timedelta(days=15, hours=5),
        rsvp_before=now + timedelta(days=13),
        max_attendees=80,
        description="""# Spring Community Potluck & Garden Party

Celebrate the arrival of spring with neighbors, friends, and community members! Bring a dish
to share and enjoy an afternoon of food, games, and connection in the garden.

## Event Activities
- **Potluck Feast** - International dishes from our diverse community
- **Live Music** - Acoustic performances throughout the afternoon
- **Lawn Games** - Kubb, frisbee, badminton
- **Kids Corner** - Face painting, crafts, and activities
- **Plant Swap** - Bring cuttings to share!

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
        address="Augarten, Obere Augartenstrasse, 1020 Vienna, Austria",
        location_maps_url="https://maps.app.goo.gl/HLZE9e3mzrxZBoyR8",
        location_maps_embed=(
            "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d9387.125669096846"
            "!2d16.374930297504203!3d48.22007689713033!2m3!1f0!2f0!3f0!3m2!1i1024!2i768"
            "!4f13.1!3m3!1m2!1s0x476d07a8bcc2f5cf%3A0x1cf8f8c0a86e2304!2sAugartenspitz"
            "!5e1!3m2!1sen!2sat!4v1768233815811!5m2!1sen!2sat"
        ),
    )
    spring_potluck.add_tags("food", "community", "casual")
    state.events["spring_potluck"] = spring_potluck


def _create_tech_conference(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 5: Tech Conference (Public, Ticketed, Open, Future)."""
    tech_conference = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="FutureStack 2025: AI & Web3 Conference",
        slug="futurestack-2025",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["berlin"],
        requires_ticket=True,
        start=now + timedelta(days=60),
        end=now + timedelta(days=62),
        max_attendees=1000,
        waitlist_open=True,
        apply_before=now + timedelta(days=57),
        description="""# FutureStack 2025: AI & Web3 Conference

Three days of cutting-edge tech insights, hands-on workshops, and networking with 1000+
developers, founders, and tech leaders from around the world.

## Conference Themes
- **Artificial Intelligence** - LLMs, ML Ops, AI Safety
- **Web3 & Blockchain** - DeFi, NFTs, DAOs
- **Cloud Native** - Kubernetes, Serverless, Edge Computing
- **Security** - Zero Trust, Supply Chain Security

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
- All meals and refreshments
- Conference swag bag
- Access to recordings (60 days)
- Workshop materials
- Networking app access

**Early Bird pricing ends in 2 weeks!**
""",
        address="Berlin Congress Center, Alexanderplatz 5, 10178 Berlin, Germany",
        check_in_starts_at=now + timedelta(days=60, hours=-2),
        check_in_ends_at=now + timedelta(days=62, hours=10),
    )
    tech_conference.add_tags("tech", "conference", "professional", "networking")
    state.events["tech_conference"] = tech_conference


def _create_wellness_retreat(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 6: Yoga & Wellness (Public, Ticketed, Open, Future)."""
    wellness_retreat = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Weekend Wellness Retreat",
        slug="weekend-wellness-retreat",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["vienna"],
        requires_ticket=True,
        start=now + timedelta(days=35),
        end=now + timedelta(days=37),
        max_attendees=25,
        description="""# Weekend Wellness Retreat

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
- 2 nights accommodation (shared rooms)
- All meals (vegetarian/vegan)
- Yoga & meditation sessions
- Workshops & activities
- Welcome gift bag

## What to Bring
- Yoga mat (or rent on-site for 5 EUR)
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
        address="Wachau Valley Retreat Center, Durnstein, 3601 Lower Austria",
    )
    wellness_retreat.add_tags("wellness", "casual", "social")
    state.events["wellness_retreat"] = wellness_retreat


def _create_networking_event(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 7: Networking Happy Hour (Members-only, Free, Open, Future)."""
    networking_event = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="Tech Founders Networking Happy Hour",
        slug="tech-founders-happy-hour",
        event_type=events_models.Event.EventType.MEMBERS_ONLY,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["berlin"],
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
    state.events["networking_event"] = networking_event


def _create_art_opening(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 8: Art Gallery Opening (Public, Free, Open, Future)."""
    art_opening = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Contemporary Art Exhibition Opening",
        slug="contemporary-art-exhibition",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["london"],
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
Wine, beer, and canapes served throughout the evening.

## Accessibility
The gallery is fully wheelchair accessible. ASL interpreter available upon request
(please email 48hrs in advance).

**Free admission - RSVP appreciated for catering purposes**
""",
        address="Shoreditch Gallery Space, 88 Brick Lane, London E1 6RL, UK",
    )
    art_opening.add_tags("arts", "social", "casual")
    state.events["art_opening"] = art_opening


def _create_past_event(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 9: Past event (for testing)."""
    past_event = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="New Year's Eve Gala 2024",
        slug="nye-gala-2024",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.CLOSED,
        city=state.cities["vienna"],
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
    state.events["past_event"] = past_event


def _create_draft_event(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 10: Draft event (for testing staff/owner views)."""
    draft_event = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="Future Tech Summit (Planning Phase)",
        slug="future-tech-summit-draft",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.DRAFT,
        city=state.cities["tokyo"],
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
    state.events["draft_event"] = draft_event


def _create_tech_talk_may(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 11: Monthly tech talk (part of series)."""
    tech_talk_may = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="Tech Talk May: Scaling Microservices",
        slug="tech-talk-may-2025",
        event_type=events_models.Event.EventType.MEMBERS_ONLY,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        event_series=state.event_series["tech_talks"],
        city=state.cities["berlin"],
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
    state.events["tech_talk_may"] = tech_talk_may


def _create_sold_out_workshop(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 12: Sold out event with waitlist."""
    sold_out_workshop = events_models.Event.objects.create(
        organization=state.orgs["beta"],
        name="Advanced Machine Learning Workshop",
        slug="advanced-ml-workshop",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["berlin"],
        requires_ticket=True,
        start=now + timedelta(days=28),
        end=now + timedelta(days=28, hours=6),
        max_attendees=20,
        waitlist_open=True,
        description="""# Advanced Machine Learning Workshop

**SOLD OUT - Join Waitlist**

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
    state.events["sold_out_workshop"] = sold_out_workshop


def _create_seated_concert(state: BootstrapState, now: "datetime.datetime") -> None:
    """Event 13: Seated concert event (with venue and reserved seating)."""
    seated_concert = events_models.Event.objects.create(
        organization=state.orgs["alpha"],
        name="Classical Music Evening",
        slug="classical-music-evening",
        event_type=events_models.Event.EventType.PUBLIC,
        visibility=events_models.Event.Visibility.PUBLIC,
        status=events_models.Event.EventStatus.OPEN,
        city=state.cities["vienna"],
        venue=state.venues["concert_hall"],
        requires_ticket=True,
        start=now + timedelta(days=50),
        end=now + timedelta(days=50, hours=3),
        max_attendees=100,
        description="""# Classical Music Evening

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
    state.events["seated_concert"] = seated_concert
