# Bootstrap Events Command Documentation

This document describes the comprehensive test data created by the `bootstrap_events.py` management command. This dataset is designed to support frontend development and testing with realistic, varied scenarios.

## Running the Commands

### Bootstrap Events
```bash
python manage.py bootstrap_events
```

**⚠️ Warning:** This command creates data. Run it on a clean database or be prepared for duplicate/conflicting data.

### Reset Events (Demo Mode Only)
```bash
python manage.py reset_events
```

**⚠️ Warning:** This command **deletes ALL organizations and users with @example.com emails**, then runs `bootstrap_events` again. Only works when `DEMO_MODE=True`.

Use `--no-input` flag to skip confirmation (useful for periodic tasks):
```bash
python manage.py reset_events --no-input
```

## Overview

The bootstrap command creates:
- **2 Organizations** with different visibility settings
- **13 Users** with varied roles and relationships
- **15 Tags** for categorization
- **12 Events** covering diverse scenarios
- **2 Event Series** for recurring events
- **Multiple Ticket Tiers** with different pricing strategies
- **12 Potluck Items** for community events
- **Comprehensive User Relationships** (invitations, tickets, RSVPs, waitlists, payments)
- **3 Questionnaires** with different evaluation modes

---

## User Accounts

All users have the password: `password123`

### Organization Alpha (Revel Events Collective)

| Email | Name | Role | Additional Info |
|-------|------|------|-----------------|
| `alice.owner@example.com` | Alice Owner | Owner | Full control of org |
| `bob.staff@example.com` | Bob Staff | Staff Member | Can manage events |
| `charlie.member@example.com` | Charlie Member | Member | Member privileges |

### Organization Beta (Tech Innovators Network)

| Email | Name | Role | Additional Info |
|-------|------|------|-----------------|
| `diana.owner@example.com` | Diana Owner | Owner | Full control of org |
| `eve.staff@example.com` | Eve Staff | Staff Member | Can manage events |
| `frank.member@example.com` | Frank Member | Member | Member privileges |
| `george.attendee@example.com` | George Attendee | Member | Also a regular member |

### Regular Attendees

| Email | Name | Notes |
|-------|------|-------|
| `george.attendee@example.com` | George Attendee | Has various tickets and RSVPs |
| `hannah.attendee@example.com` | Hannah Attendee | Active in multiple events |
| `ivan.attendee@example.com` | Ivan Attendee | On waitlists |
| `julia.attendee@example.com` | Julia Attendee | Pending payment |

### Special Users

| Email | Name | Notes |
|-------|------|-------|
| `karen.multiorg@example.com` | Karen Multi | Member of BOTH organizations |
| `leo.pending@example.com` | Leo Pending | Has cancelled ticket |
| `maria.invited@example.com` | Maria Invited | On waitlists, invited to events |

### Pending Invitations (not registered)
- `vip.guest@example.com` - Invited to wine tasting

---

## Organizations

### 1. Revel Events Collective
- **Slug:** `revel-events-collective`
- **Owner:** Alice Owner
- **Visibility:** Public
- **Location:** Vienna, Austria
- **Members:** Charlie Member, Karen Multi
- **Staff:** Bob Staff
- **Settings:**
  - Accepts new members: Yes
  - Contact email: hello@revelcollective.example.com
  - Membership request methods: email, webform
- **Tags:** community, music, arts
- **Focus:** Community events, music, arts, wellness

### 2. Tech Innovators Network
- **Slug:** `tech-innovators-network`
- **Owner:** Diana Owner
- **Visibility:** Members Only
- **Location:** Berlin, Germany
- **Members:** Frank Member, Karen Multi, George Attendee
- **Staff:** Eve Staff
- **Settings:**
  - Accepts new members: Yes
  - Contact email: info@techinnovators.example.com
  - Membership request methods: email
- **Tags:** tech, professional, networking
- **Focus:** Tech workshops, conferences, professional networking

---

## Event Series

### 1. Monthly Tech Talks
- **Organization:** Tech Innovators Network
- **Slug:** `monthly-tech-talks`
- **Tags:** tech, educational, networking
- **Description:** Recurring monthly tech presentations and networking

### 2. Seasonal Community Gatherings
- **Organization:** Revel Events Collective
- **Slug:** `seasonal-community-gatherings`
- **Tags:** food, community, casual
- **Description:** Seasonal potluck celebrations

---

## Events

### 1. Summer Sunset Music Festival
- **Organization:** Revel Events Collective
- **Type:** Public
- **Status:** Open
- **Location:** Vienna, Austria (Donauinsel)
- **Start:** +45 days from now
- **Duration:** 8 hours
- **Requires Ticket:** Yes
- **Max Attendees:** 500
- **Waitlist:** Open
- **Tags:** music, casual, community
- **Ticket Tiers:**
  - Early Bird General Admission ($45) - 180/200 sold
  - General Admission ($65) - 45/250 sold
  - VIP Experience ($150) - 12/50 sold
- **User Relationships:**
  - 5 active tickets (attendee_1-3, multi_org_user, org_alpha_owner)
  - 1 pending ticket (attendee_4)
  - 1 cancelled ticket (pending_user)
  - 1 waitlist (invited_user)

### 2. Exclusive Wine Tasting & Pairing Dinner
- **Organization:** Revel Events Collective
- **Type:** Private
- **Status:** Open
- **Visibility:** Private
- **Location:** Vienna, Austria (Steirereck Restaurant)
- **Start:** +30 days from now
- **Duration:** 4 hours
- **Requires Ticket:** Yes
- **Max Attendees:** 40
- **Free for Members:** Yes
- **Tags:** food, formal, social
- **Featured Wines:** Austrian wines from Wachau Valley and Burgenland regions
- **Ticket Tiers:**
  - Exclusive Seating ($200) - Invitation only - 8/40 sold
- **User Relationships:**
  - 2 invitations (multi_org_user, attendee_1)
  - 1 pending invitation (vip.guest@example.com)
- **Questionnaire:** Wine Tasting Dinner Application (manual evaluation)

### 3. Hands-on Workshop: Building with AI APIs
- **Organization:** Tech Innovators Network
- **Type:** Members Only
- **Status:** Open
- **Visibility:** Public (but requires membership)
- **Location:** Berlin, Germany
- **Start:** +20 days from now
- **Duration:** 3 hours
- **Requires Ticket:** No (RSVP)
- **Max Attendees:** 30
- **RSVP Deadline:** +18 days from now
- **Tags:** tech, workshop, educational
- **User Relationships:**
  - 3 RSVPs (org_beta_member, org_beta_staff, multi_org_user)

### 4. Spring Community Potluck & Garden Party
- **Organization:** Revel Events Collective
- **Type:** Public
- **Status:** Open
- **Series:** Seasonal Community Gatherings
- **Location:** Vienna, Austria (Augarten)
- **Start:** +15 days from now
- **Duration:** 5 hours
- **Requires Ticket:** No (RSVP)
- **Potluck:** Enabled
- **Max Attendees:** 80
- **RSVP Deadline:** +13 days from now
- **Tags:** food, community, casual
- **User Relationships:**
  - 6 YES RSVPs
  - 1 MAYBE RSVP
  - 1 NO RSVP
  - 12 potluck items (6 suggested, 6 claimed)

### 5. FutureStack 2025: AI & Web3 Conference
- **Organization:** Tech Innovators Network
- **Type:** Public
- **Status:** Open
- **Location:** Berlin, Germany
- **Start:** +60 days from now
- **Duration:** 3 days
- **Requires Ticket:** Yes
- **Max Attendees:** 1000
- **Waitlist:** Open
- **Tags:** tech, conference, professional, networking
- **Ticket Tiers:**
  - Early Bird - Full Access (€399) - 295/300 sold
  - Standard - Full Access (€599) - 120/500 sold
  - Workshop Bundle (€899) - 34/100 sold
  - Member Discount (€299) - Members only - 23/100 sold
- **User Relationships:**
  - 1 member ticket (org_beta_member)
- **Questionnaire:** Code of Conduct Agreement (automatic evaluation)

### 6. Weekend Wellness Retreat
- **Organization:** Revel Events Collective
- **Type:** Public
- **Status:** Open
- **Location:** Vienna, Austria
- **Start:** +35 days from now
- **Duration:** 2.5 days (weekend)
- **Requires Ticket:** Yes
- **Max Attendees:** 25
- **Free for Members:** Yes
- **Tags:** wellness, casual, social
- **Ticket Tiers:**
  - Shared Room (€250) - 14/20 sold
  - Community Support Rate (€100-€250 PWYC) - 3/5 sold
- **User Relationships:**
  - 1 ticket (attendee_2)

### 7. Tech Founders Networking Happy Hour
- **Organization:** Tech Innovators Network
- **Type:** Members Only
- **Status:** Open
- **Visibility:** Members Only
- **Location:** Berlin, Germany
- **Start:** +10 days from now
- **Duration:** 3 hours
- **Requires Ticket:** No (RSVP)
- **Max Attendees:** 50
- **RSVP Deadline:** +9 days from now
- **Tags:** networking, professional, tech
- **User Relationships:**
  - 4 RSVPs (org_beta_member, org_beta_staff, multi_org_user, attendee_1)

### 8. Contemporary Art Exhibition Opening
- **Organization:** Revel Events Collective
- **Type:** Public
- **Status:** Open
- **Location:** London, UK
- **Start:** +25 days from now
- **Duration:** 4 hours
- **Requires Ticket:** No (RSVP)
- **RSVP Deadline:** +23 days from now
- **Tags:** arts, social, casual
- **User Relationships:**
  - 3 RSVPs (attendee_2, attendee_3, org_alpha_member)

### 9. New Year's Eve Gala 2024 (Past Event)
- **Organization:** Revel Events Collective
- **Type:** Public
- **Status:** Closed
- **Location:** Vienna, Austria (Palais Ferstel)
- **Start:** -90 days ago
- **Duration:** 1 day
- **Requires Ticket:** Yes
- **Max Attendees:** 200
- **Tags:** social, formal
- **Ticket Tiers:**
  - Gala Ticket ($250) - SOLD OUT
- **User Relationships:**
  - 1 checked-in ticket (attendee_1)
- **Purpose:** Testing past event views and historical data

### 10. Future Tech Summit (Draft)
- **Organization:** Tech Innovators Network
- **Type:** Public
- **Status:** Draft
- **Location:** Tokyo, Japan
- **Start:** +180 days from now
- **Duration:** 3 days
- **Requires Ticket:** Yes
- **Tags:** tech, conference
- **Purpose:** Testing draft event visibility (only visible to org owners/staff)

### 11. Tech Talk May: Scaling Microservices
- **Organization:** Tech Innovators Network
- **Type:** Members Only
- **Status:** Open
- **Series:** Monthly Tech Talks
- **Location:** Berlin, Germany
- **Start:** +40 days from now
- **Duration:** 2 hours
- **Requires Ticket:** No (RSVP)
- **Max Attendees:** 60
- **RSVP Deadline:** +38 days from now
- **Tags:** tech, educational, networking
- **User Relationships:**
  - 1 RSVP (org_beta_member)

### 12. Advanced Machine Learning Workshop (Sold Out)
- **Organization:** Tech Innovators Network
- **Type:** Public
- **Status:** Open
- **Location:** Berlin, Germany
- **Start:** +28 days from now
- **Duration:** 6 hours
- **Requires Ticket:** Yes
- **Max Attendees:** 20
- **Waitlist:** Open
- **Tags:** tech, workshop, educational
- **Ticket Tiers:**
  - Workshop Seat (€299) - SOLD OUT (20/20)
- **User Relationships:**
  - 3 waitlist entries (attendee_3, attendee_4, invited_user)
- **Purpose:** Testing sold-out event and waitlist functionality

---

## Tags Taxonomy

### Category Tags
- **music** (#FF6B6B) - Music-related events
- **food** (#4ECDC4) - Food and dining events
- **workshop** (#45B7D1) - Educational workshops
- **conference** (#96CEB4) - Professional conferences
- **networking** (#FFEAA7) - Networking events
- **arts** (#DDA15E) - Arts and culture
- **tech** (#6C5CE7) - Technology events
- **sports** (#00B894) - Sports and fitness
- **wellness** (#FDCB6E) - Health and wellness
- **community** (#E17055) - Community gatherings

### Vibe Tags
- **casual** (#74B9FF) - Relaxed atmosphere
- **formal** (#2D3436) - Formal dress code
- **educational** (#00CEC9) - Learning focused
- **social** (#FD79A8) - Social interaction
- **professional** (#636E72) - Professional setting

---

## Questionnaires

### 1. Code of Conduct Agreement
- **Evaluation Mode:** Automatic
- **Max Attempts:** 3
- **Min Score:** 100%
- **Questions:** 1 multiple choice (fatal)
- **Linked to:** FutureStack 2025 Conference
- **Purpose:** Simple mandatory agreement

### 2. Wine Tasting Dinner Application
- **Evaluation Mode:** Manual (requires human review)
- **Max Attempts:** 1
- **Min Score:** 60%
- **Sections:** 1 (About You)
- **Questions:**
  1. Code of Conduct (multiple choice, fatal)
  2. Interest in wine/culinary arts (free text)
  3. Wine knowledge level (multiple choice)
- **Linked to:** Exclusive Wine Tasting & Pairing Dinner
- **Purpose:** Application-based event admission

### 3. Tech Innovators Network Membership Application
- **Evaluation Mode:** Hybrid (AI + human review)
- **Max Attempts:** 2
- **Retake After:** 30 days
- **Min Score:** 70%
- **Sections:** 2 (Professional Background, Community Fit)
- **Questions:**
  1. Professional background (free text)
  2. Tech areas of interest (multiple choice, multiple answers)
  3. Community contribution (free text)
  4. Inclusive community commitment (multiple choice, fatal)
- **Linked to:** Organization (not specific events)
- **Purpose:** Organization membership screening

---

## Potluck Items (Spring Community Potluck)

### Host-Suggested Items (Unassigned)
1. Main Course (pasta, casserole, etc) - Serves 8-10
2. Fresh Garden Salad - Large bowl
3. Dessert - Serves 8-10
4. Beverages (non-alcoholic) - 2-3 liters
5. Paper plates, cups, napkins - For 80 people
6. Setup Help - 2-3 volunteers

### User-Contributed Items (Claimed)
1. **Homemade Lasagna** (attendee_1) - Vegetarian spinach ricotta
2. **Mediterranean Mezze Platter** (attendee_2) - Vegan
3. **Fresh Fruit Salad** (multi_org_user) - Seasonal berries
4. **Chocolate Brownies** (attendee_3) - 2 dozen
5. **Fresh Lemonade** (org_alpha_member) - With mint
6. **Acoustic Guitar Performance** (attendee_4) - 30 min set

---

## Payment & Ticket States

### Payment States Demonstrated
- **Succeeded:** Past event ticket (attendee_1 - NYE Gala)
- **Pending:** Summer festival ticket (attendee_4 - $65, expires in 30 min)
- **Refunded:** Cancelled summer festival ticket (pending_user)

### Ticket States Demonstrated
- **Active:** Multiple tickets across events
- **Pending:** Summer festival (attendee_4) - awaiting payment
- **Checked In:** Past event (attendee_1) - checked in by org_alpha_staff
- **Cancelled:** Summer festival (pending_user) - with refund

---

## Testing Scenarios Covered

### User Permissions & Visibility
✅ Anonymous user browsing public events
✅ Member accessing members-only content
✅ Staff managing organization events
✅ Owner with full organization control
✅ Multi-organization membership

### Event Types & Configurations
✅ Public events (open to all)
✅ Private events (invitation only)
✅ Members-only events (requires membership)
✅ Free events (no tickets)
✅ Ticketed events (various pricing)
✅ Events with RSVP deadlines
✅ Events with check-in windows
✅ Events with waitlists
✅ Events with potluck

### Ticket & Payment Flows
✅ Multiple ticket tiers per event
✅ Early bird pricing
✅ VIP/premium tiers
✅ Members-only tier
✅ Pay What You Can (PWYC) pricing
✅ Sales windows (start/end dates)
✅ Limited quantity tiers
✅ Sold out events
✅ Invitation-only tiers
✅ Free tickets for members
✅ Payment pending state
✅ Payment succeeded state
✅ Payment refunded state
✅ Ticket check-in flow

### User Relationships
✅ Event invitations (registered users)
✅ Pending invitations (email only)
✅ Custom invitation messages
✅ Invitation waiving questionnaire
✅ Invitation waiving purchase
✅ RSVP yes/no/maybe
✅ Waitlist entries
✅ Ticket purchases
✅ Ticket cancellations

### Questionnaires
✅ Automatic evaluation
✅ Manual evaluation
✅ Hybrid evaluation
✅ Multiple sections
✅ Multiple choice questions
✅ Free text questions
✅ Fatal questions (must pass)
✅ Optional questions
✅ Question shuffling options
✅ Max attempts limiting
✅ Retake cooldown periods

### Time-Based States
✅ Future events (upcoming)
✅ Past events (historical data)
✅ Draft events (planning phase)
✅ Open events (accepting registrations)
✅ Closed events (registration closed)
✅ Active ticket sales periods
✅ Expired ticket sales periods

### Organization Features
✅ Public organizations
✅ Members-only organizations
✅ Organization membership
✅ Organization staff with permissions
✅ Organization settings
✅ Multiple membership request methods
✅ Contact email configuration

### Event Series
✅ Recurring event patterns
✅ Multiple events in a series
✅ Series-level tags and descriptions

### Potluck Management
✅ Host-suggested items
✅ User-claimed items
✅ Various item types (food, supplies, labor, entertainment)
✅ Quantity and notes
✅ Assignee tracking

---

## Entity Relationship Summary

```
Organizations (2)
├── Revel Events Collective
│   ├── Owner: alice.owner@example.com
│   ├── Staff: bob.staff@example.com
│   ├── Members: charlie.member@example.com, karen.multiorg@example.com
│   ├── Events: Summer Festival, Wine Tasting, Potluck, Wellness Retreat, Art Opening, NYE Gala
│   └── Event Series: Seasonal Community Gatherings
│
└── Tech Innovators Network
    ├── Owner: diana.owner@example.com
    ├── Staff: eve.staff@example.com
    ├── Members: frank.member@example.com, karen.multiorg@example.com, george.attendee@example.com
    ├── Events: AI Workshop, FutureStack, Networking Hour, Tech Talk, ML Workshop, Future Summit
    └── Event Series: Monthly Tech Talks

Users (13)
├── Organization Owners (2)
├── Organization Staff (2)
├── Organization Members (4, including 1 multi-org)
├── Regular Attendees (4)
└── Special Users (2)

Events (12)
├── With Tickets (6)
│   ├── Multiple Tiers (4)
│   ├── Single Tier (2)
│   └── Sold Out (1)
├── With RSVP Only (5)
├── With Waitlist (2)
├── With Potluck (1)
├── With Questionnaires (2)
└── In Different States: Open (10), Closed (1), Draft (1)

Questionnaires (3)
├── Automatic Evaluation (1)
├── Manual Evaluation (1)
└── Hybrid Evaluation (1)

Relationships
├── Event Invitations (2)
├── Pending Invitations (1)
├── Tickets (11) - Active (7), Pending (1), Checked-in (1), Cancelled (1)
├── Payments (4) - Succeeded (1), Pending (1), Refunded (1)
├── RSVPs (18) - Yes (15), Maybe (1), No (1)
├── Waitlist Entries (4)
└── Potluck Items (12) - Suggested (6), Claimed (6)
```

---

## Tips for Frontend Development

### Testing Different User Views
1. Log in as **alice.owner@example.com** to test full organization owner capabilities
2. Log in as **bob.staff@example.com** to test staff member permissions
3. Log in as **charlie.member@example.com** to test regular member views
4. Log in as **karen.multiorg@example.com** to test multi-organization membership
5. Log out to test anonymous user views

### Testing Event Flows
- **Ticket Purchase:** Try FutureStack 2025 (multiple tiers available)
- **RSVP:** Try Spring Potluck (public, no ticket required)
- **Waitlist:** Try ML Workshop (sold out, waitlist active)
- **Invitation:** Check wine tasting invitations for multi_org_user
- **Potluck:** Manage potluck items for Spring Potluck
- **Past Event:** View NYE Gala 2024 for historical data
- **Draft Event:** Login as org owner to see draft events

### Testing Edge Cases
- **Nearly sold out:** Summer Festival early bird tier (180/200 sold)
- **Just opened tier:** Summer Festival general admission (sales start in future)
- **PWYC pricing:** Wellness Retreat community support rate
- **Expired payment:** Check pending_user's cancelled ticket
- **Check-in flow:** Past event has checked-in ticket

---

## Cleaning Up

### Option 1: Use reset_events Command (Recommended for Demo Mode)
```bash
python manage.py reset_events
```
This will automatically delete all organizations and @example.com users, then re-bootstrap fresh data.

**Requirements:** Only works when `DEMO_MODE=True` in settings.

### Option 2: Manual Cleanup
To remove all bootstrap data manually:
- Delete all events from both organizations
- Delete both organizations
- Delete all users with @example.com emails

### Option 3: Fresh Database
Simply reset your database and run migrations again.

---

## Questions or Issues?

If you find any issues with the bootstrap data or need additional scenarios, please update this command and regenerate the documentation.
