# By design: revel-backend

Policy-level intended behaviour that reviewers must not report as a finding. Each entry states the behaviour, the boundary (what is still a bug), and a reference. Reviewers and verifiers read this before reporting. Zero findings is a valid outcome; never pad a report.

## Shareable invitation and token links
- behaviour: organization and event invitation tokens (staff, member, attendee) are bearer tokens; any authenticated user holding a valid link can claim it. "Anyone with the link can claim it" is the feature.
- boundary: a claim must still respect global and organization bans and blacklists, expiry, usage limits, the token's scope, and its role ceiling (a link can never grant more than it was issued for, and never owner rights). Claiming must not leak anything beyond what the claim grants. Tokens issued for single use or a single recipient must not be replayable.
- reference: `docs/architecture/permissions.md`; maintainer decision.

## Personal membership invitations
- behaviour: a personal invitation (`OrganizationToken` with `invitee` set) is claimable only by its invitee, but its public preview (`GET /organizations/tokens/{id}`) and `X-Org-Token` view access behave like any membership link, so the invitee can see the organization before accepting. Staff with `manage_members` can see and withdraw pending invitations.
- boundary: a claim by anyone other than the invitee, any route that edits or lists personal invitations as shareable links (`/tokens`), a preview or response that exposes the invitee, or creating a membership before the invitee accepts are still bugs.
- reference: `docs/architecture/member-register.md` (member invitations); maintainer decision 2026-11-18.

## Staff-originated rows are masked
- behaviour: a box-office ticket, a staff-recorded RSVP or a staff-made event invitation shows staff only what staff supplied (the email they typed, the name they typed) unless the account is known to the organization through its own acts (`events/service/holder_visibility.py`); `StaffViewUserSchema` then returns `known: false` with profile fields `null`. Such rows also give no standing for RSVP on behalf, box-office sale by `user_id` or private notes. A holder who later acts becomes known, and their earlier rows unmask.
- boundary: any admin surface, export or staff notification that shows an unknown holder's phone, legal or preferred name, pronouns, bio or picture is a bug, as is a staff-made row counting as standing, or a new operation projecting `MemberUserSchema`/`MinimalRevelUserSchema` without an entry in `test_holder_profile_guard.py`. Members are never masked. A row created by a ban (`created_by_ban`) confers no standing, and a waitlist entry consumed by a staff sale stops conferring standing (accepted, fails closed).
- reference: ADR-0020; maintainer decision 2026-11-19.

## Staff permissions do what they say
- behaviour: a staff member holding a permission (for example `manage_members` or `invite_to_event`) can perform exactly that action in their organization. The organization owner implicitly passes organization permission checks.
- boundary: a staff member without the permission, or acting on another organization's objects, is a bug. Staff must not be able to escalate to owner, grant themselves or others more than they hold, or edit owner-only fields. An OAuth app token must never exceed its granted scopes: the scope gate runs before any owner or staff shortcut.
- reference: `docs/architecture/permissions.md`.

## Permission class plus manual owner check
- behaviour: object-level access is often enforced twice (a permission class and an explicit owner or organization check in the service). The duplication is defense in depth, not an inconsistency. A bare model class passed to `get_object_or_exception()` is still authorized by the permission class attached to the controller.
- boundary: a reachable path where no layer checks object ownership or organization is a bug.
- reference: `docs/architecture/permissions.md`, `docs/architecture/controllers.md`.

## Reusable unsubscribe tokens
- behaviour: unsubscribe links work on every click, from any email, at any time; they are not single-use.
- boundary: a token may only change the notification and suppression preferences of the user it was issued for, and nothing else. It must not be a credential for any other action, and it must not reveal account data.
- reference: `docs/architecture/email-sending.md`, `docs/architecture/notifications.md`.

## Identifiers and field names in eligibility and error responses
- behaviour: eligibility and validation responses include UUIDs, field names, statuses and next-step hints because the frontend needs them to guide the user (for example which questionnaire to fill in).
- boundary: responses must not expose another user's personal data, attendee-only information (address, attendee list, cancellation reasons) to someone who is not attending, or the contents of resources the caller may not see. Stack traces and internal exception detail must not reach the client.
- reference: `docs/architecture/eligibility-pipeline.md`.

## Attendance is ACTIVE or CHECKED_IN
- behaviour: "attending" means a ticket in status ACTIVE or CHECKED_IN. Only attending users see attendee-only information (address, attendee list, attendee-only resources and polls, and so on). A PENDING ticket (for example an unpaid wire transfer) does not unlock it. Notification audiences are deliberately broader and include PENDING buyers so they hear about updates and cancellations. Capacity, seat, discount and check-in counts use their own definitions.
- boundary: any code that unlocks attendee-only data from `status != CANCELLED`, from PENDING, or from ACTIVE alone (which drops checked-in attendees) is a bug; the definition lives in `Ticket.ATTENDING_STATUSES` and `Ticket.objects.attending()`. Conversely, narrowing a notification audience to attending tickets only is a regression.
- reference: `src/events/tests/test_attending_followups_1278.py`; maintainer decision (#1308).

## Venues are public
- behaviour: a venue (name, address, map URL and embed, city, capacity, description) is public information. It is nested in full in every event, event list, calendar, ticket and ticket-tier response, whatever the event's `address_visibility`. That setting governs only the event's own `address` and map fields (`EventDetailSchema.resolve_address` and siblings). An organizer who needs a hidden location uses a custom address instead of a venue; the frontend form says so (letsrevel/revel-frontend#1254).
- boundary: the event's own `address`, `location_maps_url` and `location_maps_embed` must still be gated by `Event.can_user_see_address` wherever they are returned. Admin-only venue data (sectors, seats, layout metadata beyond the public chart) stays behind organization permissions.
- reference: maintainer decision 2026-11-18 (re-confirmed after a scan flagged it).

## Platform fee is not refunded
- behaviour: when a ticket is refunded, Revel keeps its platform fee (the application fee), mirroring how Stripe keeps its processing fee on a refunded charge. This is a business decision, not an oversight.
- boundary: while the fee exists it must be apportioned correctly: mixed-price carts split it pro rata by ticket price with largest-remainder rounding, never evenly. Refund amounts must still never exceed what was paid for the ticket, and the organization's share must be refunded correctly.
- reference: maintainer decision; apportionment bug tracked and fixed in #753.

## Uniform price per cart
- behaviour: batch checkout is built on one price per cart (discounts are delivered as a price override computed from the tier price; line items and refund matching assume it). This is a known design constraint, not a defect.
- boundary: today no path may let two tickets in one cart end up with different effective prices. A change that introduces per-ticket price differences (per-seat-category pricing, partial discounts) must revisit every site that assumes a scalar cart price; flag a diff that does so without handling them.
- reference: `docs/engineering-notes.md` (checkout reserve/session split); maintainer decision.

## Discount uses follow unfinished checkouts
- behaviour: a discount code use is given back when a still-PENDING ticket is cancelled or removed, including an offline ticket the organizer moved back to pending with "unconfirm" and then cancelled (it is unpaid again). Confirmed (paid) tickets that are later cancelled or refunded keep their use.
- boundary: releasing a use twice for one ticket, or releasing for a ticket that never consumed one, is a bug.
- reference: owner decision 2026-11-18.

## `SYSTEM_TESTING` flag
- behaviour: `SYSTEM_TESTING` is an operational toggle for test and CI behaviour, set through the environment.
- boundary: it must not be settable from a request, and production configuration must not enable it.
- reference: `docs/adr/0008-environment-feature-flags.md`.

## LLM prompt-injection defenses
- behaviour: questionnaire answers are sanitized and evaluated with defensive prompting; these defenses were stress-tested and are accepted as they are.
- boundary: report only a concrete, demonstrated bypass that changes an evaluation outcome or leaks data across users, with the input that does it.
- reference: `docs/architecture/questionnaires.md`; maintainer decision.

## `update_db_instance` saves without `update_fields`
- behaviour: `common.utils.update_db_instance` is a deliberately generic helper; the caller decides which fields are written.
- boundary: a caller that feeds it unrestricted client input containing privileged fields is a bug; callers must pass a restricted schema dumped with `exclude_unset=True`.
- reference: `docs/architecture/permissions.md`; maintainer decision.

## Review scope rules
- Present-day only: a hypothetical future regression ("if someone removes this check") is not a finding.
- Legitimate access: if the "attack" needs access the caller already has, or the impact is trivial or indistinguishable from normal use, it is not a vulnerability.
- Feature requests: missing audit trails, extra logging, soft-delete suggestions, additional confirmation steps, and more throttling on endpoints that are already throttled go to the backlog, not a security report.
- boundary: a missing throttle on an expensive or abusable endpoint (LLM, file scan, geo, mass email) with no throttle at all remains a valid finding.
- reference: maintainer decision.
