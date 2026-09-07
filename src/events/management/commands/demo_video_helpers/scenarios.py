# src/events/management/commands/demo_video_helpers/scenarios.py
"""The five demo-video scenarios.

Each function seeds one self-contained organization — its own slug, its own
``@demovideo.example.com`` accounts — and returns the summary the command prints.
Nothing here touches the ``bootstrap_events`` / ``bootstrap_test_events`` fixtures
the E2E suite depends on.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import structlog

from events import models as events_models
from questionnaires import models as questionnaires_models

from .artwork import attach_scenario_artwork
from .base import (
    EvaluationStatus,
    ScenarioSummary,
    at_local_hour,
    default_membership_tier,
    drop_default_ticket_tier,
    upsert_choice_question,
    upsert_event,
    upsert_free_text_question,
    upsert_invitation,
    upsert_membership,
    upsert_organization,
    upsert_potluck_item,
    upsert_questionnaire,
    upsert_rsvp,
    upsert_section,
    upsert_submission,
    upsert_ticket_tier,
    upsert_user,
)

logger = structlog.get_logger(__name__)

ItemTypes = events_models.PotluckItem.ItemTypes
TierVisibility = events_models.TicketTier.Visibility
PaymentMethod = events_models.TicketTier.PaymentMethod
PurchasableBy = events_models.TicketTier.PurchasableBy


@dataclass(frozen=True)
class WalkOutcome:
    """How the organizer answered a photo-walk application, and how we describe it."""

    status: str
    label: str
    score: Decimal | None


APPROVED = WalkOutcome(EvaluationStatus.APPROVED, "approved", Decimal("85.00"))
REJECTED = WalkOutcome(EvaluationStatus.REJECTED, "rejected", Decimal("30.00"))
PENDING = WalkOutcome(EvaluationStatus.PENDING_REVIEW, "pending review", None)


@dataclass(frozen=True)
class WalkApplicant:
    """One seeded application to the photo walk."""

    first_name: str
    last_name: str
    pronouns: str
    gear: str
    referral: questionnaires_models.MultipleChoiceOption
    submitted_days_ago: int
    outcome: WalkOutcome
    comments: str = ""


def seed_shibari_circle() -> ScenarioSummary:
    """Scenario 1 — the flagship questionnaire gate: apply, get reviewed, get in."""
    logger.info("Seeding demo scenario: Shibari Circle Vienna")

    owner = upsert_user("Ren", "Okabe", "owner", pronouns="they/them")
    organization = upsert_organization(
        name="Shibari Circle Vienna",
        slug="shibari-circle-vienna",
        owner=owner,
        address="Brunnengasse 71, 1160 Vienna, Austria",
        contact_email="hello@shibaricircle.example.com",
        description="""# Shibari Circle Vienna

A small, consent-first rope community that has been meeting in Vienna since 2019. We run
beginner-friendly workshops, slow practice evenings, and the occasional rope jam — always with
more room for questions than anyone expects.

## What to expect

- A clear consent culture, and a code of conduct read out at the start of every session
- Experienced riggers and bottoms on hand all evening, never more than four people per teacher
- No pressure to tie, to be tied, or to stay past the point where you are enjoying yourself

## Joining us

Every workshop has a short application, so we can balance the room and make sure everyone
arrives with the same expectations. We read every single one, usually within a day or two.
""",
    )

    event = upsert_event(
        organization=organization,
        slug="intro-to-shibari-rope-and-trust",
        name="Intro to Shibari — Rope & Trust",
        address="Brunnengasse 71, 1160 Vienna, Austria",
        start=at_local_hour(7, hour=19),
        duration=dt.timedelta(hours=3),
        max_attendees=24,
        description="""# Intro to Shibari — Rope & Trust

An evening for absolute beginners. No experience, no rope and no partner required — we bring
the rope, and we will pair you up if you come on your own.

## The evening

- **19:00** — Welcome, tea, and the consent briefing
- **19:30** — Rope handling, tension, and the single-column tie
- **20:30** — Break, snacks, and questions
- **21:00** — Two-column tie and a first simple chest harness
- **21:45** — Cool-down, aftercare, and where to go next

## What to bring

Comfortable clothes you can move in, a water bottle, and a pair of safety shears if you own
them — we have spares. Trimmed nails, please.

## Access

Ground floor, step-free entrance, accessible bathroom. This is a fragrance-free space, so
please skip the perfume.
""",
    )
    drop_default_ticket_tier(event)
    upsert_ticket_tier(
        event,
        "Workshop Spot",
        visibility=TierVisibility.PUBLIC,
        payment_method=PaymentMethod.FREE,
        purchasable_by=PurchasableBy.PUBLIC,
        price=Decimal("0.00"),
        currency="EUR",
        total_quantity=24,
        description="One place at the workshop. Free — we would rather you spent the money on your own rope.",
    )

    questionnaire = upsert_questionnaire(
        organization=organization,
        name="Workshop Application",
        events=[event],
        description=(
            "We keep these evenings small and balanced. Tell us a little about yourself and we "
            "will come back to you within a couple of days."
        ),
    )
    section = upsert_section(questionnaire, "Your rope journey")
    experience_question = upsert_free_text_question(
        section,
        "Tell us about your experience with rope and what you hope to learn.",
        hint="There is no wrong answer — complete beginners are exactly who this evening is for.",
    )

    applicants = [
        (
            "Mira",
            "Lindqvist",
            "she/her",
            2,
            "I have been to two rope jams as a bottom but have never tied anyone myself. I would "
            "love to finally understand what my rigger is actually doing, and to be able to tie a "
            "safe single column on my own.",
        ),
        (
            "Tobias",
            "Ferrand",
            "he/him",
            3,
            "Complete beginner. A friend showed me a chest harness at a party last spring and I "
            "have not stopped thinking about it since. Mostly I want to learn how to do this safely.",
        ),
        (
            "Yuki",
            "Sorensen",
            "they/them",
            5,
            "About a year of self-tying at home from books and videos, which I suspect has taught "
            "me some bad habits. Hoping for eyes on my technique and some proper nerve-safety "
            "grounding.",
        ),
    ]

    attach_scenario_artwork(organization, event)

    summary = ScenarioSummary(
        title="Questionnaire gate — Shibari Circle Vienna",
        org_slug=organization.slug,
        event_paths=[f"/events/{organization.slug}/{event.slug}"],
    )
    summary.add(owner, "owner — reviews the applications on camera")

    for first_name, last_name, pronouns, days_ago, answer in applicants:
        applicant = upsert_user(first_name, last_name, "applicant", pronouns=pronouns)
        upsert_submission(
            user=applicant,
            event=event,
            questionnaire=questionnaire,
            free_text={experience_question: answer},
            submitted_days_ago=days_ago,
        )
        summary.add(applicant, "applied — waiting for review")

    newcomer = upsert_user("Noa", "Beckmann", "attendee", pronouns="she/her")
    summary.add(newcomer, "has NOT applied — use for the gate → apply → approve arc")
    summary.notes = [
        '"Workshop Application" is published, manually reviewed, with 3 submissions pending.',
        'One free tier, "Workshop Spot", 24 places — the gate is the questionnaire, not the price.',
    ]
    return summary


def seed_velvet_cellar() -> ScenarioSummary:
    """Scenario 2 — member-only ticket tier next to a public door price."""
    logger.info("Seeding demo scenario: The Velvet Cellar")

    owner = upsert_user("Dario", "Fenn", "owner", pronouns="he/him")
    organization = upsert_organization(
        name="The Velvet Cellar",
        slug="the-velvet-cellar",
        owner=owner,
        address="Lerchenfelder Gürtel 29, 1080 Vienna, Austria",
        contact_email="door@velvetcellar.example.com",
        description="""# The Velvet Cellar

A ninety-capacity basement club under the Gürtel, run as a members' club since 2016. Loud
guitars, cold soda, and a strict no-photos-on-the-floor policy.

## Membership

Membership is free and takes about a minute. Members get in free to most shows, hear about new
dates first, and can bring one guest on the door price.

## House rules

Look after each other. No photographs of anyone without asking them first. If anything feels
off, talk to whoever is wearing the red lanyard.
""",
    )

    event = upsert_event(
        organization=organization,
        slug="basement-sessions-live-and-loud",
        name="Basement Sessions: Live & Loud",
        address="Lerchenfelder Gürtel 29, 1080 Vienna, Austria",
        start=at_local_hour(10, hour=20),
        duration=dt.timedelta(hours=5),
        max_attendees=90,
        description="""# Basement Sessions: Live & Loud

Three bands, one basement, doors at 20:00. This month: a post-punk trio from Graz, a local
noise-pop four-piece, and a headliner we are not allowed to announce until the day.

## Running order

- **20:00** — Doors and opening DJ set
- **21:00** — Opener
- **21:45** — Second band
- **22:45** — Headliner
- **00:00** — Records until late

## Getting in

Members come in free — show your membership card at the door. Everyone else pays on the night,
cash or card. We do not sell tickets online.
""",
    )
    upsert_ticket_tier(
        event,
        events_models.DEFAULT_TICKET_TIER_NAME,
        visibility=TierVisibility.PUBLIC,
        payment_method=PaymentMethod.AT_THE_DOOR,
        purchasable_by=PurchasableBy.PUBLIC,
        price=Decimal("15.00"),
        currency="EUR",
        total_quantity=90,
        description="Pay at the door, cash or card. Reserve your place here so we know to expect you.",
    )
    upsert_ticket_tier(
        event,
        "Members — Free Entry",
        visibility=TierVisibility.MEMBERS_ONLY,
        payment_method=PaymentMethod.FREE,
        purchasable_by=PurchasableBy.MEMBERS,
        price=Decimal("0.00"),
        currency="EUR",
        total_quantity=60,
        description="Free entry for club members. Bring your membership card — we do check.",
    )

    member = upsert_user("Lena", "Krause", "member", pronouns="she/her")
    upsert_membership(organization, member)
    guest = upsert_user("Paul", "Vogt", "guest", pronouns="he/him")

    attach_scenario_artwork(organization, event)

    summary = ScenarioSummary(
        title="Member-only tier — The Velvet Cellar",
        org_slug=organization.slug,
        event_paths=[f"/events/{organization.slug}/{event.slug}"],
    )
    summary.add(owner, "owner")
    summary.add(member, "member — sees the free members' tier")
    summary.add(guest, "not a member — sees only the €15 door tier")
    summary.notes = [
        "Log in as each of the two accounts on the same event page for the A/B shot.",
        "General Admission is at-the-door €15; the members' tier is free and members-only.",
    ]
    return summary


def seed_picnic_club() -> ScenarioSummary:
    """Scenario 3 — an RSVP potluck with claimed and unclaimed items."""
    logger.info("Seeding demo scenario: Sunday Slow Picnic Club")

    owner = upsert_user("Ana", "Ferreira", "owner", pronouns="she/her")
    organization = upsert_organization(
        name="Sunday Slow Picnic Club",
        slug="sunday-slow-picnic-club",
        owner=owner,
        address="Obere Augartenstraße 1, 1020 Vienna, Austria",
        contact_email="hello@slowpicnic.example.com",
        description="""# Sunday Slow Picnic Club

We meet in a Viennese park roughly twice a month, spread out some blankets, and stay until the
light goes. No programme, no tickets, and no phones out the whole time. Bring something to
share if you can, and come anyway if you cannot.
""",
    )

    event = upsert_event(
        organization=organization,
        slug="picnic-in-the-park",
        name="Picnic in the Park",
        address="Augarten, Obere Augartenstraße 1, 1020 Vienna, Austria",
        start=at_local_hour(5, hour=12),
        duration=dt.timedelta(hours=5),
        requires_ticket=False,
        potluck_open=True,
        rsvp_before=at_local_hour(4, hour=20),
        description="""# Picnic in the Park

The Augarten, near the big chestnut trees past the second gate. Look for the yellow bunting.

## How it works

This is a potluck. Have a look at the list below and claim whatever you fancy bringing — or add
something of your own. Nobody keeps score, and there is always, always too much cake.

## Practical bits

- Step-free from the Obere Augartenstraße entrance
- Public toilets by the playground
- We take our rubbish home with us, so bring a bag if you have a spare
- Dogs welcome on a lead
""",
    )

    suggestions = [
        (
            "Big pot of something warm",
            ItemTypes.MAIN_COURSE,
            "Feeds 8–10",
            "Chilli, dal, a tray bake — anything that survives a bike ride.",
        ),
        (
            "Seasonal salad",
            ItemTypes.SIDE_DISH,
            "One large bowl",
            "Whatever looks good at the Karmelitermarkt on Saturday morning.",
        ),
        (
            "Bread and spreads",
            ItemTypes.SIDE_DISH,
            "For 12",
            "Please label anything containing nuts.",
        ),
        (
            "Picnic blankets",
            ItemTypes.SUPPLIES,
            "3–4 blankets",
            "The grass is usually still damp until noon.",
        ),
        (
            "Cups, plates and a bin bag",
            ItemTypes.SUPPLIES,
            "For 20",
            "Reusable if you have them, compostable if not.",
        ),
        (
            "Something to play",
            ItemTypes.ENTERTAINMENT,
            "One set of anything",
            "Boules, a pack of cards, a kite. Low stakes only.",
        ),
    ]
    for name, item_type, quantity, note in suggestions:
        upsert_potluck_item(event, name=name, item_type=item_type, quantity=quantity, note=note)

    claims = [
        (
            "Sofia",
            "Marchetti",
            "she/her",
            "Lemon and olive oil cake",
            ItemTypes.DESSERT,
            "Two tins",
            "Vegan, and honestly better on the second day.",
        ),
        (
            "Omar",
            "Haddad",
            "he/him",
            "Mujaddara with crispy onions",
            ItemTypes.MAIN_COURSE,
            "Feeds 10",
            "Vegan and mild — chilli oil on the side for anyone who wants it.",
        ),
        (
            "Greta",
            "Nowak",
            "she/they",
            "Two thermoses of iced hibiscus tea",
            ItemTypes.NON_ALCOHOLIC,
            "4 litres",
            "Unsweetened, with sugar in a jar alongside.",
        ),
        (
            "Felix",
            "Baumann",
            "he/him",
            "Guitar and a very small amp",
            ItemTypes.ENTERTAINMENT,
            "One 30-minute set",
            "Battery powered, park-legal volume, requests welcome.",
        ),
    ]

    attach_scenario_artwork(organization, event)

    summary = ScenarioSummary(
        title="Potluck — Sunday Slow Picnic Club",
        org_slug=organization.slug,
        event_paths=[f"/events/{organization.slug}/{event.slug}"],
    )
    summary.add(owner, "owner — added the six host suggestions")

    for first_name, last_name, pronouns, item_name, item_type, quantity, note in claims:
        picnicker = upsert_user(first_name, last_name, "picnicker", pronouns=pronouns)
        upsert_rsvp(event, picnicker)
        upsert_potluck_item(
            event, name=item_name, item_type=item_type, quantity=quantity, note=note, assignee=picnicker
        )
        summary.add(picnicker, f"attending — claimed “{item_name}”")

    empty_handed = upsert_user("Jonas", "Reiter", "guest", pronouns="he/him")
    upsert_rsvp(event, empty_handed)
    summary.add(empty_handed, "attending with NO claim — use for the claim-an-item shot")
    summary.notes = [
        "RSVP event (no tickets) with the potluck board open.",
        "Six unclaimed host suggestions and four claimed items are already on the board.",
    ]
    return summary


def seed_photo_walks() -> ScenarioSummary:
    """Scenario 4 — a questionnaire with enough submissions to show insights."""
    logger.info("Seeding demo scenario: Analog Photo Walks")

    owner = upsert_user("Kaia", "Lund", "owner", pronouns="she/her")
    organization = upsert_organization(
        name="Analog Photo Walks",
        slug="analog-photo-walks",
        owner=owner,
        address="Karlsplatz 1, 1040 Vienna, Austria",
        contact_email="walks@analogphoto.example.com",
        description="""# Analog Photo Walks

A film-only photo walk, once a month, around a different corner of Vienna. Any camera that takes
a roll counts — point-and-shoot, medium format, or the thing you found in your grandmother's
cupboard. We walk slowly, we stop a lot, and we get a drink at the end.

## Why we ask questions

The walks stay at twelve people, so that nobody spends the evening photographing the back of
someone's head. The short application below just helps us keep the group mixed and know who is
bringing what.
""",
    )

    event = upsert_event(
        organization=organization,
        slug="golden-hour-photo-walk",
        name="Golden Hour Photo Walk",
        address="Karlsplatz 1, 1040 Vienna, Austria",
        start=at_local_hour(12, hour=18, minute=30),
        duration=dt.timedelta(hours=3),
        requires_ticket=False,
        max_attendees=12,
        description="""# Golden Hour Photo Walk

We meet at the Otto Wagner pavilions on Karlsplatz an hour before sunset, walk through the
Naschmarkt and along the Wienzeile as the light turns orange, and finish at a bar in the 5th
about two hours later.

## Bring

- A loaded camera and a spare roll — 400 ISO is the safe bet for this one
- Shoes you can walk four kilometres in
- Enough cash for one drink at the end

## Level

All of them. Half the group has never developed a roll, and the other half will happily talk
your ear off about it.
""",
    )

    questionnaire = upsert_questionnaire(
        organization=organization,
        name="Walk Application",
        events=[event],
        description="Two quick questions so we can keep the group to twelve and nicely mixed.",
    )
    section = upsert_section(questionnaire, "About your camera")
    gear_question = upsert_free_text_question(
        section,
        "What do you shoot with?",
        order=1,
        hint="Camera, film stock, or “no idea yet, I have just inherited this thing” — all fine.",
    )
    referral_question, referral_options = upsert_choice_question(
        section,
        "How did you hear about us?",
        ["A friend brought me along", "Instagram", "I found the group on Revel"],
        order=2,
    )
    friend, instagram, on_revel = referral_options

    applicants = [
        WalkApplicant(
            first_name="Hanna",
            last_name="Persson",
            pronouns="she/her",
            gear="A Pentax K1000 my dad taught me on, usually loaded with HP5. I develop black and "
            "white at home in the kitchen sink.",
            referral=friend,
            submitted_days_ago=2,
            outcome=APPROVED,
        ),
        WalkApplicant(
            first_name="Milan",
            last_name="Kovac",
            pronouns="he/him",
            gear="An Olympus mju-II in my coat pocket at all times. Portra 400 when I can afford "
            "it, Gold 200 when I cannot.",
            referral=instagram,
            submitted_days_ago=3,
            outcome=APPROVED,
        ),
        WalkApplicant(
            first_name="Aisha",
            last_name="Rahman",
            pronouns="she/her",
            gear="A Mamiya RB67. It is absurdly heavy and I love it. Mostly portraits, so a slow "
            "walk suits me perfectly.",
            referral=on_revel,
            submitted_days_ago=4,
            outcome=APPROVED,
        ),
        WalkApplicant(
            first_name="Lukas",
            last_name="Berger",
            pronouns="he/him",
            gear="Nothing yet — I have just been given my grandmother's Werra and I would like to "
            "shoot my first roll with people who know what they are doing.",
            referral=friend,
            submitted_days_ago=5,
            outcome=APPROVED,
            comments="Total beginner, borrowed a light meter from Hanna. Lovely.",
        ),
        WalkApplicant(
            first_name="Zoe",
            last_name="Martins",
            pronouns="they/them",
            gear="A Canon AE-1 and a 50mm, and that is the whole kit. Cinestill 800T for anything after dark.",
            referral=instagram,
            submitted_days_ago=6,
            outcome=APPROVED,
        ),
        WalkApplicant(
            first_name="Bruno",
            last_name="Salgado",
            pronouns="he/him",
            gear="I shoot digital, is that a problem? I have a very nice Sony.",
            referral=instagram,
            submitted_days_ago=7,
            outcome=REJECTED,
            comments="Film-only walk. Sent a friendly note pointing at the city photo club instead.",
        ),
        WalkApplicant(
            first_name="Timo",
            last_name="Rask",
            pronouns="he/him",
            gear="not sure yet, mostly just want to know if there will be models",
            referral=instagram,
            submitted_days_ago=8,
            outcome=REJECTED,
            comments="Asked about models rather than photography. Not a fit for this group.",
        ),
        WalkApplicant(
            first_name="Elif",
            last_name="Demir",
            pronouns="she/her",
            gear="A Yashica Mat 124G, twin lens. I have been shooting expired Ektar and getting beautiful mistakes.",
            referral=on_revel,
            submitted_days_ago=1,
            outcome=PENDING,
        ),
        WalkApplicant(
            first_name="Pawel",
            last_name="Zielinski",
            pronouns="he/him",
            gear="A Nikon FM2 with a 28mm. Coming back to film after ten years away, and quite rusty.",
            referral=friend,
            submitted_days_ago=1,
            outcome=PENDING,
        ),
        WalkApplicant(
            first_name="Nora",
            last_name="Lindgren",
            pronouns="she/her",
            gear="A Lomo LC-A and a lot of enthusiasm. I would like to learn to actually meter instead of guessing.",
            referral=on_revel,
            submitted_days_ago=2,
            outcome=PENDING,
        ),
    ]

    attach_scenario_artwork(organization, event)

    summary = ScenarioSummary(
        title="Questionnaire insights — Analog Photo Walks",
        org_slug=organization.slug,
        event_paths=[f"/events/{organization.slug}/{event.slug}"],
    )
    summary.add(owner, "owner — open the questionnaire insights here")

    for applicant in applicants:
        walker = upsert_user(applicant.first_name, applicant.last_name, "walker", pronouns=applicant.pronouns)
        upsert_submission(
            user=walker,
            event=event,
            questionnaire=questionnaire,
            free_text={gear_question: applicant.gear},
            choices={referral_question: applicant.referral},
            submitted_days_ago=applicant.submitted_days_ago,
            status=applicant.outcome.status,
            score=applicant.outcome.score,
            comments=applicant.comments,
        )
        summary.add(walker, f"applied — {applicant.outcome.label}")

    summary.notes = [
        "10 submissions: 5 approved (85), 2 rejected (30), 3 still pending review.",
        'The multiple-choice "How did you hear about us?" answers drive the insights charts.',
    ]
    return summary


def seed_book_club() -> ScenarioSummary:
    """Scenario 5 — three accounts hitting three different eligibility outcomes."""
    logger.info("Seeding demo scenario: Paper Hearts Book Club")

    owner = upsert_user("Iris", "Halvorsen", "owner", pronouns="she/her")
    organization = upsert_organization(
        name="Paper Hearts Book Club",
        slug="paper-hearts-book-club",
        owner=owner,
        address="Gumpendorfer Straße 11, 1060 Vienna, Austria",
        contact_email="hello@paperhearts.example.com",
        description="""# Paper Hearts Book Club

Twelve people, one book a month, and a rule that you are welcome whether or not you finished it.
We read fiction in translation, mostly, and we have been meeting above the same café on
Gumpendorfer Straße since 2021.

## Membership

The reading circle is for members. Membership is free — send us a short note about what you are
reading at the moment and we will get you on the list before the next meeting.
""",
    )

    event = upsert_event(
        organization=organization,
        slug="monthly-reading-circle",
        name="Monthly Reading Circle",
        address="Gumpendorfer Straße 11, 1060 Vienna, Austria",
        start=at_local_hour(9, hour=19, minute=30),
        duration=dt.timedelta(hours=2, minutes=30),
        event_type=events_models.Event.EventType.MEMBERS_ONLY,
        requires_ticket=False,
        max_attendees=12,
        description="""# Monthly Reading Circle

**This month:** *The Employees* by Olga Ravn.

Upstairs at the café from 19:30, until they throw us out at around 22:00. Coffee and wine at the
bar; the first round is on the club.

## How the evening runs

- A quick round of first impressions — one sentence each, no summarising
- Two passages read aloud, chosen by whoever volunteered last month
- Open discussion, which is where the actual evening happens
- Choosing next month's book by a show of hands

## If you have not finished it

Come anyway. Seriously. Half the room never does.
""",
    )

    questionnaire = upsert_questionnaire(
        organization=organization,
        name="Reading Circle Intro",
        events=[event],
        description="A quick hello, so we know who is joining us upstairs.",
    )
    section = upsert_section(questionnaire, "Say hello")
    upsert_free_text_question(
        section,
        "What are you reading at the moment, and what made you pick it up?",
        hint="A sentence is plenty. We are nosy, not strict.",
    )

    outsider = upsert_user("Bea", "Nilsson", "outsider", pronouns="she/her")
    member = upsert_user("Hugo", "Almeida", "member", pronouns="he/him")
    upsert_membership(organization, member, default_membership_tier(organization))
    invited = upsert_user("Clara", "Mendel", "invited", pronouns="she/her")
    upsert_invitation(
        event,
        invited,
        waives_questionnaire=True,
        waives_membership_required=True,
        custom_message=(
            "Clara — come straight up on Thursday. No application and no membership needed; "
            "just ask at the bar for Iris."
        ),
    )

    attach_scenario_artwork(organization, event)

    summary = ScenarioSummary(
        title="Eligibility gates — Paper Hearts Book Club",
        org_slug=organization.slug,
        event_paths=[f"/events/{organization.slug}/{event.slug}"],
    )
    summary.add(owner, "owner")
    summary.add(outsider, "no membership, no submission — blocked by both gates")
    summary.add(member, "member, no submission — blocked by the questionnaire only")
    summary.add(invited, "invited — invitation waives the questionnaire AND membership")
    summary.notes = [
        "Members-only event type, public visibility, RSVP (no tickets).",
        "Open the same event page as each of the three accounts to show all three outcomes.",
    ]
    return summary


SCENARIOS = [
    seed_shibari_circle,
    seed_velvet_cellar,
    seed_picnic_club,
    seed_photo_walks,
    seed_book_club,
]
