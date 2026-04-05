# Revel User Journeys V2

This document maps every user journey through the Revel platform, organized by persona. Its purpose is to serve as the source of truth for Playwright E2E test cases on the frontend. Each journey describes the **what** and **why** from the user's perspective — the exact UI steps and assertions will live in the test suite.

> **Last updated**: 2026-04-01 (v1.47.0)

---

## Table of Contents

- [Platform Overview](#platform-overview)
- [Personas](#personas)
- [Journey 1: Guest / Anonymous User](#journey-1-guest--anonymous-user)
- [Journey 2: New User Registration & Onboarding](#journey-2-new-user-registration--onboarding)
- [Journey 3: Authenticated User (No Organization)](#journey-3-authenticated-user-no-organization)
- [Journey 4: Organization Member](#journey-4-organization-member)
- [Journey 5: Event Attendee (RSVP Flow)](#journey-5-event-attendee-rsvp-flow)
- [Journey 6: Event Attendee (Ticket Flow)](#journey-6-event-attendee-ticket-flow)
- [Journey 7: Guest Attendee (No Account)](#journey-7-guest-attendee-no-account)
- [Journey 8: Organization Owner](#journey-8-organization-owner)
- [Journey 9: Organization Staff](#journey-9-organization-staff)
- [Journey 10: Event Administration](#journey-10-event-administration)
- [Journey 11: Questionnaire Flows](#journey-11-questionnaire-flows)
- [Journey 12: Invitation & Token Flows](#journey-12-invitation--token-flows)
- [Journey 13: Blacklist, Whitelist & Moderation](#journey-13-blacklist-whitelist--moderation)
- [Journey 14: Potluck Coordination](#journey-14-potluck-coordination)
- [Journey 15: Notifications & Communication](#journey-15-notifications--communication)
- [Journey 16: Billing, Payments & VAT](#journey-16-billing-payments--vat)
- [Journey 17: Account Lifecycle (Security, GDPR, Deletion)](#journey-17-account-lifecycle-security-gdpr-deletion)
- [Journey 18: Event Series & Following](#journey-18-event-series--following)
- [Journey 19: Venue & Seating](#journey-19-venue--seating)
- [Journey 20: Discount Codes](#journey-20-discount-codes)
- [Journey 21: Referral Program](#journey-21-referral-program)
- [Journey 22: Attendee Invoicing](#journey-22-attendee-invoicing)
- [Cross-Cutting Concerns](#cross-cutting-concerns)
- [Gap-Fill Interview Questions](#gap-fill-interview-questions)

---

## Platform Overview

Revel is a privacy-focused, community-first event management and ticketing platform. It serves organizers who run community events — from queer meetups and kink parties to tech conferences and potluck dinners.

**Core differentiators** (inferred — to be validated):
- Privacy-first: no tracking, minimal data collection, GDPR-native
- Self-hostable: organizations own their data and infrastructure
- Questionnaire-based access control: events can screen attendees via questionnaires
- Flexible attendance models: free RSVP, ticketed (fixed, PWYC, offline), invitation-only, members only
- Community features: potluck coordination, dietary tracking, membership tiers
- Blacklist/whitelist system with fuzzy name matching for safer spaces
- Multi-channel notifications: in-app, email, Telegram
- Internationalized: English, German, Italian

---

## Personas

| Persona | Description | Auth State |
|---------|-------------|------------|
| **Guest** | Anonymous visitor browsing events/orgs | Not logged in |
| **New User** | Just registered, verifying email | Partially authenticated |
| **Authenticated User** | Logged in, no org affiliation | Fully authenticated |
| **Organization Member** | Member of one or more orgs (ACTIVE/PAUSED status, optional tier) | Fully authenticated |
| **Event Attendee** | Has RSVP'd YES or holds an active ticket | Fully authenticated |
| **Guest Attendee** | Attends event without account (guest checkout / guest RSVP) | Not logged in |
| **Organization Owner** | Created the org, has all permissions implicitly | Fully authenticated |
| **Organization Staff** | Assigned staff role with granular JSON-based permissions | Fully authenticated |
| **Waitlisted User** | Joined waitlist for a full event | Fully authenticated |
| **Referrer** | Has a referral code and earns payouts from referred users' ticket purchases | Fully authenticated |

---

## Journey 1: Guest / Anonymous User

### 1.1 Browse Public Events
- Land on `/events`
- See list/calendar toggle view
- Filter by: category, date range, price type, location
- Sort by distance (if location shared)
- Paginate through results
- Click event → navigate to `/events/[org_slug]/[event_slug]`

### 1.2 View Event Details (Public Event)
- See: cover art, name, dates, location (address visibility rules apply), organizer info
- See: description, ticket tiers (public tiers only), attendee count
- See: resources (PUBLIC visibility only)
- See: potluck items (if potluck_open and event is public)
- See: announcements (if any public ones exist)
- Do NOT see: full attendee list (requires auth), private resources, member-only tiers
- CTA: "Log in to RSVP" or "Log in to buy tickets" or "Register"

### 1.3 Browse Organizations
- Navigate to `/organizations`
- See public organizations with search/filter
- Click org → `/org/[slug]`
- See: org profile, public events, public resources, social links

### 1.4 View Event Series
- Navigate to `/events/[org_slug]/series/[series_slug]`
- See series info + list of events in the series

### 1.5 Preview Invitation Token
- Visit `/join/event/[token_id]` or `/join/org/[token_id]`
- See what the token grants (staff, member, attendee, etc.)
- See token expiration and usage limits
- CTA: "Log in to claim" or "Register to claim"

### 1.6 Legal Pages
- Visit `/legal/privacy` and `/legal/terms`
- Content is localized (en/de/it)

### 1.7 Marketing Pages
- Visit various marketing pages (`/community-first-event-platform`, etc.)
- Localized variants exist for de/it

---

## Journey 2: New User Registration & Onboarding

### 2.1 Email Registration
- Navigate to `/register`
- Fill: email, password (8+ chars, uppercase, lowercase, number, special char)
- Accept terms of service
- Submit → redirected to `/register/check-email`
- Receive verification email
- Click verification link → account verified → auto-login → redirected to dashboard

### 2.2 Google SSO Registration
- Navigate to `/login`
- Click "Sign in with Google"
- Complete Google OAuth flow
- If new user: account created automatically from Google profile (name, picture, locale)
- If existing user: linked/logged in
- Redirected to dashboard

### 2.3 Registration with Pending Invitation
- User received invitation email for an event/org before registering
- Register → verify email → pending invitations auto-linked
- PendingEventInvitation → converted to EventInvitation
- Redirected to dashboard showing pending invitations

### 2.4 Guest-to-User Upgrade
- A guest account (created via guest checkout) exists with the email
- User registers with same email → activation link sent
- On verification, guest account upgraded to full user
- Previous guest tickets/RSVPs preserved

### 2.5 Registration Blocked by Global Ban
- Email or domain is globally banned
- Registration fails with appropriate error message

---

## Journey 3: Authenticated User (No Organization)

### 3.1 Dashboard
- Navigate to `/dashboard`
- See: personalized greeting, quick actions (create event, create org)
- See: empty state for events/invitations (if no activity yet)
- Filter events by: owner, staff, member, RSVP, ticketed, invited, subscribed

### 3.2 Profile Management
- Navigate to `/account/profile`
- Edit: first name, last name, preferred name, pronouns
- Edit: language preference (en/de/it)
- Edit: bio (Markdown)
- Upload/change profile picture
- View email and verification status
- Resend verification email (with cooldown)

### 3.3 Dietary Preferences
- On profile page or dedicated section
- Add dietary preferences (vegetarian, vegan, etc.)
- Add dietary restrictions (allergies, intolerances, dislikes) linked to specific food items
- Mark as public or private
- Add notes to restrictions

### 3.4 Notification Preferences
- Navigate to `/account/notifications`
- Toggle notification types on/off
- Configure channels (in-app, email, Telegram)
- Set digest frequency (immediate, hourly, daily, weekly)
- Set digest send time

### 3.5 Security Settings
- Navigate to `/account/security`
- Set up 2FA (TOTP): scan QR code, verify with code
- Disable 2FA (requires TOTP verification)
- Change password

### 3.6 Connect Telegram
- On profile page
- Connect Telegram account via OTP
- Receive notifications via Telegram bot
- Disconnect Telegram

### 3.7 Billing Profile (for Referral Payouts)
- Navigate to `/account/billing`
- Set: billing name, billing address, billing email
- Set VAT ID (VIES-validated for EU) — required for self-billing invoices
- Agree to self-billing terms (required before payouts are processed)
- Connect Stripe account for receiving referral payouts

### 3.8 Create Organization
- Navigate to `/create-org`
- Fill: name, contact email, city, address, description
- Submit → organization created, user is owner
- Redirected to org admin dashboard

### 3.9 Follow Organization
- Visit org public page
- Click "Follow" button
- Configure: notify on new events, notify on announcements
- View followed orgs in `/dashboard/following`

### 3.10 Request Organization Membership
- Visit org public page (org must accept_membership_requests)
- Click "Request Membership"
- Submit optional message
- Wait for approval → notification when approved/rejected

---

## Journey 4: Organization Member

### 4.1 Become a Member
Multiple paths:
- **Membership request approved** by staff/owner
- **Claim organization token** (invitation link) that grants_membership
- **Direct addition** by owner/staff

### 4.2 Member-Specific Access
- See member-only events (EventType.MEMBERS_ONLY)
- See member-only ticket tiers (visibility: MEMBERS_ONLY)
- See member-only resources (visibility: MEMBERS_ONLY)
- Bypass fuzzy blacklist matching (trusted as active member)
- May be exempt from questionnaires (if members_exempt=True on questionnaire)
- Access events in series if series-level questionnaire already passed

### 4.3 Membership Tier Benefits
- Member may have an assigned MembershipTier
- Certain ticket tiers restricted to specific membership tiers
- Announcements can target specific tiers
- Tier assignment set by staff on approval or later

### 4.4 Membership Status Changes
- ACTIVE → PAUSED (by staff): limited access
- ACTIVE → BANNED (via blacklist): full block
- ACTIVE → CANCELLED: removed from org
- PAUSED → ACTIVE: restored by staff

---

## Journey 5: Event Attendee (RSVP Flow)

**Precondition**: Event has `requires_ticket=False`.

### 5.1 Discover & View Event
- Find event via browse, search, invitation, or direct link
- View event details page

### 5.2 Check Eligibility (Implicit)
The backend eligibility pipeline runs automatically to decide whether and how the user can respond to the event.

For the authoritative list of gates and their evaluation order, see:
`docs/architecture/eligibility-pipeline.md`
User sees one of:
- "RSVP" buttons (YES / MAYBE / NO)
- "Complete questionnaire first" prompt
- "Request invitation" button (for invitation-required events)
- "Join waitlist" button (if at capacity and waitlist_open)
- Error explaining why they can't attend

### 5.3 RSVP
- Click YES → RSVP confirmed, attendee_count incremented
- Click MAYBE → recorded but doesn't count toward capacity
- Click NO → recorded, frees spot if was YES
- Already RSVP'd YES? Can always change to MAYBE/NO (even if eligibility changed — prevents trapping)

### 5.4 View Attendee Experience
After RSVP YES:
- See full event address (if address_visibility allows)
- See attendee list (based on AttendeeVisibilityFlag settings)
- See attendee-only resources
- See dietary summary and pronoun distribution (if `public_pronoun_distribution` enabled)
- Access potluck coordination (if enabled)
- Receive event announcements

### 5.5 Join Waitlist
If event at capacity and waitlist_open:
- Click "Join Waitlist"
- Receive notification when spot opens
- Then complete RSVP flow

### 5.6 Leave Waitlist
- Click "Leave Waitlist"
- Removed from waitlist

---

## Journey 6: Event Attendee (Ticket Flow)

**Precondition**: Event has `requires_ticket=True`.

### 6.1 View Ticket Tiers
- See available tiers (filtered by visibility and user role)
- Each tier shows: name, price, description, availability, sales window
- Tiers sorted by display_order
- Member-restricted tiers only visible to matching members
- Staff-only tiers visible only to staff
- Invitation-linked tiers: when `restrict_visibility_to_linked_invitations=True`, only users whose invitation links to that tier can see it
- Invitation-linked purchase: when `restrict_purchase_to_linked_invitations=True`, only users whose invitation links to that tier can buy

### 6.2 Purchase — Free Tier
- Select tier → ticket created immediately with status ACTIVE
- Receive ticket confirmation notification
- Ticket appears in `/dashboard/tickets`

### 6.3 Purchase — Fixed Price (Online/Stripe)
- Select tier → Stripe checkout session created
- Redirected to Stripe payment page
- Complete payment → webhook fires → ticket PENDING → ACTIVE
- Receive confirmation notification
- Can apply discount code at checkout

### 6.4 Purchase — Fixed Price (Offline)
- Select tier → ticket created with status PENDING
- See manual payment instructions from tier
- Staff confirms payment → ticket PENDING → ACTIVE
- Receive confirmation notification

### 6.5 Purchase — At-The-Door
- Select tier → ticket created with status ACTIVE immediately
- Represents commitment to attend and pay at door
- Counts toward attendee_count

### 6.6 Purchase — Pay What You Can (PWYC)
- Select tier → choose amount (between pwyc_min and pwyc_max)
- Payment method determines flow (same as fixed price variants)
- Admin can override PWYC bounds

### 6.7 Seat Selection
Depends on tier's seat_assignment_mode:
- **NONE**: No seats, general admission
- **RANDOM**: System assigns seat randomly from sector
- **USER_CHOICE**: User picks seat from interactive seat map

### 6.8 Batch Purchase
- Can purchase multiple tickets at once (up to max_tickets_per_user)
- Per-user limits checked at both tier and event level
- Capacity checked atomically with row locking

### 6.9 Ticket Management
- View tickets in `/dashboard/tickets`
- Filter by status (active, pending, checked_in, cancelled)
- Download ticket as PDF
- Add ticket to Apple Wallet (.pkpass)
- View ticket QR code for check-in

### 6.10 Check-In
- Arrive at event
- Staff scans QR code or searches by name
- Ticket status: ACTIVE → CHECKED_IN
- For PWYC offline tickets: price_paid recorded at check-in
- Check-in window enforced (check_in_starts_at to check_in_ends_at)

### 6.11 Apply Discount Code
- Enter discount code during checkout
- Code validated: active, within dates, not exceeded max_uses, applicable to tier/event
- Discount applied: percentage or fixed amount off

---

## Journey 7: Guest Attendee (No Account)

**Precondition**: Event has `can_attend_without_login=True`.

### 7.1 Guest RSVP
- View public event
- Click "RSVP as Guest"
- Provide: name, email
- RSVP recorded without account creation
- Guest RevelUser created (guest=True)

### 7.2 Guest Ticket Checkout
- View public event with ticket tiers
- Click "Buy as Guest"
- Provide: name, email
- Follow payment flow for tier type
- Guest RevelUser created with ticket

### 7.3 Guest-to-User Conversion
- Guest later registers with same email
- Account upgraded, all guest activity preserved
- Full dashboard access to previous tickets/RSVPs

---

## Journey 8: Organization Owner

### 8.1 Create Organization
- Navigate to `/create-org`
- Fill required fields (name, contact email, city)
- Auto-become owner with all permissions
- Contact email auto-verified if matches user's verified email

### 8.2 Organization Settings
- Navigate to `/org/[slug]/admin/settings`
- Edit: name, description, contact email, address, social links
- Upload: logo, cover art
- Set visibility: PUBLIC, UNLISTED, PRIVATE, MEMBERS_ONLY
- Toggle: accept_membership_requests

### 8.3 Stripe Connect Setup
- Navigate to billing settings
- Click "Connect Stripe"
- Complete Stripe Connect onboarding
- Verify account connection
- Required for online payment ticket tiers

### 8.4 VAT & Billing Configuration
- Set VAT ID (VIES-validated for EU)
- Set billing name (legal entity name for invoices; auto-populated from VIES if available, falls back to org name)
- Configure billing address and email
- Set VAT rate
- View platform fee invoices

### 8.5 Staff Management
- Navigate to `/org/[slug]/admin/members` → Staff tab
- Promote member to staff
- Assign granular permissions per staff member:
  - view_organization_details, create/edit/delete_event
  - create/edit/delete_event_series
  - open_event, close_event
  - manage_tickets, check_in_attendees
  - manage_event, invite_to_event
  - edit_organization, manage_members
  - manage_potluck
  - create/edit/delete/evaluate_questionnaire
  - send_announcements
- Set per-event permission overrides
- Remove staff

### 8.6 Member Management
- View members list with search/pagination
- Change member status (ACTIVE/PAUSED/CANCELLED/BANNED)
- Assign/change membership tier
- Remove member
- Approve/reject membership requests with tier assignment

### 8.7 Membership Tiers
- Create/edit/delete tiers
- Reorder tiers (display_order)
- Tiers used for: ticket tier restrictions, announcement targeting, token assignments

### 8.8 Organization Tokens (Invitation Links)
- Create shareable token links
- Configure: grants_membership, grants_staff_status, membership_tier
- Set: max_uses, expiration
- Share link → anyone with link can claim
- View token usage

### 8.9 Blacklist Management
- Navigate to `/org/[slug]/admin/blacklist`
- Add entry by: email, name, phone, telegram username, or link to existing user
- Entries with user FK: hard block (no recourse)
- Entries with name only: fuzzy block (85% match threshold)
- Consequence: removes from staff, bans membership, blocks event access
- Cannot blacklist self (owner safeguard)

### 8.10 Venue Management
- Navigate to `/org/[slug]/admin/venues`
- Create venues with: name, address, city, capacity, description
- Create sectors within venues: name, capacity (hard limit for GA), shape (polygon for visual map)
- Create seats within sectors: label, row/number, position, accessibility flags
- Bulk create seats
- Link venues to events and ticket tiers

### 8.11 Resource Management
- Create resources: files, links, or text content
- Set visibility: PUBLIC, UNLISTED, PRIVATE, MEMBERS_ONLY, STAFF_ONLY, ATTENDEES_ONLY
- Attach to organization page, events, or event series

---

## Journey 9: Organization Staff

### 9.1 Access Based on Permissions
Staff see admin pages filtered by their permission set:
- With `create_event`: can create new events
- With `manage_tickets`: can manage ticket tiers and tickets
- With `check_in_attendees`: can use check-in interface
- With `manage_members`: can manage members, approve requests
- With `evaluate_questionnaire`: can review questionnaire submissions
- With `send_announcements`: can create and send announcements
- Per-event overrides: permissions can be granted/revoked for specific events

### 9.2 Event Management (with appropriate permissions)
Same as owner event management but filtered by permissions.

---

## Journey 10: Event Administration

### 10.1 Create Event
- Navigate to `/org/[slug]/admin/events/new`
- Fill: name, slug, description, dates (start/end), location
- Configure:
  - Event type: PUBLIC, UNLISTED, PRIVATE, MEMBERS_ONLY
  - Status: starts as DRAFT
  - Requires ticket: yes/no
  - Requires full profile: yes/no
  - Can attend without login: yes/no (enables guest flows)
  - Accept invitation requests: yes/no
  - Apply before deadline
  - RSVP before deadline
  - Max attendees
  - Max tickets per user
  - Waitlist open: yes/no
  - Potluck open: yes/no
  - Address visibility rules
  - Check-in window (starts_at, ends_at)
  - Invitation message (default message for invitations)
- Link to: venue, event series
- Upload: logo, cover art
- Add tags

### 10.2 Event Lifecycle
```
DRAFT → OPEN → CLOSED
  ↓              ↓
  └→ CANCELLED ←─┘
```
- **DRAFT**: Only visible to staff/owner. Configure everything.
- **OPEN**: Visible per event_type rules. Accepts RSVPs/tickets.
- **CLOSED**: No new RSVPs/tickets. Event still visible.
- **CANCELLED**: Event cancelled. Notifications sent.

### 10.3 Edit Event
- All fields editable while in DRAFT
- Limited fields editable when OPEN/CLOSED (to be clarified)
- Update slug separately (PATCH)
- Duplicate event (copies settings with new name/dates)

### 10.4 Ticket Tier Management
- Create tiers with: name, price, description, payment method
- Configure:
  - Visibility: PUBLIC, PRIVATE, MEMBERS_ONLY, STAFF_ONLY
  - Purchasable by: PUBLIC, MEMBERS, INVITED, INVITED_AND_MEMBERS
  - Payment method: ONLINE, OFFLINE, AT_THE_DOOR, FREE
  - Price type: FIXED or PWYC (with min/max)
  - Sales window (start/end dates)
  - Total quantity (capacity)
  - Max tickets per user
  - Restricted to specific membership tiers
  - Restrict visibility to invitation-linked tiers (`restrict_visibility_to_linked_invitations`)
  - Restrict purchase to invitation-linked tiers (`restrict_purchase_to_linked_invitations`)
  - Linked venue/sector
  - Seat assignment mode: NONE, RANDOM, USER_CHOICE
  - VAT rate
- Reorder tiers (display_order)

### 10.5 Manage Tickets
- View all tickets with status, search, filters
- Create tickets manually (on behalf of users)
- Update ticket status
- Bulk update tickets
- Check in tickets (mark CHECKED_IN)
- Cancel tickets
- View payment details

### 10.6 Manage RSVPs
- View all RSVPs with status
- Create RSVP on behalf of user
- Update RSVP status
- Search and filter

### 10.7 Manage Invitations
- Create direct invitations (to registered users)
- Create pending invitations (to unregistered emails)
- Configure waivers per invitation:
  - waives_questionnaire
  - waives_purchase
  - overrides_max_attendees
  - waives_membership_required
  - waives_rsvp_deadline
  - waives_apply_deadline
- Link specific ticket tiers to invitation (for tier-restricted visibility/purchase)
- Set custom message (defaults to event's invitation_message)
- View sent invitations and pending invitations
- Delete invitations

### 10.8 Manage Invitation Requests
- View pending requests from users
- Approve (optionally assign tier) → creates EventInvitation
- Reject → sends notification

### 10.9 Event Tokens (Shareable Links)
- Create tokens that grant invitation to event
- Configure: ticket_tier, invitation_payload (waivers), max_uses, expiration
- Share link → claimable by anyone

### 10.10 Manage Waitlist
- View waitlist entries
- Remove users from waitlist
- Waitlist auto-processed when capacity increases or cancellations occur

### 10.11 Announcements
- Create draft announcement
- Target: event attendees, all members, specific tiers, staff only
- Preview recipient count before sending
- Send → notification dispatched to all targets
- Past visibility flag: controls if announcement visible after event

### 10.12 Event Resources
- Attach files, links, or text to event
- Set per-resource visibility

### 10.13 View Event Statistics
- Attendee count
- Dietary summary (aggregated restrictions/preferences)
- Pronoun distribution (visible to non-staff only when `public_pronoun_distribution=True`)
- Ticket sales by tier

---

## Journey 11: Questionnaire Flows

### 11.1 Create Questionnaire (Organizer)
- Navigate to `/org/[slug]/admin/questionnaires/new`
- Configure:
  - Name, description
  - Type: ADMISSION, MEMBERSHIP, FEEDBACK, GENERIC
  - Evaluation mode: AUTOMATIC, MANUAL, HYBRID
  - LLM backend: MOCK, SANITIZING, SENTINEL
  - LLM guidelines (instructions for AI evaluator)
  - Min passing score (0-100)
  - Shuffle questions/sections
  - Can retake after (duration)
  - Max attempts (0 = unlimited)
  - Members exempt: yes/no
  - Per event: yes/no (separate submission per event vs. reusable across events)
  - Requires evaluation: yes/no
  - Max submission age (duration — how long a passing submission remains valid)
- Add questions:
  - **Multiple Choice**: options, correct answers, scoring weight, shuffleable, allow multiple selection. Note: single-answer questions (`allow_multiple_answers=False`) can still have multiple options marked as correct — the guest selects one, and any correct option earns full points.
  - **Free Text**: max 1000 chars, evaluated by LLM
  - **File Upload**: mime type restrictions, max files, max size
- Questions support conditional branching (depends_on_option_id)
- Organize into sections (ordered)

### 11.2 Attach Questionnaire to Event
- From questionnaire admin: attach to specific events or event series
- Questionnaire appears in event eligibility pipeline

### 11.3 Fill Questionnaire (Attendee)
- Navigate to event page
- Eligibility check shows "Complete questionnaire" prompt
- Navigate to `/events/[org_slug]/[event_slug]/questionnaire/[id]`
- Answer all required questions
- Conditional questions appear/disappear based on selections
- Upload files if required (ClamAV scanned)
- Submit

### 11.4 Automatic Evaluation
- Celery task picks up submission
- Multiple choice scored per-option: selecting a correct option awards `positive_weight`, selecting an incorrect option deducts `negative_weight`
- Free text evaluated by LLM (if configured)
- Score calculated, compared to min_score
- Result: APPROVED (score >= min_score) or REJECTED
- Notification sent to user
- If APPROVED: user can now proceed with RSVP/ticket

### 11.5 Manual/Hybrid Evaluation
- Submission goes to PENDING_REVIEW
- Organizer reviews in `/org/[slug]/admin/questionnaires/[id]/submissions/[submission_id]`
- See: all answers, AI evaluation (if hybrid), score
- Approve or reject with notes
- Notification sent to user

### 11.6 Retake Questionnaire
- `max_attempts` controls **whether** retakes are allowed (0 = unlimited)
- `can_retake_after` controls the **cooldown** between attempts (None or zero = immediate retake)
- User resubmits after cooldown elapses (if any)
- Previous submission replaced

### 11.7 Export Submissions
- Organizer exports all submissions (for offline review, record-keeping)

---

## Journey 12: Invitation & Token Flows

### 12.1 Direct Event Invitation (Registered User)
- Organizer creates invitation for existing user (by email/user)
- User receives notification (in-app + email)
- Invitation carries waivers (skip questionnaire, override capacity, etc.)
- User views invitation in `/dashboard/invitations`
- User proceeds to RSVP/ticket (with waivers applied)

### 12.2 Pending Event Invitation (Unregistered Email)
- Organizer invites email that's not registered
- PendingEventInvitation created
- Email sent to invitee
- When user registers with that email → PendingEventInvitation converted to EventInvitation
- Waivers preserved

### 12.3 Event Token (Shareable Link)
- Organizer creates event token
- Shares link: `/join/event/[token_id]`
- Anyone with link can:
  - Preview what the token grants
  - Claim (if authenticated) → EventInvitation created
  - If not authenticated → redirect to login/register → claim after auth
- Token can be limited by max_uses, expiration
- Expired or fully-used tokens return **410 Gone** with a specific message ("expired" vs "used up") instead of 404, allowing the frontend to show contextual guidance

### 12.4 Organization Token (Shareable Link)
- Owner creates org token
- Shares link: `/join/org/[token_id]`
- Token can grant: membership, staff status, specific tier
- Anyone with link can claim
- Limited by max_uses, expiration
- Expired or fully-used tokens return **410 Gone** (same behavior as event tokens)

### 12.5 Invitation Request Flow
- User views event with accept_invitation_requests=True
- Clicks "Request Invitation"
- Submits optional message
- Organizer reviews in event admin
- Approve → EventInvitation created with optional tier
- Reject → notification to user

---

## Journey 13: Blacklist, Whitelist & Moderation

### 13.1 Hard Blacklist
- Organizer adds entry with: user FK, email, phone, or telegram
- Immediate consequences:
  - Removed from staff
  - Membership set to BANNED
  - Blocked from all org events (eligibility gate fails)
- No recourse for user

### 13.2 Fuzzy Blacklist (Name-Based)
- Organizer adds entry with: first_name, last_name, preferred_name
- When user tries to access event:
  - RapidFuzz compares all name variants (85% threshold)
  - If match: user sees "You may be on a restricted list"
  - User prompted to submit whitelist request

### 13.3 Whitelist Request Flow
- User blocked by fuzzy match
- Submits whitelist request with message explaining who they are
- Organizer reviews in admin
- Approve → user whitelisted (future access unblocked)
- Reject → user remains blocked, notification sent

### 13.4 Auto-Linking
- When user registers or connects Telegram:
  - System checks for matching blacklist entries
  - Auto-links fuzzy matches to user FK (upgrading to hard block)
  - Applies consequences (ban, staff removal)

### 13.5 Member Bypass
- Active organization members bypass fuzzy blacklist matching
- Only hard blacklist (with FK) blocks members

---

## Journey 14: Potluck Coordination

### 14.1 View Potluck List
- Event has potluck_open=True
- Navigate to event page → potluck section
- See all items: name, type, quantity, who's bringing it

### 14.2 Add Potluck Item
- Click "Add Item"
- Fill: name, item_type (FOOD, DRINK, SUPPLIES, etc.), quantity, notes
- Item can be "suggested" (by organizer) or user-created

### 14.3 Claim Item
- See unclaimed item
- Click "I'll bring this"
- Item assigned to user

### 14.4 Unclaim Item
- Click "I can't bring this anymore"
- Item released back to pool

### 14.5 Auto-Release on Attendance Change
- User RSVPs NO/MAYBE or cancels ticket
- Claimed potluck items automatically released

### 14.6 Item Types
FOOD, MAIN_COURSE, SIDE_DISH, DESSERT, DRINK, ALCOHOL, NON_ALCOHOLIC, SUPPLIES, LABOR, ENTERTAINMENT, SEXUAL_HEALTH, TOYS, CARE, TRANSPORT, MISC

---

## Journey 15: Notifications & Communication

### 15.1 Notification Channels
- **In-app**: Bell icon with unread count, notification list
- **Email**: Immediate or digest (hourly, daily, weekly)
- **Telegram**: Via connected Telegram account

### 15.2 Notification Types (non-exhaustive)
- Account: registration welcome, email verified, password reset
- Events: created, opened, closed, cancelled, reminder
- Tickets: created, purchased, cancelled, refunded, checked in
- Invitations: sent, accepted, pending conversion
- Memberships: granted, request created/approved/rejected
- Questionnaires: submitted, evaluation complete
- RSVPs: confirmed
- Potluck: item claimed/unclaimed
- Whitelist: request created/approved/rejected
- Announcements: new announcement

### 15.3 Notification Preferences
- Silence all notifications (global toggle)
- Per-channel enable/disable
- Per-notification-type settings
- Digest frequency and send time
- Event reminder toggle

### 15.4 Announcements
- Organizer creates and sends announcement
- Recipients determined by targeting (event attendees, members, tiers, staff)
- Delivered via configured channels
- Visible on event/org page (with past_visibility flag)

### 15.5 System Announcements (Platform Admin)
- Platform administrators can broadcast announcements to all active users
- Configure: title, rich-text body, optional URL
- Option to include guest accounts
- Delivered via all configured notification channels in async batches

### 15.6 Unsubscribe
- Every email has unsubscribe link
- Click → `/unsubscribe` page
- One-click unsubscribe (token-based, no auth needed)

### 15.7 Notification Inbox
- Navigate to notification bell / inbox
- See all notifications (paginated, filterable)
- Mark as read/unread
- Mark all as read

---

## Journey 16: Billing, Payments & VAT

### 16.1 Stripe Connect Onboarding (Organizer)
- Organization owner initiates Stripe Connect
- Redirected to Stripe onboarding
- Completes account setup (banking, identity)
- Verify connection in Revel

### 16.2 Ticket Purchase (Buyer)
- Select tier → see price (VAT-inclusive)
- Apply discount code (if available)
- Redirected to Stripe Checkout
- Complete payment
- Receive ticket + receipt

### 16.3 VAT Handling
- **Ticket sales**: VAT-inclusive pricing for consumers (extracted from gross using org's vat_rate)
- **Platform fees (B2B)**:
  - Same country as platform: domestic VAT extracted
  - Different EU country + valid VAT ID: reverse charge (no VAT)
  - Different EU country without VAT ID: platform's domestic VAT
  - Outside EU: no VAT (export of services)

### 16.4 Platform Fee Invoices
- Generated per transaction or periodically
- Viewable in `/org/[slug]/admin/billing/invoices`
- Download as PDF
- Credit notes for refunds

### 16.5 Discount Codes
- Organizer creates codes in `/org/[slug]/admin/discount-codes`
- Configure: code, type (percentage/fixed), value, valid dates, max uses, per-user limit
- Scope to: events, event series, specific tiers
- Min purchase amount
- Usage tracking

### 16.6 Attendee Invoicing
See [Journey 22: Attendee Invoicing](#journey-22-attendee-invoicing) for the full flow.

---

## Journey 17: Account Lifecycle (Security, GDPR, Deletion)

### 17.1 Two-Factor Authentication
- Navigate to `/account/security`
- Enable 2FA: scan QR code with authenticator app → verify with code
- Future logins require TOTP code after password
- Disable 2FA: verify with current TOTP code

### 17.2 Password Reset
- Navigate to `/password-reset`
- Enter email → receive reset link
- Click link → set new password
- Endpoint returns generic response (prevents user enumeration)

### 17.3 GDPR Data Export
- Navigate to `/account/privacy`
- Click "Export my data"
- Celery task gathers all personal data
- Download link provided when ready

### 17.4 Account Deletion
- Navigate to `/account/privacy`
- Click "Delete my account"
- Confirmation email sent
- Click confirmation link → account deleted
- Payment records cascade-deleted (Stripe retains financial records)

### 17.5 Email Verification Reminders
- System tracks: last_reminder_sent_at, final_warning_sent_at, deactivation_email_sent_at
- Unverified users receive escalating reminders

---

## Journey 18: Event Series & Following

### 18.1 View Event Series
- Browse series at `/events/[org_slug]/series/[series_slug]`
- See: series info, list of events, shared branding

### 18.2 Follow Event Series
- Click "Follow" on series page
- Configure: notify on new events
- See followed series in `/dashboard/following`
- Unfollow anytime

### 18.3 Series-Level Questionnaires
- Questionnaire attached to event series
- User completes once → valid for all events in series
- Unless per_event=True (then must complete per event)
- max_submission_age can expire old submissions

---

## Journey 19: Venue & Seating

### 19.1 Venue Setup (Organizer)
- Create venue with address, capacity
- Add sectors (sections of venue)
- Add seats within sectors (with position, accessibility flags)
- Bulk seat creation

### 19.2 Assigned Seating (Attendee)
- Tier with seat_assignment_mode=USER_CHOICE:
  - User sees interactive seat map
  - Available/occupied seats indicated
  - Select seat → included in ticket
- Tier with seat_assignment_mode=RANDOM:
  - System auto-assigns seat on purchase
- Sector capacity enforced as hard limit

### 19.3 Seat Attributes
- Accessibility flag (is_accessible)
- Obstructed view flag (is_obstructed_view)
- Active/inactive toggle
- Row/number for labeling

---

## Journey 20: Discount Codes

### 20.1 Create Discount Code (Organizer)
- Navigate to `/org/[slug]/admin/discount-codes/new`
- Configure: code string, discount type (percentage/fixed), value
- Scope: events, series, specific tiers
- Limits: max uses total, max uses per user, min purchase amount
- Validity: start date, end date
- Activate/deactivate

### 20.2 Apply Discount Code (Attendee)
- During ticket checkout
- Enter code → validated and applied
- See discounted price before confirming

### 20.3 Track Usage (Organizer)
- View usage count per code
- See which users used which codes

---

## Journey 21: Referral Program

### 21.1 Referral Code
- Every authenticated user has a unique referral code (auto-generated, uppercase, immutable)
- View referral code on `/account/profile` (included in `/me` API response)
- Share code with potential new users

### 21.2 Refer a New User
- New user enters referral code during registration
- Code validated (must be active, cannot self-refer)
- `Referral` record created linking referrer → referred user
- Revenue share percentage snapshotted from platform settings at creation time

### 21.3 Earn Referral Payouts
- When the referred user purchases tickets, the referrer earns a share of the net platform fees
- Monthly payout calculation task (1st of month) aggregates net platform fees per referral for the previous month
- `ReferralPayout` created with status CALCULATED
- If payout amount is below the minimum threshold, it rolls over to the next month

### 21.4 Set Up Billing Profile
- Navigate to billing profile settings
- Fill: billing name, billing address, billing email
- Optionally set VAT ID (VIES-validated for EU)
- Agree to self-billing terms (required for payout processing)
- Connect Stripe account (required for receiving transfers)

### 21.5 Receive Payout
- Monthly payout processing task (1st of month, 06:30 UTC) runs after calculation
- Pre-flight checks: Stripe enabled, billing profile complete, self-billing agreed
- For B2B referrers (validated VAT ID): self-billing invoice (Gutschrift) generated with full VAT math
- For B2C referrers (no VAT ID): payout statement generated (no VAT)
- Stripe transfer executed → payout status: PAID
- Statement PDF emailed to referrer
- If Stripe transfer fails: status set to FAILED

### 21.6 View Payout History
- Navigate to `/account/referral/payouts`
- See all payouts: period, amount, status (CALCULATED, PAID, FAILED, ROLLED_OVER)
- Download statement PDFs for issued payouts

---

## Journey 22: Attendee Invoicing

### 22.1 Enable Invoicing (Organizer)
- Navigate to org billing settings
- Set invoicing mode:
  - **NONE** (default): No attendee invoices generated
  - **HYBRID**: Invoices created as DRAFT, org admin reviews and issues manually
  - **AUTO**: Invoices created as ISSUED and emailed immediately
- Prerequisites: EU-based org with VIES-validated VAT ID, billing name, and billing address

### 22.2 VAT Preview (Buyer)
- During ticket checkout, buyer can enter billing info (name, address, country, VAT ID)
- `POST /events/{event_id}/tickets/vat-preview` returns price breakdown
- VAT treatment depends on buyer info:
  - Domestic (same country): org's VAT rate applied
  - EU cross-border B2B (valid VAT ID): reverse charge (0% VAT, net price only)
  - EU cross-border B2C (no VAT ID): org's VAT rate applied
  - Non-EU: no VAT (export of services)
- VIES validation results cached in Redis (30-minute TTL)

### 22.3 Invoice Generation
- Triggered by `checkout.session.completed` Stripe webhook
- Invoice created with seller snapshot (org details) and buyer snapshot (from billing info)
- Per-org sequential numbering: `{ORG_SLUG}-{YEAR}-{SEQ:06d}`
- Line items derived from Payment records (ticket details, quantities, VAT breakdown)
- PDF rendered via WeasyPrint
- For AUTO mode: email sent immediately with PDF attachment

### 22.4 Manage Draft Invoices (Organizer — HYBRID Mode)
- Navigate to `/org/[slug]/admin/attendee-invoices`
- View draft invoices awaiting review
- Edit buyer-facing fields (buyer name, address, VAT ID) — seller info is immutable
- Issue draft: sets status to ISSUED, regenerates PDF, sends email to buyer
- Delete draft: removes invoice and PDF entirely

### 22.5 View Invoices (Buyer)
- Navigate to `/dashboard/invoices`
- See all issued invoices (paginated)
- Download invoice PDFs via signed URLs

### 22.6 Credit Notes (Refund)
- Generated automatically on `charge.refunded` Stripe webhook
- If invoice is DRAFT: draft is deleted
- If invoice is ISSUED: credit note created with refunded amounts
- If total credits >= invoice total: invoice marked as CANCELLED
- Credit note PDF generated and emailed

---

## Cross-Cutting Concerns

### Internationalization
- UI available in: English, German, Italian
- User sets language preference
- Emails sent in user's preferred language
- Legal content localized

### Accessibility
- WCAG 2.1 AA compliance required
- Keyboard navigation
- Screen reader support
- Color contrast ratios enforced

### Mobile-First Design
- Responsive layouts
- Touch-friendly interactive elements
- Mobile-optimized ticket display and check-in

### Error States & Edge Cases
- Network errors → appropriate retry/error messages
- Sold-out tiers → clear messaging, waitlist option
- Expired/used-up tokens → **410 Gone** with distinct messages ("expired" vs "used up"), enabling contextual re-request UI
- Failed payments → ticket remains PENDING, retry option
- Concurrent purchases → atomic locking prevents overselling
- Rate limiting → appropriate error messages for throttled requests

### Global Banning (Platform Admin)
- Platform administrators can ban by email, domain, or Telegram username
- Email ban: blocks registration and deactivates existing account
- Domain ban: blocks all emails from that domain, deactivates matching accounts async via Celery
- Telegram ban: blocks Telegram-linked accounts
- Auto-linking: new registrations and Telegram connections checked against global ban list
- Banned users receive `ACCOUNT_BANNED` notification before deactivation

### Feature Flags
- `FEATURE_LLM_EVALUATION`: Gates LLM-based questionnaire evaluation (when disabled, LLM evaluation is skipped)
- `FEATURE_GOOGLE_SSO`: Gates Google SSO login (when disabled, endpoint returns 403)
- Configured via environment variables

### UNLISTED Visibility
- Organizations and events can be set to UNLISTED
- Accessible via direct link (treated as publicly accessible)
- Hidden from browse/search/discovery listings for non-owners
- Useful for soft launches, invite-link-only events, or events not ready for public discovery

### Demo Mode
- Login page has demo account selector
- Pre-seeded data: 13 users, 2 orgs, 13+ events
- Allows exploring platform without real account

---

## Gap-Fill Interview Questions

The following questions represent gaps in my understanding that I could not resolve from code alone. Answering these will complete the user journey map and produce better test cases.

### Product Vision & Positioning

1. **Elevator pitch**: How would you describe Revel in one sentence to someone who's never heard of it? What problem does it solve that Eventbrite/Luma/Meetup don't?

2. **Target communities**: The marketing pages mention queer events, kink events, and community-first organizing. Are these the primary audiences? Are there others?

3. **Revenue model**: Is Revel monetized via platform fees on ticket sales? Are there plans for subscription tiers for organizations?

### Event Type Matrix

4. **What are the canonical event configurations you want tested?** For example:
   - Public free RSVP event (simplest case)
   - Public ticketed event with Stripe payments
   - Members-only RSVP event
   - Private event with invitation-only access
   - Event with mandatory questionnaire (automatic evaluation)
   - Event with questionnaire (manual review)
   - PWYC ticketed event
   - Event with potluck coordination
   - Event with assigned seating
   - Recurring event in a series
   - Event with waitlist
   - Event with guest checkout enabled
   - Any other combos I should be testing?

5. **What combinations are impossible/invalid?** For example, can a PRIVATE event also have can_attend_without_login=True? Can a free tier have ONLINE payment?

### User Experience Details

6. **What happens on the event page when a user is NOT eligible?** Does the UI show a specific wizard/flow guiding them through remaining steps (e.g., "Step 1: Complete questionnaire → Step 2: RSVP"), or just a single blocking message?

7. **Invitation flow from the attendee's perspective**: When a user receives a direct invitation email, what does the link point to? The event page directly? A special claim page? What do they see?

8. **RSVP change behavior**: After RSVP YES, when the user changes to NO, is there a confirmation dialog? Does it mention potluck items being released?

9. **Ticket cancellation**: Can users cancel their own tickets? Under what conditions? Is there a refund flow for Stripe payments?

10. **Guest checkout UX**: For `can_attend_without_login=True` events, what does the guest see? A simplified form? Are they encouraged to create an account afterward?

### Organization Admin UX

11. **Dashboard overview**: What metrics/stats does the org admin dashboard show? Revenue? Attendee counts? Recent activity?

12. **Check-in interface**: Is there a dedicated check-in page/mode? QR scanner? Search by name? What does the staff member see after scanning?

13. **Questionnaire builder UX**: Is it a drag-and-drop interface? Can you preview the questionnaire as an attendee would see it?

14. **Announcement drafting**: Can announcements include rich text/Markdown? Are there templates? Can you schedule sends?

### Moderation & Safety

15. **Blacklist notification**: When someone is blacklisted, do they receive a notification? Or do they only discover it when trying to access an event?

16. **Whitelist request visibility**: Can the user see the specific blacklist entry that triggered their block? Or just "you may be on a restricted list"?

17. **Moderation audit trail**: Is there a log of who blacklisted whom and when? Who approved/rejected whitelist requests?

### Notifications & Communication

18. **Which notification types are most critical to test E2E?** i.e., which ones drive user action (invitation emails, ticket confirmations, questionnaire results)?

19. **Telegram bot**: What commands/conversations does the bot support? Is it just notification delivery, or can users interact (RSVP via Telegram, etc.)?

20. **Digest email**: What does a digest email look like? Is it just a list of notification titles, or full content?

### Technical / Testing

21. **Demo mode**: Should E2E tests use the seeded demo data, or create their own test data via API calls? Is there a test seed script?

22. **Stripe testing**: For payment E2E tests, should we use Stripe test mode? Are there test card numbers configured?

23. **Email testing**: Is there a Mailhog/Mailpit instance in dev for intercepting emails? How should E2E tests verify email content?

24. **File uploads**: For questionnaire file uploads, should E2E tests actually upload files? What about ClamAV scanning in test environments?

25. **Rate limiting**: Should E2E tests account for rate limiting, or is it disabled in test environments?

26. **Feature flags**: Which features are behind flags that might affect test scenarios? (Google SSO is one — any others?)

### Prioritization

27. **If you could only write 10 E2E test suites, which user journeys would they cover?** Rank by business criticality.

28. **Are there any known broken flows or flaky areas that should be tested first?**

29. **What existing Playwright tests exist on the frontend?** Are there page objects already defined?

30. **Do you want tests for all three languages, or just English?**

---

## Next Steps

1. Answer the gap-fill questions above
2. Update this document with answers
3. Derive concrete Playwright test suites from each journey
4. Prioritize test implementation order based on criticality ranking
