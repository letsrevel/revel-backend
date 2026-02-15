# Demo & Seeding

Revel includes management commands to populate the database with realistic test data, useful for development, demos, and manual QA.

---

## Commands

### `make bootstrap`

Runs the `bootstrap` management command from `common/management/commands/bootstrap.py`, which orchestrates the full setup:

1. **`migrate`** -- applies all pending database migrations
2. **`bootstrap_events`** -- creates the main demo dataset (users, organizations, events, questionnaires, etc.)
3. **`bootstrap_test_events`** -- creates a third organization with events designed to test every eligibility gate
4. **`generate_test_jwts`** -- prints JWT access/refresh tokens for the superuser to stdout

```bash
make bootstrap
```

Safe to run multiple times -- it checks for existing data.

### `make seed`

Runs a separate, large-scale seeder (`seed` command) that generates a configurable amount of synthetic data for load testing and comprehensive QA. Defaults to `--seed 999`.

```bash
# Default: 1000 users, 20 organizations, 50 events per org
make seed

# Custom scale
uv run python src/manage.py seed --seed 42 --users 500 --organizations 10 --events 25

# Clear all existing data first (destructive!)
uv run python src/manage.py seed --seed 42 --clear
```

The seed command runs 12 phases: files, users, organizations, venues, events, questionnaires, tickets, interactions, notifications, social, preferences, and Telegram links. It also creates (or verifies) a superuser (`admin@revel.io` / `password` by default -- note this differs from `make bootstrap` which uses `admin@letsrevel.io`).

The seed command also supports a `--yes` flag to skip confirmation prompts, useful for automated scripts:

```bash
uv run python src/manage.py seed --seed 42 --yes
```

!!! info "Bootstrap vs Seed"
    `make bootstrap` creates a small, hand-crafted dataset with known users and scenarios that are easy to reason about during development. `make seed` creates a large, randomly generated dataset for stress testing and realistic-scale QA. They are independent -- you can run one or both.

### `bootstrap_perf_tests`

Creates test data specifically for Locust performance tests:

```bash
# Create performance test data
uv run python src/manage.py bootstrap_perf_tests

# Reset and recreate
uv run python src/manage.py bootstrap_perf_tests --reset
```

### `make reset-events`

Runs the `reset_events` command, which deletes all organizations and all non-`@letsrevel.io` users, then re-runs `bootstrap_events` to recreate fresh demo data.

```bash
make reset-events
```

!!! danger "Destructive Operation"
    `reset_events` **deletes all organizations, all questionnaires, and all users** whose email does not end with `@letsrevel.io`. It then re-runs `bootstrap_events` from scratch. This command is **only available when `DEMO_MODE=True`** in settings. It supports `--no-input` to skip the confirmation prompt (used by periodic tasks in demo environments).

!!! warning "Does not recreate test events"
    `reset_events` only re-runs `bootstrap_events`, **not** `bootstrap_test_events`. After a reset, the Eligibility Test Organization and its test events will be missing. Run `make bootstrap` if you need them recreated.

---

## What Gets Created

### Superuser

The `bootstrap` command creates a superuser if one does not already exist:

| Field | Value |
|---|---|
| **Username** | `admin@letsrevel.io` (or `DEFAULT_SUPERUSER_USERNAME` env var) |
| **Password** | `password` (or `DEFAULT_SUPERUSER_PASSWORD` env var) |
| **Email** | `admin@letsrevel.io` (or `DEFAULT_SUPERUSER_EMAIL` env var) |

### Tags (15)

A comprehensive tag taxonomy is created, split into two groups:

**Category tags (10):**

| Tag | Color | Description |
|---|---|---|
| `music` | `#FF6B6B` | Music-related events |
| `food` | `#4ECDC4` | Food and dining events |
| `workshop` | `#45B7D1` | Educational workshops |
| `conference` | `#96CEB4` | Professional conferences |
| `networking` | `#FFEAA7` | Networking events |
| `arts` | `#DDA15E` | Arts and culture |
| `tech` | `#6C5CE7` | Technology events |
| `sports` | `#00B894` | Sports and fitness |
| `wellness` | `#FDCB6E` | Health and wellness |
| `community` | `#E17055` | Community gatherings |

**Vibe tags (5):**

| Tag | Color | Description |
|---|---|---|
| `casual` | `#74B9FF` | Relaxed atmosphere |
| `formal` | `#2D3436` | Formal dress code |
| `educational` | `#00CEC9` | Learning focused |
| `social` | `#FD79A8` | Social interaction |
| `professional` | `#636E72` | Professional setting |

### Users (13 + 4 test users)

All users share the password: **`password123`**

**Main bootstrap users:**

| Email | Display Name | Key | Role |
|---|---|---|---|
| `alice.owner@example.com` | Alice Owner | `org_alpha_owner` | Owner of Revel Events Collective |
| `bob.staff@example.com` | Bob Staff | `org_alpha_staff` | Staff at Revel Events Collective |
| `charlie.member@example.com` | Charlie Member | `org_alpha_member` | Member of Revel Events Collective |
| `diana.owner@example.com` | Diana Owner | `org_beta_owner` | Owner of Tech Innovators Network |
| `eve.staff@example.com` | Eve Staff | `org_beta_staff` | Staff at Tech Innovators Network |
| `frank.member@example.com` | Frank Member | `org_beta_member` | Member of Tech Innovators Network |
| `george.attendee@example.com` | George Attendee | `attendee_1` | Regular attendee (also member of Beta) |
| `hannah.attendee@example.com` | Hannah Attendee | `attendee_2` | Regular attendee |
| `ivan.attendee@example.com` | Ivan Attendee | `attendee_3` | Regular attendee |
| `julia.attendee@example.com` | Julia Attendee | `attendee_4` | Regular attendee |
| `karen.multiorg@example.com` | Karen Multi | `multi_org_user` | Member of both organizations |
| `leo.pending@example.com` | Leo Pending | `pending_user` | Pending/unaffiliated user |
| `maria.invited@example.com` | Maria Invited | `invited_user` | Invited user |

Users are assigned random pronouns from a seeded list (`he/him`, `she/her`, `they/them`, `he/they`, `she/they`, `any pronouns`, or none).

**Additional test users (from `bootstrap_test_events`):**

| Email | Display Name | Role |
|---|---|---|
| `test.random@example.com` | Random Tester | No organization -- used for testing public access |
| `test.admin@example.com` | Test Admin | Owner of Eligibility Test Organization |
| `test.staff@example.com` | Test Staff | Staff at Eligibility Test Organization |
| `test.member@example.com` | Test Member | Member of Eligibility Test Organization |

10 dummy users (`dummy0@test.com` through `dummy9@test.com`) and 5 ticket holders (`ticketholder0@test.com` through `ticketholder4@test.com`) are also created for capacity testing.

### Organizations (3)

#### Revel Events Collective (Alpha)

| Setting | Value |
|---|---|
| **Slug** | `revel-events-collective` |
| **Visibility** | Public |
| **City** | Vienna |
| **Owner** | `alice.owner@example.com` |
| **Staff** | `bob.staff@example.com` |
| **Members** | `charlie.member@example.com`, `karen.multiorg@example.com` |
| **Tags** | `community`, `music`, `arts` |
| **Accept membership requests** | Yes |
| **Contact email** | `hello@revelcollective.example.com` |
| **Stripe Connect** | Enabled (uses `CONNECTED_TEST_STRIPE_ID` env var) |

#### Tech Innovators Network (Beta)

| Setting | Value |
|---|---|
| **Slug** | `tech-innovators-network` |
| **Visibility** | Public |
| **City** | Berlin |
| **Owner** | `diana.owner@example.com` |
| **Staff** | `eve.staff@example.com` |
| **Members** | `frank.member@example.com`, `karen.multiorg@example.com`, `george.attendee@example.com` |
| **Tags** | `tech`, `professional`, `networking` |
| **Accept membership requests** | Yes |
| **Contact email** | `info@techinnovators.example.com` |

#### Eligibility Test Organization

| Setting | Value |
|---|---|
| **Slug** | `eligibility-test-org` |
| **Visibility** | Public |
| **City** | Vienna |
| **Owner** | `test.admin@example.com` |
| **Staff** | `test.staff@example.com` |
| **Members** | `test.member@example.com` |
| **Tags** | `tech`, `test` |
| **Accept membership requests** | Yes |
| **Contact email** | `test@eligibility.example.com` |

### Venues (1)

#### Revel Concert Hall

| Setting | Value |
|---|---|
| **Organization** | Revel Events Collective |
| **City** | Vienna |
| **Address** | Musikvereinsplatz 1, 1010 Vienna, Austria |
| **Capacity** | 100 |
| **Sector** | Main Floor (code: `MF`) |
| **Seats** | 100 seats in a 10x10 grid (rows A-J, seats 1-10). Row A is accessible. |

### Event Series (2)

| Series | Organization | Tags |
|---|---|---|
| **Monthly Tech Talks** (`monthly-tech-talks`) | Tech Innovators Network | `tech`, `educational`, `networking` |
| **Seasonal Community Gatherings** (`seasonal-community-gatherings`) | Revel Events Collective | `food`, `community`, `casual` |

### Events (13 main + 11 test)

#### Main Bootstrap Events

| # | Event Name | Org | Type | Status | Key Features |
|---|---|---|---|---|---|
| 1 | **Summer Sunset Music Festival** | Alpha | Public | Open | Ticketed (3 tiers), 500 cap, waitlist, check-in window |
| 2 | **Exclusive Wine Tasting & Pairing Dinner** | Alpha | Public | Open | Ticketed (invite-only tier), 40 cap, questionnaire-gated, invitation requests |
| 3 | **Hands-on Workshop: Building with AI APIs** | Beta | Members-only | Open | Free RSVP, 30 cap, RSVP deadline |
| 4 | **Spring Community Potluck & Garden Party** | Alpha | Public | Open | Free RSVP, potluck enabled, 80 cap, part of Seasonal Gatherings series |
| 5 | **FutureStack 2025: AI & Web3 Conference** | Beta | Public | Open | Ticketed (4 tiers incl. member discount), 1000 cap, waitlist, check-in window |
| 6 | **Weekend Wellness Retreat** | Alpha | Public | Open | Ticketed (incl. PWYC tier), 25 cap |
| 7 | **Tech Founders Networking Happy Hour** | Beta | Members-only | Open | Free RSVP, 50 cap, RSVP deadline |
| 8 | **Contemporary Art Exhibition Opening** | Alpha | Public | Open | Free RSVP, London |
| 9 | **New Year's Eve Gala 2024** | Alpha | Public | Closed | Past event, ticketed, feedback questionnaire |
| 10 | **Future Tech Summit (Planning Phase)** | Beta | Private | Draft | Draft event, Tokyo |
| 11 | **Tech Talk May: Scaling Microservices** | Beta | Members-only | Open | Free RSVP, part of Monthly Tech Talks series |
| 12 | **Advanced Machine Learning Workshop** | Beta | Public | Open | Ticketed (sold out), 20 cap, waitlist |
| 13 | **Classical Music Evening** | Alpha | Public | Open | Ticketed (reserved seating + offline payment), venue: Revel Concert Hall, check-in window |

#### Eligibility Test Events

These events are all in the **Eligibility Test Organization** and are designed to test every possible eligibility gate:

| Event Name | Scenario Tested |
|---|---|
| Accessible Public Event | No restrictions -- any user can RSVP |
| Event Requires Questionnaire | QuestionnaireGate -- must complete questionnaire first |
| Members-Only Event | MembershipGate -- must be organization member |
| Private Event (Invitation Required) | InvitationGate -- must have valid invitation |
| Event at Full Capacity | AvailabilityGate -- event is full (10/10), waitlist available |
| RSVP Deadline Passed | RSVPDeadlineGate -- RSVP period ended |
| Tickets Not Yet On Sale | TicketSalesGate -- sales start in the future |
| Draft Event (Not Yet Open) | EventStatusGate -- event not yet open |
| Past Event (Finished) | EventStatusGate -- event already ended |
| Event Requires Ticket Purchase | Tickets on sale, must purchase |
| Sold Out Event | AvailabilityGate -- all tickets sold (5/5), waitlist available |

### Ticket Tiers

#### Summer Sunset Music Festival

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Early Bird General Admission | $45.00 | 200 (180 sold) | Online | Discounted, limited time |
| General Admission | $65.00 | 250 (45 sold) | Online | Standard pricing |
| VIP Experience | $150.00 | 50 (12 sold) | Online | Priority entry, lounge, meet & greet |

#### Exclusive Wine Tasting & Pairing Dinner

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Exclusive Seating | $200.00 | 40 (8 sold) | Online | Invitation-only, private visibility |

#### FutureStack 2025

| Tier | Price | Currency | Quantity | Payment | Notes |
|---|---|---|---|---|---|
| Early Bird - Full Access | 399.00 | EUR | 300 (295 sold) | Online | Almost sold out |
| Standard - Full Access | 599.00 | EUR | 500 (120 sold) | Online | 3-day access |
| Workshop Bundle | 899.00 | EUR | 100 (34 sold) | Online | Conference + 2 workshops |
| Member Discount | 299.00 | EUR | 100 (23 sold) | Online | Members-only, 50% off |

#### Weekend Wellness Retreat

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Shared Room | 250.00 EUR | 20 (14 sold) | Online | Standard |
| Community Support Rate | 150.00 EUR (PWYC: 100-250) | 5 (3 sold) | Online | Pay What You Can |

#### New Year's Eve Gala 2024

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Gala Ticket | $250.00 | 200 (200 sold) | Online | Sold out, past event |

#### Advanced Machine Learning Workshop

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Workshop Seat | 299.00 EUR | 20 (20 sold) | Online | Sold out |

#### Classical Music Evening

| Tier | Price | Quantity | Payment | Notes |
|---|---|---|---|---|
| Reserved Seat | 75.00 EUR | 100 (0 sold) | Online | Venue-linked, user chooses seat |
| Standing Room | 35.00 EUR | 50 (8 sold) | Offline | Bank transfer, manual payment instructions |

### Questionnaires (4 main + 1 test)

#### Main Bootstrap Questionnaires

| Questionnaire | Evaluation Mode | Linked To | Type |
|---|---|---|---|
| **Code of Conduct Agreement** | Automatic | FutureStack 2025 (event) | Admission |
| **Wine Tasting Dinner Application** | Manual | Wine Tasting (event) | Admission |
| **Tech Innovators Network Membership Application** | Hybrid (AI + human) | Tech Innovators Network (org-level) | Admission |
| **Event Feedback** | Manual | NYE Gala 2024 (event) | Feedback |

**Code of Conduct Agreement** -- Simple yes/no agreement. Fatal question (wrong answer = automatic fail). Max 3 attempts, requires 100% score.

**Wine Tasting Dinner Application** -- Multi-question application with a conditional question: if the user selects "Advanced" wine knowledge, they get an additional free-text question about their specialization. Max 1 attempt, requires 60% score.

**Tech Innovators Network Membership Application** -- Multi-section questionnaire with conditional questions and conditional sections. Selecting "AI/Machine Learning" reveals a follow-up multi-select about AI areas. Selecting "Blockchain/Web3" reveals an entire section about Web3 experience. Max 2 attempts (can retake after 30 days), requires 70% score.

**Event Feedback** -- Simple post-event feedback with a yes/no question and optional free-text comments. Linked as `FEEDBACK` type to the past event.

#### Eligibility Test Questionnaire

| Questionnaire | Evaluation Mode | Linked To | Type |
|---|---|---|---|
| **Eligibility Test Questionnaire** | Automatic | Eligibility Test Org (org) + questionnaire-gated event | Admission |

Simple automatic questionnaire with a fatal yes/no question and a free-text question.

### Potluck Items (12)

For the **Spring Community Potluck & Garden Party**:

**Host-suggested items (unassigned):**

| Item | Type | Quantity |
|---|---|---|
| Main Course (pasta, casserole, etc) | Main Course | Serves 8-10 |
| Fresh Garden Salad | Side Dish | Large bowl |
| Dessert | Dessert | Serves 8-10 |
| Beverages (non-alcoholic) | Non-Alcoholic | 2-3 liters |
| Paper plates, cups, napkins | Supplies | For 80 people |
| Setup Help | Labor | 2-3 volunteers |

**User-contributed items (assigned):**

| Item | Type | Brought By |
|---|---|---|
| Homemade Lasagna | Main Course | George Attendee |
| Mediterranean Mezze Platter | Side Dish | Hannah Attendee |
| Fresh Fruit Salad | Side Dish | Karen Multi |
| Chocolate Brownies | Dessert | Ivan Attendee |
| Fresh Lemonade | Non-Alcoholic | Charlie Member |
| Acoustic Guitar Performance | Entertainment | Julia Attendee |

### Invitations

| Event | User | Waives Purchase | Notes |
|---|---|---|---|
| Wine Tasting | `karen.multiorg@example.com` | No | Must still purchase ticket |
| Wine Tasting | `george.attendee@example.com` | Yes | Complimentary |
| Wine Tasting | `vip.guest@example.com` | No | Pending invitation (email not registered) |
| Private Event (test) | `test.member@example.com` | No | Test invitation gate |

### RSVPs

| Event | Users (YES) | MAYBE | NO |
|---|---|---|---|
| Spring Potluck | George, Hannah, Ivan, Julia, Karen, Charlie | Bob Staff | Leo Pending |
| AI APIs Workshop | Frank, Eve, Karen | -- | -- |
| Tech Talk May | Frank | -- | -- |
| Networking Happy Hour | Frank, Eve, Karen, George | -- | -- |
| Art Exhibition | Hannah, Ivan, Charlie | -- | -- |

### Tickets

- **Summer Festival**: 4 Early Bird (active), 1 VIP (active), 1 General (pending payment), 1 Early Bird (cancelled/refunded)
- **NYE Gala 2024**: 2 checked-in tickets (George, Charlie) with succeeded payments
- **Wellness Retreat**: 1 Shared Room (active, Hannah)
- **FutureStack 2025**: 1 Member Discount (active, Frank)
- **Classical Music Evening**: 3 Standing Room (active), 5 Standing Room (pending payment)

### Waitlist Entries

| Event | Users |
|---|---|
| Advanced ML Workshop | Ivan, Julia, Maria |
| Summer Festival | Maria |

### Follow Relationships

Organization members automatically follow their organization via a signal. Additional non-member follows:

| User | Follows | Notifications |
|---|---|---|
| Hannah Attendee | Revel Events Collective | New events + announcements (private) |
| Ivan Attendee | Tech Innovators Network | New events only (private) |
| Julia Attendee | Revel Events Collective | New events + announcements (public) |
| Julia Attendee | Tech Innovators Network | New events + announcements (private) |

### Dietary Data

Every bootstrap user has dietary preferences and/or food restrictions configured:

| User | Dietary Preference | Food Restriction |
|---|---|---|
| Alice Owner | Vegetarian | Milk (intolerant -- mild lactose) |
| Bob Staff | Vegan | -- |
| Charlie Member | Gluten-Free | Gluten (allergy -- celiac) |
| Diana Owner | -- | Peanuts (severe allergy -- anaphylaxis) |
| Eve Staff | Pescatarian | Shellfish (allergy) |
| Frank Member | Halal | -- |
| George Attendee | -- | Tree nuts (allergy) |
| Hannah Attendee | Dairy-Free | Eggs (allergy) |
| Ivan Attendee | -- | Soy (intolerant) |
| Julia Attendee | Vegetarian | Sesame (allergy) |
| Karen Multi | Kosher | -- |
| Leo Pending | -- | Celery (allergy) |
| Maria Invited | Pescatarian, Gluten-Free | -- |

---

## Testing Tips

!!! tip "Exploring Different Permission Levels"
    Log in as different users to experience the platform from various perspectives:

    - **Owner** (`alice.owner@example.com`): Full organizational control of Revel Events Collective
    - **Staff** (`bob.staff@example.com`): Event management without org admin
    - **Member** (`charlie.member@example.com`): Standard attendee experience
    - **Attendee** (`george.attendee@example.com`): Regular user, member of Beta only
    - **Multi-org** (`karen.multiorg@example.com`): Member of both organizations
    - **Non-member** (`hannah.attendee@example.com`): Not a member of any org
    - **Eligibility tester** (`test.random@example.com`): No org membership, test all gates

### Suggested Test Scenarios

| Scenario | How to Test |
|---|---|
| Ticket purchase flow | Log in as any user, buy a ticket for **FutureStack 2025** |
| PWYC ticket | Log in as any user, purchase the Community Support Rate tier for **Weekend Wellness Retreat** |
| RSVP with potluck | Log in as a member, RSVP to **Spring Community Potluck & Garden Party** |
| Waitlist behavior | Try to get a ticket for **Advanced Machine Learning Workshop** (sold out) |
| Past event data | View **New Year's Eve Gala 2024** for historical event information |
| Event feedback | Log in as `charlie.member@example.com` (checked-in at NYE Gala) and submit feedback |
| Questionnaire flow (simple) | Attend **FutureStack 2025** -- complete the Code of Conduct Agreement |
| Questionnaire flow (complex) | Apply to **Wine Tasting** -- conditional questions based on wine experience level |
| Membership application | Apply to join **Tech Innovators Network** via the membership questionnaire |
| Members-only access | Try accessing **Hands-on Workshop: Building with AI APIs** as a non-member |
| Invitation-only access | Try accessing **Exclusive Wine Tasting & Pairing Dinner** without an invitation |
| Offline payment | Purchase a Standing Room ticket for **Classical Music Evening** (bank transfer) |
| Reserved seating | Purchase a Reserved Seat ticket for **Classical Music Evening** (seat selection) |
| Eligibility gates | Log in as `test.random@example.com` and visit each event in the Eligibility Test Organization |
| Event series | Browse **Monthly Tech Talks** or **Seasonal Community Gatherings** series |
