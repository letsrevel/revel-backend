# Revel User Journeys V2

This document maps every user journey through the Revel platform, organized by persona. Its purpose is to serve as the source of truth for Playwright E2E test cases on the frontend. Each journey describes the **what** and **why** from the user's perspective — the exact UI steps and assertions will live in the test suite.

> **Last updated**: 2026-07-19 (seating phase-4: paint, box office, guest accessible)

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
- [Journey 18: Event Series, Recurring Events & Following](#journey-18-event-series-recurring-events--following)
- [Journey 19: Venue & Seating](#journey-19-venue--seating)
- [Journey 20: Discount Codes](#journey-20-discount-codes)
- [Journey 21: Referral Program](#journey-21-referral-program)
- [Journey 22: Attendee Invoicing](#journey-22-attendee-invoicing)
- [Journey 23: Membership Subscriptions](#journey-23-membership-subscriptions)
- [Journey 24: Polls](#journey-24-polls)
- [Journey 25: Revenue & VAT Reporting](#journey-25-revenue--vat-reporting)
- [Journey 26: Series Passes (Season Tickets)](#journey-26-series-passes-season-tickets)
- [Cross-Cutting Concerns](#cross-cutting-concerns)
- [Gap-Fill Interview Questions](#gap-fill-interview-questions)

---

## Platform Overview

Revel is a privacy-focused, community-first event management and ticketing platform. It serves organizers who run community events — from queer meetups and kink parties to tech conferences and potluck dinners.

**Core differentiators** (inferred — to be validated):
- Privacy-first: no tracking, minimal data collection, GDPR-native
- Self-hostable: organizations own their data and infrastructure; single-org instances run without ClamAV, Telegram, or the full geo dataset, tailored via feature flags
- Questionnaire-based access control: events can screen attendees via questionnaires
- Flexible attendance models: free RSVP, ticketed (fixed, PWYC, offline, at-the-door), invitation-only, members only
- Recurring events: first-class recurring series with rolling-window materialization (each occurrence is a real, independent event)
- Community features: potluck coordination, dietary tracking, membership tiers, recurring membership subscriptions, organization-managed polls
- Blacklist/whitelist system with fuzzy name matching for safer spaces
- Organizer financial tooling: VAT-precise revenue reporting, attendee invoicing, referral payouts
- Multi-channel notifications: in-app, email, Telegram (immediate transactional emails + grouped digests)
- Internationalized: English, German, Italian, French

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
| **Waitlisted User** | Joined waitlist for a full event; may hold a time-limited waitlist offer | Fully authenticated |
| **Referrer** | Has a referral code and earns payouts from referred users' ticket purchases | Fully authenticated |
| **Subscriber** | Member with an active (currently OFFLINE) recurring membership subscription tied to a plan/tier | Fully authenticated |

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
- Localized variants exist for de/it/fr

### 1.8 Contact an Organization
- Org has `contact_method` set to `email` or `form` (with a verified contact email)
- `none` → no contact UI shown; `email` → org's verified address is exposed on the public org schema
- Submit a message via the public contact form (`POST /organizations/{slug}/contact`)
  - Requires a verified-email account; throttled
  - Delivered as a single transactional email to the org's mailbox (`Reply-To: <sender>`)
  - Org staff with `edit_organization` also receive an in-platform `ORG_CONTACT_MESSAGE_RECEIVED` notification

### 1.9 Feature-Flag-Aware UI
- `GET /version` (anonymous) returns a `features` object: `organization_creation`, `telegram`, `google_sso`, `llm_evaluation`
- Frontend hides gated UI (e.g. Google SSO button, "Create organization" CTA, Telegram linking) instead of letting users hit a 403/404

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
- Filter events by: owner, staff, member, RSVP, ticketed, invited, subscribed, **bookmarked**
- Bookmarked **unlisted** events surface in the `bookmarked` facet even though they stay hidden from discovery
- Dashboard RSVP list (`GET /api/dashboard/rsvps`) accepts **multiple statuses** at once (`?status=yes&status=maybe` → union) — lets the UI count Going + Maybe while excluding declined; no `status` param returns all (upcoming only by default)

### 3.2 Profile Management
- Navigate to `/account/profile`
- Edit: first name, last name, preferred name, pronouns
- Edit: language preference (en/de/it/fr)
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

### 3.11 Bookmark Events
- Click the bookmark icon on an event (list or detail)
- `POST /api/events/{event_id}/bookmark` → `201` for a new bookmark, `200` if it already existed
- `is_bookmarked` reflected on event list/detail responses
- Unbookmark (`DELETE`) is idempotent and works even for events you can no longer see
- Bookmarks are private — they do not sign you up or affect eligibility
- Find bookmarked events via the `bookmarked` facet on `/dashboard/events`

### 3.12 View Memberships & Subscriptions
- `GET /me/memberships` — unified, paginated list of every org membership for the user, inlining the most recent non-terminal subscription per org when one exists
- See [Journey 23: Membership Subscriptions](#journey-23-membership-subscriptions) for the subscriber flow

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
- A tier can be backed by a recurring **membership subscription** (currently OFFLINE) — see [Journey 23](#journey-23-membership-subscriptions)

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

`EventUserEligibility` carries a stable, machine-readable `reason_code` (`ReasonCode` enum) alongside the human-readable `reason` string — clients should branch on `reason_code`, not on localized prose.

### 5.3 RSVP
- Click YES → RSVP confirmed, attendee_count incremented
- Click MAYBE → recorded but doesn't count toward capacity
- Click NO → recorded, frees spot if was YES
- Already RSVP'd YES? Can always change to MAYBE/NO (even if eligibility changed — prevents trapping)
- When the event has `accept_rsvp_notes` enabled, the RSVP dialog offers a clearly-marked optional note (max 500 chars); each new RSVP submission overrides the stored note (submitting without a note clears it); sending a note when the event doesn't accept notes → 400. The current note is returned by `GET /events/{id}/my-status` (`rsvp.note`) so the form can prefill.

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

### 5.7 Receive a Waitlist Offer (Advanced Waitlist)
When the event opts in via `waitlist_time_window`:
- Capacity opening triggers a **batch** of offers (FIFO or lottery, per `waitlist_lottery_mode`), sized by `waitlist_batch_size`
- The offered spot is **reserved** for the duration of the window — non-waitlist users can't take it
- User receives an offer notification with an expiry deadline (`WaitlistOffer`)
- Accept before expiry → proceed with RSVP/ticket flow
- Let it expire → spot released to the next batch; the user stays eligible for future batches
- `waitlist_cutoff_date` / `waitlist_cutoff_window` stop new offers close to the event

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
Depends on the tier's `seat_assignment_mode`:
- **NONE**: No seat assigned — general admission (optionally capped by a standing sector's hard capacity)
- **BEST_AVAILABLE**: Server assigns the best adjacent block from the tier's price category on purchase
- **USER_CHOICE**: Buyer picks seats from the interactive seat map, backed by 10-minute TTL holds

See [Journey 19: Venue & Seating](#journey-19-venue--seating) for the full chart / availability / hold / best-available flow. (The old `RANDOM` mode has been removed.)

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
- The check-in code is a string: a plain ticket UUID **or** a `series:<uuid>` series-pass QR, resolved to that event's pass ticket (see [Journey 26](#journey-26-series-passes-season-tickets))
- Ticket status: ACTIVE → CHECKED_IN
- For PWYC offline tickets: price_paid recorded at check-in
- The scan / search response (`CheckInResponseSchema`) carries the resolved `seat` (label, row, number, accessibility flags) and `sector_name`, so door staff can direct the attendee (e.g. "Stalls, Row C seat 12")
- Check-in window enforced (check_in_starts_at to check_in_ends_at)

### 6.11 Apply Discount Code
- Enter discount code during checkout
- Code validated: active, within dates, not exceeded max_uses, applicable to tier/event
- Discount applied: percentage or fixed amount off

### 6.12 Cancel Ticket & Refund (Self-Service)
Opt-in per tier via `allow_user_cancellation`, `cancellation_deadline_hours`, and `refund_policy`:
- Buyers see the cancellation terms **before purchase** — `TicketTierSchema` exposes `allow_user_cancellation`, `cancellation_deadline_hours`, and `refund_policy` (tiered % by hours-before-event + flat fee)
- The active policy is snapshotted onto the ticket at purchase time (`refund_policy_snapshot`), so later policy changes never retroactively affect issued tickets
- **Preview**: `GET /events/tickets/{id}/cancellation-preview` returns the policy windows + a live refund quote for this ticket
- **Cancel**: `POST /events/tickets/{id}/cancel` works for free, offline, at-the-door, and online (Stripe) tickets
  - Online tickets trigger a Stripe refund (per-`Payment`, partial-amount aware)
  - Blocked cases return `409 CancellationBlockedErrorSchema` with a stable `code` (already cancelled, checked-in, event started, past deadline, part of a series pass, etc.)
- Cancelling an already-cancelled ticket returns **409**
- Receive a `TICKET_CANCELLED` / `TICKET_REFUNDED` notification (immediate transactional email); copy branches on `cancellation_source` (user-initiated / organizer / stripe_dashboard)
- Refund notifications report the actual `refund_amount` (partial-refund accurate), not the full ticket price

---

## Journey 7: Guest Attendee (No Account)

**Precondition**: Event has `can_attend_without_login=True`.

### 7.1 Guest RSVP
- View public event
- Click "RSVP as Guest"
- Provide: name, email
- RSVP recorded without account creation
- Guest RevelUser created (guest=True)
- Accepts the same optional note as a registered RSVP; it is applied when the RSVP is confirmed via the email link (dropped silently if the organizer disabled notes in the meantime)

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

### 7.4 Guest Seated Checkout
- A guest can hold seats **before** identifying themselves: the first anonymous hold mints a server-signed `revel_guest_hold` cookie (httpOnly, `SameSite=Lax`, 24h) — guest hold identities are never client-minted
- The guest picks seats (USER_CHOICE) or requests a best-available block, exactly as a logged-in user does (see [Journey 19](#journey-19-venue--seating))
- On "Buy as Guest", the held seats are **carried into checkout**: the guest-session cookie is the hold identity, so the buyer's own live holds are consumed rather than treated as foreign conflicts
- A best-available guest checkout may pass `accessible_required`; that flag and the guest-hold session are bound into the signed email-confirm token, so accessible-pool assignment happens at confirm time even on another device (see [Journey 19.6](#196-guest-best-available--holds))
- Everything else follows the guest ticket checkout flow (see [Journey 7.2](#72-guest-ticket-checkout))

---

## Journey 8: Organization Owner

### 8.1 Create Organization
- Navigate to `/create-org`
- Fill required fields (name, contact email, city)
- Auto-become owner with all permissions
- Contact email auto-verified if matches user's verified email
- Name is rejected if it slugifies to a **reserved slug token** — a hardcoded platform set (`admin`, `api`, `www`, `revel`, …) plus a DB-managed `ReservedSlugToken` soft list (`demo`, `test`, …); message localized in en/de/it
- On single-org / self-hosted instances, `FEATURE_ORGANIZATION_CREATION=off` returns `403` for non-staff (staff/superusers bypass)

### 8.2 Organization Settings
- Navigate to `/org/[slug]/admin/settings`
- Edit: name, description, contact email, address, social links
- Upload: logo, cover art
- Set visibility: PUBLIC, UNLISTED, PRIVATE, MEMBERS_ONLY
- Toggle: accept_membership_requests
- Set `contact_method`: `none` / `email` / `form` (both `email` and `form` require a verified `contact_email`)
  - Changing `contact_email` to a new (unverified) address auto-forces `contact_method=none`
- Set `revenue_report_cadence`: `NONE` / `QUARTERLY` / `MONTHLY` (**owner-only** — non-owners get a `403`; see [Journey 25](#journey-25-revenue--vat-reporting))
- Set membership-subscription policy: `membership_grace_period_days` (default 7), `membership_refund_policy`

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
- Access live financials and downloadable revenue & VAT reports (see [Journey 25](#journey-25-revenue--vat-reporting))

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
  - manage_polls
  - manage_subscriptions
  - send_announcements
- Set per-event permission overrides
- Remove staff
- Sales-specific permissions are also configurable from the admin panel

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
- Create sectors within venues: name, **kind** (`seated` / `standing`), capacity (hard limit for standing/GA), shape (polygon for visual map)
- Create seats within sectors: label, row/number, position, accessibility flags
- Bulk create / update / delete seats (the seat grid editor)
- Define venue-scoped **price categories** (name + hex color) painted onto seats
- Link venues, sectors, and price categories to events and ticket tiers
- Full enterprise seating setup + per-mode tier configuration is detailed in [Journey 19: Venue & Seating](#journey-19-venue--seating)

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
  - Waitlist open: yes/no (+ advanced waitlist window/batch/lottery/cutoff config — see [Journey 5.7](#57-receive-a-waitlist-offer-advanced-waitlist))
  - Potluck open: yes/no
  - Open-ended: `is_open_ended` — mark an event with no fixed end. `end` is still set (defaults to start + 24h) and used internally for ICS/Wallet/upcoming gates; the flag is a display signal (render "Ongoing", hide the end time)
  - Address visibility rules
  - Check-in window (starts_at, ends_at)
  - Invitation message (default message for invitations)
- Link to: venue, event series
- Upload: logo, cover art
- Add tags
- Authoring also exposes: resolved IANA `timezone` (UTC fallback) on list/detail; event `schedule` / timeline (see [10.14](#1014-event-schedule--timeline))

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
- **Cancellation reason** (optional, ≤1000 chars): accepted on `POST /actions/update-status/{status}` only when transitioning to `cancelled`; auto-cleared on un-cancel. Included in the `EVENT_CANCELLED` notification and returned on event list/detail — but readable only by **attendees** (ticket holders, confirmed YES RSVPs, staff/owners); merely-invited or anonymous users receive `null`.

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
  - Linked venue / sector / price category
  - Seat assignment mode: NONE, USER_CHOICE, BEST_AVAILABLE — validated per mode: USER_CHOICE requires a sector, BEST_AVAILABLE requires a price category, NONE requires neither (see [Journey 19.3](#193-tier-seating-configuration-organizer))
  - VAT rate
- Reorder tiers (display_order)

### 10.5 Manage Tickets
- View all tickets with status, search, filters; list is **sortable**
- Each ticket row shows the **discount code** applied (if any)
- Create tickets manually (on behalf of users)
- Update ticket status
- Bulk update tickets
- Check in tickets (mark CHECKED_IN)
- Cancel / mark-refunded tickets — admin actions record `cancelled_at`, `cancelled_by`, `cancellation_source=ORGANIZER`, optional reason, and (on refund) `refund_amount` / `refund_status` / `refunded_at`
- View payment details
- **Box-office seat control** (with `manage_tickets`): bulk-hold/kill individual seats with a reason or release them; **door-sell / comp** a ticket straight onto a seat; and **reseat** a ticket within its price category — all for this event, see [Journey 19.7](#197-box-office-seat-control-event-admin)

### 10.6 Manage RSVPs
- View all RSVPs with status
- Create RSVP on behalf of user
- Update RSVP status
- Search and filter (status filter accepts multiple values — `?status=yes&status=maybe`)
- Staff see attendee notes in the RSVP list/detail (`note` on `RSVPDetailSchema`) and receive them in RSVP confirmation/update notifications; note changes alone trigger an update notification
- Admins can record a note on behalf of a user regardless of the event's `accept_rsvp_notes` setting

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
- **Advanced waitlist offers** (when `waitlist_time_window` configured): batched, time-limited offers (FIFO or lottery), reserved spots during the window
  - Admin offer-management endpoints: manually create / reactivate / revoke offers; `WaitlistOfferSchema.user` is a nested `MinimalRevelUserSchema` (no follow-up lookup)
  - Manual create / reactivate accept a payload `expires_at` even when no global window is set; they only 400 when neither event nor payload defines an expiry
  - Revoked offers are excluded from auto-reoffering (soft-skip until reactivated); expired users stay eligible for future batches

### 10.11 Announcements
- Create draft announcement
- Target: event attendees, all members, specific tiers, staff only
- Preview recipient count before sending
- Send → notification dispatched to all targets
- Past visibility flag: controls if announcement visible after event
- **Schedule** a send for a future absolute time, or relative to event start/end (auto-shifts when the event moves) — adds a `SCHEDULED` status with `/schedule` and `/unschedule` endpoints; delivered by a background sweep
- **Resend to new sign-ups**: re-deliver an event announcement to attendees who join after the first send, until the event ends, without double-notifying
- Public announcement schema carries an `audience` descriptor

### 10.12 Event Resources
- Attach files, links, or text to event
- Set per-resource visibility

### 10.13 View Event Statistics
- Attendee count
- Dietary summary (aggregated restrictions/preferences)
- Pronoun distribution (visible to non-staff only when `public_pronoun_distribution=True`)
- Ticket sales by tier

### 10.14 Event Schedule / Timeline
- Author an ordered list of display-only sessions via `PUT /event-admin/{event_id}/schedule`
- Each session: title, markdown description, relative start offset, optional duration/location, `is_required` badge
- Exposed on the public event detail; copied verbatim on event duplication and recurring-series materialization

### 10.15 Event Revenue (per event)
- `GET /event-admin/{event_id}/tickets/revenue` returns `EventFinancialsSchema`: `sold_count` (incl. later refunded/cancelled), `refunds`, `refunded_count`, and VAT detail (`net_taxable`, `vat`, `rate_buckets`)
- Tracks both online (Stripe) and offline/at-the-door ticket refunds
- Org-wide financials live in [Journey 25](#journey-25-revenue--vat-reporting)

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
- Upload files if required (ClamAV scanned, unless `FEATURE_MALWARE_SCAN` is off)
- Question/section shuffling is **stable per viewer** across reloads (varies between users), not re-randomized on every load
- Submit
- Single-answer questions (`allow_multiple_answers=False`) reject submissions with more than one selected option — enforced at the service layer, closing an admission-gate bypass that `bulk_create` previously skipped

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
- Submissions expose `requires_evaluation` so the UI can distinguish auto-scored from review-pending

### 11.8 Duplicate Questionnaire
- `POST /api/questionnaires/{org_questionnaire_id}/duplicate` deep-copies sections, questions, options, and intra-questionnaire dependencies into a new **DRAFT** in the same org (requires `create_questionnaire`)
- Pass `copy_associations: true` to also link the copy to the template's events/series (unattached by default)
- Votes, submissions, answers, evaluations, and uploaded files are never copied

### 11.9 Access Control (Answer Key)
- `GET /api/questionnaires/{id}` and the list endpoint require **org admin** permission — the full answer key (`is_correct`, weights, `min_score`, `reviewer_notes`, `llm_guidelines`) is no longer readable by arbitrary authenticated users

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

### 13.6 Unban Behavior
- Removing a blacklist entry clears the stale `BANNED` `OrganizationMember` row the ban created
- Membership transitions to `CANCELLED` (not `ACTIVE` — avoids implicitly re-granting prior privileges); the org and its events become visible again
- If another blacklist entry still covers the user in the org, `BANNED` is preserved
- Hard-blacklisted users can no longer reclaim a staff-granting org invitation token to regain access

### 13.7 Content Moderation (Platform / Staff)
- Offensive **food-item names** are blocked at creation across all languages (new `moderation` app)
- Staff can delete offensive food items via the admin
- Quarantined (malware-flagged) uploads are stored behind the protected, signed-URL media path — never publicly downloadable; staff retrieve them via a signed admin link

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
  - **Transactional emails always deliver immediately**, bypassing the digest: `PAYMENT_CONFIRMATION`, `TICKET_CREATED`, `TICKET_CANCELLED`, `TICKET_REFUNDED`
  - **Digest** is grouped by human-readable type label; each item carries a body, timestamp, and a "View details" link, styled to match the transactional emails
- **Telegram**: Via connected Telegram account (unavailable when `FEATURE_TELEGRAM` is off — linking endpoints 404)
  - Globally-banned users are blocked from interacting with the bot; the bot's `/unsubscribe` command actually stops notifications (was a no-op on per-type settings)
- All event times in notifications/emails render in the **event's own timezone**
- **Let's Revel branding**: every outbound email (including previously plain-text ones: guest RSVP/ticket confirmations, invoice/credit-note/payout/revenue-report delivery) and generated PDF carries the Let's Revel brand; sender display name is `Let's Revel <revel@letsrevel.io>`. Tickets keep the organizer's own colors, adding only a "Powered by let's revel." footer

### 15.2 Notification Types (non-exhaustive)
- Account: registration welcome, email verified, email change in-flight/completed, password reset
- Events: created, opened, closed, cancelled (with reason for attendees), reminder, series events generated
- Tickets: created, purchased, cancelled, refunded (note: on-check-in `TICKET_CHECKED_IN` is **no longer sent** — the holder is already there)
- Invitations: sent, accepted, pending conversion
- Memberships: granted, request created/approved/rejected
- Subscriptions: payment recorded, lifecycle changes (cancel/pause/resume), expiry/past-due
- Questionnaires: submitted, evaluation complete
- Polls: (organization-managed; see [Journey 24](#journey-24-polls))
- RSVPs: confirmed
- Potluck: item claimed/unclaimed
- Whitelist: request created/approved/rejected
- Announcements: new announcement (immediate, scheduled, or resent to new sign-ups)
- Organization: contact message received (`ORG_CONTACT_MESSAGE_RECEIVED` — Telegram body omits subject/preview since chats aren't E2E-encrypted)

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

### 16.7 Revenue & VAT Reporting
See [Journey 25: Revenue & VAT Reporting](#journey-25-revenue--vat-reporting) for live financials, downloadable reports, and scheduled delivery.

### 16.8 Stripe Webhook Reliability (Behind the Scenes)
- Multiple rotating signing secrets (`STRIPE_WEBHOOK_SECRETS`); invalid signatures fail closed with `403`
- DB-level idempotency (`StripeWebhookEvent` log) so redeliveries replay task dispatches instead of being dropped or double-processed
- Stripe API version pinned for predictable behaviour; `provision_stripe_webhooks` management command for setup

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

### 17.6 Self-Served Email Change
- `POST /account/email-change-request` — requires the current password; sends a single-use confirmation link to the **new** address (proof of control)
- `POST /account/email-change-confirm` — swaps `email` + `username`, **blacklists every outstanding JWT** for the user, and returns a fresh token pair so the confirming device stays signed in
- Both addresses receive completion emails; the old address also gets an in-flight notice with a masked rendering of the new address
- Rejected with clear `400`s for Google-SSO accounts and same-email / already-taken targets; globally-banned targets silently no-op

---

## Journey 18: Event Series, Recurring Events & Following

### 18.1 View Event Series
- Browse series at `/events/[org_slug]/series/[series_slug]`
- See: series info, list of events, shared branding
- `is_recurring` exposed on series list/retrieve schemas

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

### 18.4 Recurring Events (Organizer)
First-class recurring series with rolling-window materialization:
- Define a `RecurrenceRule` (frequency, interval, weekdays, monthly params, boundaries, IANA `timezone`) → emits an RFC 5545 `RRULE`; occurrences preserve their wall-clock time in the rule's `timezone` across DST transitions
- Series config: `template_event`, `recurrence_rule`, `exdates`, `auto_publish`, `generation_window_weeks`
- Each occurrence is a **real materialized `Event`** (independent tickets, payments, capacity) duplicated from the template; safe template edits propagate via a `PROPAGATABLE_FIELDS` whitelist
- A daily Celery beat task drives rolling-window generation
- 7 admin endpoints: create recurring series, edit template with propagation, update recurrence, cancel occurrence, manual generate, pause/resume
- `GET /organization-admin/{slug}/event-series/{series_id}` returns the full `EventSeriesRecurrenceDetailSchema`
- `GET .../template-event` returns the template's `EventDetailSchema` (the public detail route filters templates out by design)

### 18.5 Cadence-Drift Detection
- `GET /organization-admin/{slug}/event-series/{series_id}/drift` returns the IDs of future occurrences that no longer match the current `RRULE` after a cadence change (excludes manually-modified and cancelled occurrences)

### 18.6 Series Notifications
- A single digest `SERIES_EVENTS_GENERATED` notification per generated batch (not one per occurrence)
- Series-followed (staff) and series-events-generated (follower) notifications link to the public `/events/[org_slug]/series/[series_slug]` route

---

## Journey 19: Venue & Seating

> **Enterprise seating.** Reusable venue layouts (sectors of kind seated/standing, seats, painted price categories) drive three per-tier assignment modes (GA, user-choice, best-available), TTL cart holds, guest (incl. accessible) seating, and per-event box-office control (overrides, door sales/comps, reseat). Seat row/adjacency order is derived server-side; the seating chart is served render-ready straight from live tables (no versioning in v1).

### 19.1 Venue & Layout Setup (Organizer)
- Navigate to `/org/[slug]/admin/venues`; requires `edit_organization` (list/read also allowed for org staff)
- **Venue**: name, address, city, capacity, description (GA-only venues are valid — no seats required)
- **Sectors** — a sector has a `kind`:
  - `seated`: holds individual seats; capacity is implied by its seat count
  - `standing`: no seats; a hard `capacity` acts as the ceiling for GA (`NONE`-mode) tiers linked to it
  - Optional `shape` polygon + `display_order` for the visual map
  - A sector's `kind` can only be changed while it has **zero seats** (delete its seats first); otherwise **400**
- **Seats** (in a seated sector) via the grid editor — bulk create / bulk-update / bulk-delete (by label) plus single seat update/delete:
  - Attributes: `label`, `row_label` / `row_order`, `number`, `adjacency_index` (left→right order within a row), `position`, `is_accessible`, `is_obstructed_view`, `is_active`, `price_category_id` (paint)
  - **Row/adjacency order is derived server-side**: leave `row_order` / `adjacency_index` unset and the server re-ranks the whole sector — rows front-to-back from `row_label` in *natural* order (numeric `2` before `10`; theatre `Z` before `AA`), seats left-to-right within a row by `number` then `label`. Passing an explicit `row_order`/`adjacency_index` on **any** seat in the request wins wholesale (no derivation for that call). Ranks are re-derived on create / bulk-create / bulk-update whenever a `row`/`number` changes
  - When the sector has a shape, every seat `position` must fall inside the polygon
  - Seat labels are immutable (rename = delete + recreate); a seat with active/pending tickets for a **future** event cannot be deleted (past/checked-in/cancelled don't block)

### 19.2 Price Categories (Organizer)
- Venue-scoped categories: `name` + `color` (hex, for map rendering) + `display_order`
- CRUD under `/organization-admin/{slug}/venues/{venue_id}/price-categories`; a duplicate `(venue, name)` is rejected with **400**
- A category is **painted** onto seats as each seat's `default_price_category` — this is the pool a best-available tier draws from
- **Paint endpoint**: `PUT /organization-admin/{slug}/venues/{venue_id}/seats/paint` bulk-paints the given `seat_ids` in a single UPDATE and returns `{"painted": n}`; `price_category_id: null` **unpaints**. Every seat id must belong to the venue (else **404**) and the category, when given, must belong to the venue (else **400**)
- **Paint round-trip**: seat reads (`VenueSeatSchema` — e.g. the sector / seat-list endpoints) carry `price_category_id` plus a nested `price_category` (name + color), so the grid editor re-hydrates existing paint on reload
- **Delete guard**: deleting a category referenced by any ticket tier is refused with **400** (the tier FK is `SET_NULL`, so a silent delete would leave best-available tiers unsellable). Seats painted with the category are unaffected — on delete their `default_price_category` simply becomes NULL and can be repainted

### 19.3 Tier Seating Configuration (Organizer)
Set on the ticket tier (see [Journey 10.4](#104-ticket-tier-management)); the model validates each mode:
- **`NONE`** (general admission): no seat/sector/category required. Optionally link a **standing sector** — its `capacity` is then a hard limit enforced atomically at sale time (429 "This sector is full")
- **`USER_CHOICE`**: **requires a seated sector**; buyers pick specific seats from it. Pointing a user-choice tier at a **standing** sector is rejected (model validation — a standing sector has no seats to choose from)
- **`BEST_AVAILABLE`**: **requires a price category**; the server assigns an adjacent block from that category's seat pool
- Cross-checks: a tier's sector, price category, and venue must all belong to the same venue/organization, and must match the event's venue when one is set

### 19.4 Buy Seated Tickets — User Choice (Attendee)
- **Chart**: `GET /events/{event_id}/seating/chart` returns the render-ready layout (sectors, seats, price categories) for the event's venue
- **Availability**: `GET /events/{event_id}/seating/availability` returns a **sparse** map — only non-available seats appear (anything absent is available), plus standing sector counts (`capacity` / `taken`) and the caller's own holds + earliest expiry. Per-seat state precedence is `sold` > `blocked` > `held`:
  - `sold`: any non-cancelled ticket (incl. checked-in) occupies the seat
  - `blocked`: a box-office override (held/killed) **or** a decommissioned (`is_active=False`) seat
  - `held`: a live TTL hold owned by someone else
- **Holds**: `POST /events/{event_id}/seating/holds` acquires **10-minute** TTL holds on the requested seats:
  - **All-or-nothing** — if any seat is unavailable, no holds are created; the response is **409** with the conflicting `seat_ids` and a `conflict_reason` of `"unavailable"` (bad/blocked/sold/foreign-held) or `"capacity"` (the caller already holds too many — cap is the event's `max_tickets_per_user`, default 10)
  - **Auto-refresh**: re-requesting seats you already hold live refreshes their TTL, bounded by an absolute **30-minute** lifetime from first acquisition
  - `DELETE /events/{event_id}/seating/holds` releases the caller's holds (a subset when `seat_ids` given, else all)
- **Purchase** consumes the buyer's own holds: at checkout, a seat live-held by **another** identity is a 409 conflict, while the buyer's own holds on the requested seats are deleted as they convert to tickets

### 19.5 Buy Seated Tickets — Best Available (Attendee)
- `POST /events/{event_id}/seating/holds/best-available` with `tier_id`, `quantity`, and optional `accessible_required` optimistically holds the best adjacent block from the tier's price category
- **Scoring** (lower is better): front rows first (`row_order`) → centrality (closer to the row midpoint) → fragmentation penalty (avoid stranding a single leftover seat) → sector order. Only genuinely-equivalent placements are tie-broken by a seeded shuffle
- **Accessible seats are protected**: they are excluded from the general assignment pool, so an ordinary best-available request never consumes them
- **`accessible_required=true`** flips to the accessible-only pool and relaxes contiguity, taking the nearest-row accessible seats
- When no block of `quantity` fits, the hold route returns **409** in the same `HoldResponseSchema` shape with `conflict_reason` `"no_block"` (empty `held_seat_ids`/`conflicts`, not an `HttpError` detail body); it retries internally when a picked seat is taken between the unlocked pick and the lock
- The same adjacency-aware assignment runs server-side at purchase for a `BEST_AVAILABLE` tier even without a prior hold; a shortfall at purchase is a **409** detail — "Not enough adjacent seats available for this tier." (general pool) or, when `accessible_required`, the distinct "Not enough accessible seats available — please contact the organizer."

### 19.6 Guest Best-Available & Holds
- Anonymous users get the same chart / availability / hold / best-available flow; the first hold mints the signed `revel_guest_hold` cookie (see [Journey 7.4](#74-guest-seated-checkout)), and those holds carry into guest checkout
- **Guest accessible seating**: guest checkout accepts `accessible_required`; the flag **and** the guest-hold session id are bound into the signed email-confirm token, so best-available assignment at confirm time draws from the accessible pool and still consumes the buyer's own holds even when the link is opened on a different device. Too few accessible seats at confirm is the same distinct **409** ("Not enough accessible seats available — please contact the organizer.")

### 19.7 Box Office Seat Control (Event Admin)
- `PUT /event-admin/{event_id}/seating/overrides` — requires the `manage_tickets` permission
- **Set** items upsert a per-event override on a seat with a `status` of `held` (house/tech/promoter) or `killed` (not sellable this event) plus a free-text `reason`; **release** clears overrides by seat id
- **Per-seat rejection**, never whole-batch: a seat holding a non-cancelled ticket on this event is rejected as `"ticketed"`, and an id not on the venue as `"unknown_seat"`; the rest still apply. If a seat is in both set and release, the release wins
- **Effect**: an overridden seat reads as `blocked` in availability and is unpurchasable — user-choice rejects it and best-available skips it. Releasing the override restores it to the pool
- Response reports `applied` / `released` counts and the per-seat `rejected` map

**Door sales & comps** — `POST /event-admin/{event_id}/seating/sell` (also `manage_tickets`):
- Issues an **ACTIVE** ticket directly on a seat. `payment_method` is `at_the_door` or `free` only — `free` records `price_paid = 0` (a comp: no tier-price revenue), `at_the_door` leaves it null so fixed-price reporting falls back to the tier price
- **Recipient** is exactly one of `email` or `user_id`: `user_id` wins; an `email` matching **any** existing account (guest or not) reuses it — unlike self-service guest checkout, a staff door-sale onto a registered account is intended; an unknown email mints a guest user
- A box-office **HELD** override on the seat is **released** as part of the sale; a **KILLED** seat is rejected (**400**)
- Enforces tier capacity (**429** sold out / **400** "N remaining") and event capacity, but **bypasses the buyer-side gates** — `purchasable_by`, per-user caps, and sales windows — as a staff exemption. Seat conflicts are **400** (unknown / inactive / wrong-venue) or **409** (already ticketed / held by another buyer)

**Reseat** — `POST /event-admin/{event_id}/seating/reseat` (also `manage_tickets`):
- Moves a **PENDING/ACTIVE** ticket to another free seat in the **same price category** (cross-category reseat is not supported in v1). Both seat rows are locked PK-ordered before re-checking
- The target must be active, in the same venue, share the current seat's `default_price_category`, and be free of tickets / overrides / foreign holds. **400** for an invalid ticket state or target; **409** for an occupied or foreign-held target

### 19.8 Seat Attributes (Reference)
- `is_accessible` — accessible seat (protected from general best-available assignment)
- `is_obstructed_view` — obstructed-view flag (display only)
- `is_active` — decommissioned seats read as `blocked` and are never holdable/assignable
- `row_label` / `row_order` / `number` / `adjacency_index` — labeling and adjacency ordering
- `default_price_category` — the painted price category driving best-available pools

---

## Journey 20: Discount Codes

### 20.1 Create Discount Code (Organizer)
- Navigate to `/org/[slug]/admin/discount-codes/new`
- Configure: code string, discount type (percentage/fixed), value
- Scope: events, series, specific tiers
- Limits: max uses total, max uses per user, min purchase amount
- Validity: start date, end date
- Activate/deactivate
- Creating a code with a **duplicate code** returns a clear `409` (was an opaque 500)
- `currency` is validated against the supported `Currencies` whitelist on both create and update

### 20.2 Apply Discount Code (Attendee)
- During ticket checkout
- Enter code → validated and applied
- See discounted price before confirming

### 20.3 Track Usage (Organizer)
- View usage count per code
- See which users used which codes

### 20.4 Delete vs Deactivate
- Deleting a code that has **never been used** hard-deletes it
- Deleting a code that **has been used** deactivates it instead, preserving usage history

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
- A failure generating or emailing one referrer's statement never aborts the rest of the payout batch
- Statement email delivery is tracked (see [Journey 22.7](#227-delivery-reliability-behind-the-scenes) — same sweep covers payout statements)

### 21.6 View Payout History
- Navigate to `/account/referral/payouts`
- See all payouts: period, amount, status (CALCULATED, PAID, FAILED, ROLLED_OVER)
- Download statement PDFs for issued payouts

### 21.7 Manual Referral Recording (Platform Admin)
- Referral admin supports full create / edit / delete, so staff can manually record a referral win when the referred user never used the referrer's link
- `referrer` stays auto-derived from the referral code; double-referrals and referrals that already have payouts remain protected

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

### 22.7 Delivery Reliability (Behind the Scenes)
- Financial documents (attendee invoices, credit notes, payout statements) record when their email was sent; delivery errors surface in the admin
- A periodic sweep retries undelivered documents
- Documents with no possible recipient are marked **terminally undeliverable** instead of being re-swept forever

---

## Journey 23: Membership Subscriptions

> **Phase 1 — OFFLINE only.** Subscriptions are currently staff-managed (recorded payments), not self-served Stripe billing. Online/auto-renew is future work.

### 23.1 Configure Subscription Plans (Organizer)
- Requires the `manage_subscriptions` permission (owner ✓, staff ✓ by default, member ✗)
- Create plans per membership tier: `MembershipSubscriptionPlan` CRUD scoped to a tier
- Org-level policy: `membership_grace_period_days` (default 7), `membership_refund_policy`
- `GET /organization-admin/{slug}/plans` lists every plan across the org (optional `is_active` filter); `tier_name` resolved on `PlanSchema` so UIs render the plan→tier link without a join

### 23.2 Create & Manage a Subscription (Organizer)
- Staff endpoints: subscription create / list / get
- Lifecycle: cancel / pause / resume
- Record a payment (`MembershipPayment`); record-only refund
- `occurred_at` lets staff backfill OFFLINE payments with the real payment date — it anchors `period_start` / `period_end` math when set
- `GET /organization-admin/{slug}/subscriptions/{sub_id}/payments` — paginated payment history (newest first)
- A partial-unique constraint enforces at most one non-terminal subscription per `(user, org)`

### 23.3 Membership Sync (Behind the Scenes)
- A `post_save` signal syncs `OrganizationMember.status + tier` from the subscription
- It never **creates** members and never touches a `BANNED` member
- Daily Celery beat `events.expire_subscriptions_past_grace` transitions `ACTIVE → PAST_DUE → EXPIRED` once past the org's grace period

### 23.4 Member View
- `GET /me/membership-subscriptions` — the caller's subscriptions
- `GET /me/organizations/{org_id}/subscription` — the caller's subscription for one org
- `GET /me/memberships` — unified memberships list, inlining the most recent non-terminal subscription per org
- `MySubscriptionSchema` resolves `organization_name`, `organization_slug`, `organization_logo_url` so dashboards render a card without N+1 org lookups

---

## Journey 24: Polls

> Organization-managed polls, built on the questionnaire pipeline. A `Poll` is 1:1 with a `Questionnaire`; votes are stored as `QuestionnaireSubmission` rows.

### 24.1 Create & Manage a Poll (Organizer)
- Requires the `manage_polls` permission (JSONField-backed, no migration)
- `/api/polls/` endpoint group: list, detail, results, create, patch, open, close, reopen, delete
- Configure: audience/visibility (PUBLIC / UNLISTED / …), lifecycle (open/close timestamps), anonymity (`staff_anonymous`, `public_anonymous`), result visibility/timing, vote-change policy, membership-tier restrictions
- Lifecycle is row-locked to serialize close-vs-vote races; a `polls.close_polls_due` beat task auto-closes due polls

### 24.2 Duplicate a Poll
- `POST /api/polls/{poll_id}/duplicate` clones a poll and its wrapped questionnaire into a new **DRAFT** poll
- Copies visibility, result timing, anonymity, vote-change policy, and tier restrictions while resetting the lifecycle (status + open/close timestamps)
- `staff_anonymous` / `public_anonymous` — normally immutable after creation — may be overridden on the copy
- Votes, submissions, answers, evaluations, and uploaded files are never copied

### 24.3 Vote (Member / Eligible User)
- `POST` vote and `POST` withdraw on `/api/polls/...`
- Vote-change policy controls whether a cast vote can be changed
- A DRAFT poll's detail endpoint never leaks unpublished questions/options to non-staff (even with the UUID) — hidden until published

### 24.4 View Results
- `GET /api/polls/{id}/results` — visibility and timing governed by the poll's result settings and anonymity flags

---

## Journey 25: Revenue & VAT Reporting

> A VAT-precise financial-reporting suite for organizers, all driven by one tax engine so every view reconciles. Aggregates online (Stripe) and offline/at-the-door payments per currency and per VAT rate; refunds attributed to the period they occurred.

### 25.1 Live Org Financials
- `GET /organization-admin/{slug}/revenue` — per-event revenue
  - Sortable (`sort=revenue|event_start`)
  - Period-filtered (`year` + optional `month` / `quarter`)
  - Currency switching (`available_currencies`, `?currency=`)

### 25.2 Downloadable Revenue & VAT Report
- `POST /organization-admin/{slug}/revenue-report` kicks off generation; poll via `GET /organization-admin/{slug}/revenue-reports/{id}`
- Output is a **ZIP** bundling:
  - an **XLSX** (Summary + Transactions sheets)
  - a **PDF** (per-VAT-rate table, refunds, net taxable turnover)
- Cached and reused unless `?refresh=true`

### 25.3 Scheduled Delivery
- Org-level `revenue_report_cadence`: `NONE` / `QUARTERLY` / `MONTHLY` (**owner-only** to change — see [Journey 8.2](#82-organization-settings))
- Emails the just-closed period's report ZIP to the org `billing_email` and owner
- Computed in the org's timezone; empty periods are skipped

### 25.4 Per-Event Revenue
- `GET /event-admin/{event_id}/tickets/revenue` → `EventFinancialsSchema` (see [Journey 10.15](#1015-event-revenue-per-event)) — VAT detail, online + offline refunds tracked

---

## Journey 26: Series Passes (Season Tickets)

> Buy once, attend the whole series. A pass on an `EventSeries` materializes a real ticket for every covered event, priced pro-rata (`price − passed_events × pro_rata_discount`, floored at 0) and purchasable while ≥2 covered events remain. Non-recurring series only; no assigned-seating/PWYC tiers; no series/event ADMISSION questionnaires.

### 26.1 Configure a Pass (Organizer)
- `POST /event-series-admin/{series_id}/passes` — name, price, pro-rata discount, payment method (online/offline/free — no at-the-door), visibility, sales window, optional holder cap
- Each covered event maps to **one existing ticket tier** (tier link); pass holders consume that tier's capacity — one shared pool with direct sales
- Update/delete via `PATCH`/`DELETE /{pass_id}`; deleting a pass (or removing a tier link) with non-cancelled holders → 409
- Requires `edit_event_series`

### 26.2 Browse & Quote (Buyer)
- `GET /series-passes/event-series/{series_id}` — visible passes (visibility-filtered)
- `GET /series-passes/{pass_id}/quote` — current pro-rata price, passed/remaining event counts, purchasability + reason

### 26.3 Purchase
- `POST /series-passes/{pass_id}/checkout` (authenticated)
- Blacklist + members-only checks, then all-or-nothing capacity check across every covered future event (one sold-out tier → 429)
- Free → pass ACTIVE immediately; offline → PENDING until staff confirms (`POST .../held/{held_pass_id}/confirm-payment`); online → one Stripe session, price split penny-exact into per-event `Payment` rows honoring each tier's VAT rate
- One non-cancelled pass per user per product; duplicate purchase races → 409
- Single `SERIES_PASS_PURCHASED` notification (per-ticket emails suppressed)

### 26.4 Holder Experience
- `GET /series-passes/me` — my passes; per-pass PDF and Apple Wallet downloads (`/me/{held_pass_id}/pdf` · `/pkpass`), QR payload `series:<uuid>`
- Check-in scans the pass QR at any covered event — resolved to that event's materialized ticket (see [6.10](#610-check-in))
- Pass tickets appear in `/dashboard/tickets` but **cannot be self-cancelled** (`PART_OF_SERIES_PASS`)

### 26.5 Extend Coverage (Organizer)
- Events added to the series later are not auto-covered: extend via `POST .../passes/{pass_id}/tier-links` or inline `series_pass_links` on event create/update
- Celery task materializes tickets for active holders **free of charge**, idempotently; full tiers are skipped and reported
- Holders get `SERIES_PASS_EXTENDED`

### 26.6 Cancellation & Refunds
- Organizer cancels a covered event → each holder refunded their per-event payment share
- Organizer cancels a whole pass (`POST .../held/{held_pass_id}/cancel`) → refunds all remaining events, expires a live Stripe session, releases tier + pass capacity; holder + staff get `SERIES_PASS_CANCELLED` with the refunded amount
- Abandoned online checkouts expire the stranded PENDING pass and restore capacity (payment-cleanup sweeps + webhooks)

### 26.7 Organizer Visibility
- `GET .../passes/{pass_id}/holders` — holder list (searchable)
- Attendee/ticket lists filter by origin: `?source=pass|direct`; ticket rows expose their originating `series_pass`

---

## Cross-Cutting Concerns

### Internationalization
- UI available in: English, German, Italian, French
- User sets language preference (`en`/`de`/`it`/`fr`)
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
Configured via environment variables. The anonymous `GET /version` returns a `features` object so clients can hide gated UI instead of letting users hit a 403/404.
- `FEATURE_LLM_EVALUATION`: Gates LLM-based questionnaire evaluation (when disabled, LLM evaluation is skipped)
- `FEATURE_GOOGLE_SSO`: Gates Google SSO login (when disabled, endpoint returns 403)
- `FEATURE_MALWARE_SCAN` (default on): when off, uploads skip the ClamAV scan and are marked clean
- `FEATURE_TELEGRAM` (default on): when off, Telegram delivery is dropped and the linking endpoints 404
- `FEATURE_ORGANIZATION_CREATION` (default on): when off, `POST /organizations/` returns 403 for non-staff (single-org instances); staff/superusers bypass
- `FEATURE_OBSERVABILITY` (renamed from `ENABLE_OBSERVABILITY`, which still works as a deprecated alias for one release)
- `/version` exposes a subset to clients: `organization_creation`, `telegram`, `google_sso`, `llm_evaluation`

### Self-Hosting
- The backend boots and runs on a self-hosted box **without** ClamAV, Telegram, or the full geo dataset, tailored via the feature flags above
- Cities-CSV fallback to a bundled `worldcities.mini.csv` so a fresh container doesn't crash-loop on the city-load migration
- `provision_stripe_webhooks --format json` for machine-readable webhook setup; dedicated self-hosting docs section
- `bootstrap_admin` interactive first-run command: creates the initial superuser (username pinned to the email, matching public-API accounts) and the first organization, then prints admin-panel and frontend URLs
- The platform host's own Stripe account can be bound to a single organization (superuser admin action), letting the host sell tickets directly

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

9. ~~**Ticket cancellation**: Can users cancel their own tickets? Under what conditions? Is there a refund flow for Stripe payments?~~ **Resolved (v1.50.0)** — opt-in per tier (`allow_user_cancellation`, `cancellation_deadline_hours`, snapshotted `refund_policy`); self-service `cancel` + `cancellation-preview` endpoints; Stripe refunds for online tickets. See [Journey 6.12](#612-cancel-ticket--refund-self-service). Remaining UX question: is there a confirmation dialog, and does it surface the live refund quote inline?

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

21. ~~**Demo mode**: Should E2E tests use the seeded demo data, or create their own test data via API calls? Is there a test seed script?~~ **Resolved (2026-07-07)** — hybrid: tests run against a local backend in `DEMO_MODE` loaded with the deterministic `bootstrap_events` dataset (13 users / 12 events; see `src/events/management/commands/README.md`), logging in via the demo-account dropdown. Read-only journeys assert against seeded fixtures; mutating journeys either revert their own state or create their own data through the UI (as the recurring-series spec already does). `manage.py reset_events` (demo-mode-only) is the recovery hatch. Note: `make seed` is the *randomized* faker seeder — not for E2E.

22. ~~**Stripe testing**: For payment E2E tests, should we use Stripe test mode? Are there test card numbers configured?~~ **Resolved (2026-07-07)** — Stripe checkout E2E is deferred (needs test mode + webhook forwarding in the loop). Paid flows are covered via **offline / at-the-door tiers** (PENDING → staff confirms → ACTIVE) and free tiers first; online Stripe checkout becomes its own later effort.

23. **Email testing**: Is there a Mailhog/Mailpit instance in dev for intercepting emails? How should E2E tests verify email content?

24. **File uploads**: For questionnaire file uploads, should E2E tests actually upload files? What about ClamAV scanning in test environments?

25. **Rate limiting**: Should E2E tests account for rate limiting, or is it disabled in test environments?

26. ~~**Feature flags**: Which features are behind flags that might affect test scenarios? (Google SSO is one — any others?)~~ **Resolved (v1.64.0)** — `FEATURE_GOOGLE_SSO`, `FEATURE_LLM_EVALUATION`, `FEATURE_MALWARE_SCAN`, `FEATURE_TELEGRAM`, `FEATURE_ORGANIZATION_CREATION`, `FEATURE_OBSERVABILITY`. The client-visible subset is on `GET /version`. See [Cross-Cutting → Feature Flags](#feature-flags). Open question: which flag combinations should the E2E matrix actually exercise?

### New Capabilities (added since v1.47.0)

31. **Membership subscriptions**: Phase 1 is OFFLINE/staff-managed. What's the intended member-facing UX today — read-only "your subscription" cards, or any self-service actions? When does online/auto-renew billing land?

32. **Polls**: What's the primary use case — event feedback, community decisions, both? Are polls surfaced on the public org/event page, or member-dashboard only? Which anonymity defaults should E2E assert?

33. **Recurring events**: For E2E, should we test the full generation lifecycle (create rule → materialize → drift after cadence change), or treat occurrences as ordinary events once generated?

34. **Advanced waitlist offers**: What does the offer notification + accept flow look like to the user? Is there a countdown UI for the offer expiry window?

35. **Revenue & VAT reporting**: Is the downloadable ZIP report something to verify E2E (download + open), or is asserting the live `/revenue` endpoint sufficient? Any locale/currency combos that must be covered?

36. **Open-ended events**: How should the frontend render `is_open_ended` (e.g. "Ongoing" badge, hidden end time)? Anything special for ICS/Wallet expectations in tests?

### Prioritization

27. ~~**If you could only write 10 E2E test suites, which user journeys would they cover?** Rank by business criticality.~~ **Resolved (2026-07-07)** — agreed priority (happy paths first, unhappy where cheap):
    - **P0**: auth (login/logout, registration → check-email; unhappy: bad creds, weak password) · guest discovery (browse/filter, event detail, org page) · RSVP flow (YES → attendee view → change to NO, potluck auto-release) · free-tier ticket (purchase → dashboard) · organizer create-event (draft → open → publicly visible, with free tier)
    - **P1**: invitation-token claim · questionnaire fill → auto-eval → RSVP unlocked · guest RSVP · member management · announcements · dashboard facets
    - **P2**: offline payment confirm · discount codes · waitlist · self-service cancellation · unhappy paths (sold out, deadlines, ineligible)

28. **Are there any known broken flows or flaky areas that should be tested first?**

29. ~~**What existing Playwright tests exist on the frontend?** Are there page objects already defined?~~ **Resolved (2026-07-07)** — 8 specs in `revel-frontend/tests/e2e/` (~1030 lines): 2 CSP/FOUC regression tests (brand, dark-mode), and 6 journey smokes (login-navbar, polls admin + voter, recurring-series dashboard, duration-input, subscriptions) that run against a **demo-seeded local backend** via the demo-account dropdown (`alice.owner@example.com`, `charlie.member@example.com`, org `revel-events-collective`). **No shared infrastructure exists**: no page objects, no fixtures, no `storageState` auth setup — login helpers are copy-pasted per spec, and the `E2E_ADMIN_AUTH`/`E2E_MEMBER_AUTH` gates reference a fixture that was never built (those specs always skip). E2E is not wired into CI.

30. **Do you want tests for all three languages, or just English?**

---

## E2E Test Strategy (agreed 2026-07-07 — not yet implemented)

- **Where tests run**: locally first, against a dev backend (`make run`) in `DEMO_MODE` with the `bootstrap_events` dataset; frontend via Playwright's build+preview `webServer` (`PUBLIC_API_URL=http://localhost:8000`). Specs keep the existing self-skip guard when no demo backend is reachable. CI wiring is a deliberate follow-up (compose up Postgres/Redis + backend, run `bootstrap_events`, then Playwright) — nothing in the contract blocks it.
- **Browsers**: journey specs run on **Chromium + Mobile Chrome** only; the FOUC/CSP regression specs keep the full 5-project matrix.
- **Payments**: Stripe checkout deferred; paid flows via offline/at-the-door tiers (see Q22).
- **Data discipline**: read-only journeys use seeded fixtures; mutating journeys revert their own state or create their own data through the UI (see Q21).
- **Infra**: kept minimal for now — extract the copy-pasted demo-login helper into one shared module when the next spec lands; page objects / `storageState` personas come after the journey suites prove the shape.

## Next Steps

1. Answer the remaining gap-fill questions above
2. Update this document with answers
3. Derive concrete Playwright test suites from each journey (per the agreed P0/P1/P2 ranking in Q27)
4. Implement per the E2E Test Strategy section — run contract first, then P0 suites in order
