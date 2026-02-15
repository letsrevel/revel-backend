# Demo & Seeding

Revel includes management commands to populate the database with realistic test data, useful for development, demos, and manual QA.

---

## Commands

### Bootstrap

```bash
python manage.py bootstrap_events
```

Creates comprehensive test data from scratch. Safe to run multiple times -- it checks for existing data.

### Reset

```bash
python manage.py reset_events
```

!!! danger "Destructive Operation"
    `reset_events` deletes **all organizations and example users**, then re-runs the bootstrap. This command is only available when `DEMO_MODE` is enabled in settings. Do not use in production.

### Via Makefile

```bash
# Bootstrap test data
make bootstrap

# Seed additional data
make seed
```

---

## What Gets Created

The bootstrap command creates a complete test environment:

### Organizations (2)

| Organization | Visibility | Description |
|---|---|---|
| **Revel Events Collective** | Public | Open community events |
| **Tech Innovators Network** | Members-only | Exclusive tech community |

### Users (13)

All users share the password: **`password123`**

| Email | Role | Organization |
|---|---|---|
| `alice.owner@example.com` | Owner | Revel Events Collective |
| `diana.owner@example.com` | Owner | Tech Innovators Network |
| `bob.staff@example.com` | Staff | Revel Events Collective |
| `charlie.member@example.com` | Member | Revel Events Collective |
| `eve.pending@example.com` | Pending member | Revel Events Collective |
| `frank.nonmember@example.com` | Non-member | -- |
| `grace.blacklisted@example.com` | Blacklisted | Revel Events Collective |
| `heidi.inactive@example.com` | Inactive account | -- |
| `ivan.newuser@example.com` | New user (no org) | -- |
| `judy.multilang@example.com` | Member (German) | Revel Events Collective |
| `karen.multiorg@example.com` | Member | Both organizations |
| `leo.techstaff@example.com` | Staff | Tech Innovators Network |
| `mallory.techpending@example.com` | Pending member | Tech Innovators Network |

### Events (12)

Covers a wide range of scenarios:

| Event | Type | Key Features |
|---|---|---|
| Spring Garden Party | Public RSVP | Open to all, RSVP required |
| Members-Only Wine Tasting | Members-only | Membership gate |
| Summer Music Festival | Ticketed (paid) | Multiple tiers, Stripe checkout |
| FutureStack 2025 | Ticketed (paid) | Early bird + VIP + PWYC tiers |
| Spring Potluck in the Park | RSVP + Potluck | Potluck item management |
| Annual Charity Gala | Invite-only | Invitation gate |
| ML Workshop | Ticketed | Sold out (waitlist testing) |
| Community Hackathon | Questionnaire-gated | Automatic evaluation |
| Art Exhibition | Public | Free entry |
| NYE Gala 2024 | Past event | Historical data |
| Board Meeting | Draft | Unpublished event |
| Quarterly Review | Private | Limited visibility |

### Ticket Tiers

- **Early Bird**: Discounted, limited quantity
- **General Admission**: Standard pricing
- **VIP**: Premium tier with higher price
- **PWYC (Pay What You Can)**: Flexible pricing with minimum
- **Members-Only**: Restricted to organization members

### Questionnaires (3)

| Questionnaire | Evaluation Mode | Used By |
|---|---|---|
| Community Hackathon Application | Automatic | Community Hackathon |
| Art Exhibition Feedback | Manual review | Art Exhibition |
| Workshop Prerequisites | Hybrid (AI + human) | ML Workshop |

### Additional Data

- Potluck items with claimed/unclaimed status
- Direct and pending invitations
- Waitlist entries for sold-out events
- Various membership states (active, pending, blacklisted)

---

## Testing Tips

!!! tip "Exploring Different Permission Levels"
    Log in as different users to experience the platform from various perspectives:

    - **Owner** (`alice.owner@example.com`): Full organizational control
    - **Staff** (`bob.staff@example.com`): Event management without org admin
    - **Member** (`charlie.member@example.com`): Standard attendee experience
    - **Non-member** (`frank.nonmember@example.com`): Public-only access
    - **Multi-org** (`karen.multiorg@example.com`): Cross-organization membership

### Suggested Test Scenarios

| Scenario | How to Test |
|---|---|
| Ticket purchase flow | Log in as any user, buy a ticket for **FutureStack 2025** |
| RSVP with potluck | Log in as a member, RSVP to **Spring Potluck in the Park** |
| Waitlist behavior | Try to get a ticket for **ML Workshop** (sold out) |
| Past event data | View **NYE Gala 2024** for historical event information |
| Questionnaire flow | Apply to **Community Hackathon** as a non-member |
| Members-only access | Try accessing **Members-Only Wine Tasting** as `frank.nonmember@example.com` |
| Multi-language | Log in as `judy.multilang@example.com` (German locale) |
